from __future__ import annotations

import copy
import io
import json
from typing import TYPE_CHECKING
from unittest.mock import Mock

import httpx
import img2pdf
import pikepdf
import pytest
from PIL import Image

from documents.parsers import ParseError
from paperless.models import ApplicationConfiguration
from paperless.parsers import ParserProtocol
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless.parsers.llamaparse import language_codes
from paperless.parsers.registry import ParserRegistry
from paperless.parsers.utils import extract_pdf_text

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.django_db


@pytest.fixture()
def llama_settings(settings):
    settings.REMOTE_OCR_ENGINE = "llamaparse"
    settings.REMOTE_OCR_API_KEY = "fixture-llama-key"
    settings.REMOTE_OCR_ENDPOINT = None
    return settings


@pytest.fixture()
def llama_parser(paperless_dirs, llama_settings):
    with LlamaParseDocumentParser() as parser:
        yield parser


@pytest.fixture()
def scan_pdf(tmp_path: Path) -> Path:
    """A compressed JPEG-backed scan, requiring no OCR or external resources."""
    image = Image.new("RGB", (180, 240), (241, 235, 214))
    image.putpixel((10, 20), (0, 0, 0))
    jpeg = io.BytesIO()
    image.save(jpeg, format="JPEG", quality=75)
    path = tmp_path / "scan.pdf"
    path.write_bytes(img2pdf.convert(jpeg.getvalue()))
    return path


def result_for(source: Path, text="東京ガス ガス料金のお知らせ") -> dict:
    with pikepdf.open(source) as pdf:
        pages = []
        for number, page in enumerate(pdf.pages, 1):
            width = float(page.cropbox[2]) - float(page.cropbox[0])
            height = float(page.cropbox[3]) - float(page.cropbox[1])
            if page.rotation in (90, 270):
                width, height = height, width
            pages.append(
                {
                    "page_number": number,
                    "page_width": width,
                    "page_height": height,
                    "success": True,
                    "items": [
                        {
                            "type": "text",
                            "value": text,
                            "bbox": [
                                {
                                    "x": 8,
                                    "y": 15,
                                    "w": width - 16,
                                    "h": 14,
                                },
                            ],
                        },
                    ],
                },
            )
    return {
        "job": {"status": "COMPLETED"},
        "text": {
            "pages": [{"page_number": p["page_number"], "text": text} for p in pages],
        },
        "markdown": {
            "pages": [
                {"page_number": p["page_number"], "markdown": text} for p in pages
            ],
        },
        "items": {"pages": pages},
    }


class TestLlamaParseSelection:
    def test_preserves_plain_and_formatted_page_evidence(self, scan_pdf):
        result = result_for(scan_pdf, "plain 1234")
        result["markdown"]["pages"][0]["markdown"] = "formatted 777888"
        pages = LlamaParseDocumentParser._validated_pages(result, 1)
        assert pages[0]["plain_text"] == "plain 1234"
        assert pages[0]["markdown"] == "formatted 777888"
        assert pages[0]["text"] == "plain 1234"

    def test_protocol(self, llama_parser):
        assert isinstance(llama_parser, ParserProtocol)
        assert llama_parser.uses_remote_service

    def test_engine_selection(self, llama_settings):
        assert LlamaParseDocumentParser.score("application/pdf", "scan.pdf") == 20
        assert LlamaParseDocumentParser.score("text/plain", "note.txt") is None
        llama_settings.REMOTE_OCR_ENGINE = "azureai"
        assert LlamaParseDocumentParser.score("application/pdf", "scan.pdf") is None
        llama_settings.REMOTE_OCR_ENGINE = "llamaparse"
        llama_settings.REMOTE_OCR_API_KEY = ""
        assert LlamaParseDocumentParser.score("application/pdf", "scan.pdf") is None

    def test_database_engine_configuration(self, llama_settings):
        ApplicationConfiguration.objects.update_or_create(
            pk=1,
            defaults={
                "remote_ocr_engine": "llamaparse",
                "remote_ocr_api_key": "database-key",
            },
        )
        llama_settings.REMOTE_OCR_ENGINE = None
        llama_settings.REMOTE_OCR_API_KEY = None
        assert LlamaParseDocumentParser.score("application/pdf", "scan.pdf") == 20

    def test_remote_processing_gate(self, llama_settings):
        registry = ParserRegistry()
        registry.register_defaults()
        assert (
            registry.get_parser_for_file(
                "application/pdf",
                "scan.pdf",
                allow_remote=True,
            )
            is LlamaParseDocumentParser
        )
        assert (
            registry.get_parser_for_file(
                "application/pdf",
                "scan.pdf",
                allow_remote=False,
            )
            is not LlamaParseDocumentParser
        )


class TestLlamaParseRequests:
    def mock_cloud(
        self,
        monkeypatch,
        result,
        *,
        failed_create=False,
        status=None,
        delete_status=204,
    ):
        requests = []
        client_config = {}

        def handler(request):
            requests.append(request)
            if request.method == "DELETE":
                return httpx.Response(delete_status)
            if request.url.path == "/api/v1/beta/files":
                return httpx.Response(200, json={"id": "fixture-file"})
            if request.url.path == "/api/v2/parse":
                return httpx.Response(
                    500 if failed_create else 200,
                    json={"id": "fixture-job"},
                )
            if status:
                return httpx.Response(200, json={"job": {"status": status}})
            return httpx.Response(200, json=result)

        def client_factory(endpoint, **kwargs):
            client_config.update(endpoint=endpoint, **kwargs)
            kwargs.pop("allow_internal", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(
            "paperless.parsers.llamaparse.create_pinned_httpx_client",
            client_factory,
        )
        return requests, client_config

    def test_rest_contract_and_source_cleanup(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
    ):
        requests, config = self.mock_cloud(monkeypatch, result_for(scan_pdf))
        llama_parser.parse(scan_pdf, "application/pdf")
        assert [r.method for r in requests] == ["POST", "POST", "GET", "DELETE"]
        assert config["endpoint"] == "https://api.cloud.llamaindex.ai"
        assert config["follow_redirects"] is False
        assert all(
            r.headers["authorization"] == "Bearer fixture-llama-key" for r in requests
        )
        body = json.loads(requests[1].content)
        assert body == {
            "file_id": "fixture-file",
            "tier": "agentic",
            "version": "latest",
            "disable_cache": True,
            "processing_options": {"ocr_parameters": {"languages": ["en"]}},
            "processing_control": {
                "job_failure_conditions": {"fail_on_image_ocr_error": True},
            },
        }
        assert requests[2].url.params["expand"] == "markdown,text,items"
        assert requests[-1].url.path == "/api/v1/beta/files/fixture-file"
        assert llama_parser.get_text() == "東京ガス ガス料金のお知らせ"
        assert llama_parser.get_archive_path().is_file()

    def test_language_hints_preserve_japanese_and_chinese(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
        llama_settings,
    ):
        llama_settings.OCR_LANGUAGE = "eng+jpn+chi_sim"
        requests, _ = self.mock_cloud(monkeypatch, result_for(scan_pdf))
        llama_parser.parse(scan_pdf, "application/pdf")
        body = json.loads(requests[1].content)
        assert body["processing_options"]["ocr_parameters"]["languages"] == [
            "en",
            "ja",
            "ch_sim",
        ]
        assert language_codes("jpn+jpn_vert+chi_tra_vert+kor_vert") == [
            "ja",
            "ch_tra",
            "ko",
        ]

    def test_cleanup_when_job_creation_fails(self, llama_parser, scan_pdf, monkeypatch):
        requests, _ = self.mock_cloud(
            monkeypatch,
            result_for(scan_pdf),
            failed_create=True,
        )
        with pytest.raises(ParseError, match="HTTP 500"):
            llama_parser.parse(scan_pdf, "application/pdf")
        assert requests[-1].method == "DELETE"
        assert llama_parser.get_archive_path() is None

    @pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "PARTIAL_SUCCESS"])
    def test_incomplete_job_is_rejected(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
        status,
    ):
        requests, _ = self.mock_cloud(monkeypatch, result_for(scan_pdf), status=status)
        with pytest.raises(ParseError, match="every page"):
            llama_parser.parse(scan_pdf, "application/pdf")
        assert requests[-1].method == "DELETE"

    def test_timeout_still_deletes_upload(self, llama_parser, scan_pdf, monkeypatch):
        requests, _ = self.mock_cloud(
            monkeypatch,
            result_for(scan_pdf),
            status="PENDING",
        )
        monkeypatch.setattr("paperless.parsers.llamaparse.JOB_TIMEOUT", -1)
        with pytest.raises(ParseError, match="deadline"):
            llama_parser.parse(scan_pdf, "application/pdf")
        assert requests[-1].method == "DELETE"

    def test_deletion_failure_is_reported_without_discarding_valid_result(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
        caplog,
    ):
        self.mock_cloud(monkeypatch, result_for(scan_pdf), delete_status=500)
        llama_parser.parse(scan_pdf, "application/pdf")
        assert "source-file deletion failed" in caplog.text
        assert "fixture-llama-key" not in caplog.text
        assert llama_parser.get_text()

    def test_disallowed_endpoint_fails_without_uploading(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
    ):
        client = Mock(side_effect=ValueError("blocked internal endpoint"))
        monkeypatch.setattr(
            "paperless.parsers.llamaparse.create_pinned_httpx_client",
            client,
        )
        with pytest.raises(ParseError, match="Invalid LlamaParse endpoint"):
            llama_parser.parse(scan_pdf, "application/pdf")
        client.assert_called_once()

    def test_digital_pdf_without_archive_avoids_cloud(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "paperless.parsers.llamaparse.extract_pdf_text",
            lambda *args, **kwargs: "Exact digital text",
        )
        request = Mock(side_effect=AssertionError("must not upload digital document"))
        monkeypatch.setattr(llama_parser, "_request_result", request)
        llama_parser.parse(scan_pdf, "application/pdf", produce_archive=False)
        assert llama_parser.get_text() == "Exact digital text"
        assert llama_parser.get_archive_path() is None
        request.assert_not_called()


class TestLlamaParsePdf:
    def test_cjk_layer_preserves_original_compressed_image(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
    ):
        monkeypatch.setattr(
            llama_parser,
            "_request_result",
            lambda *args: result_for(scan_pdf),
        )
        with pikepdf.open(scan_pdf) as pdf:
            original_image = next(iter(pdf.pages[0].images.values())).read_raw_bytes()
        llama_parser.parse(scan_pdf, "application/pdf")
        archive = llama_parser.get_archive_path()
        with pikepdf.open(archive) as pdf:
            assert len(pdf.pages) == 1
            assert (
                next(iter(pdf.pages[0].images.values())).read_raw_bytes()
                == original_image
            )
        text = extract_pdf_text(archive)
        assert "東京ガス" in text.replace(" ", "")

    def test_rotated_pdf_keeps_viewport_and_extractable_text(
        self,
        llama_parser,
        scan_pdf,
        tmp_path,
        monkeypatch,
    ):
        rotated = tmp_path / "rotated.pdf"
        with pikepdf.open(scan_pdf) as pdf:
            pdf.pages[0].rotate(90)
            width = float(pdf.pages[0].cropbox[3]) - float(pdf.pages[0].cropbox[1])
            height = float(pdf.pages[0].cropbox[2]) - float(pdf.pages[0].cropbox[0])
            pdf.save(rotated)
        monkeypatch.setattr(
            llama_parser,
            "_request_result",
            lambda *args: result_for(rotated, "Water Supply Notice"),
        )
        llama_parser.parse(rotated, "application/pdf")
        with pikepdf.open(llama_parser.get_archive_path()) as pdf:
            assert pdf.pages[0].rotation == 0
            assert (
                float(pdf.pages[0].cropbox[2]) - float(pdf.pages[0].cropbox[0]) == width
            )
            assert (
                float(pdf.pages[0].cropbox[3]) - float(pdf.pages[0].cropbox[1])
                == height
            )
        assert "Water Supply Notice" in extract_pdf_text(
            llama_parser.get_archive_path(),
        )

    def test_multiple_pages_stay_in_source_order(
        self,
        llama_parser,
        scan_pdf,
        tmp_path,
        monkeypatch,
    ):
        combined = tmp_path / "two.pdf"
        with pikepdf.open(scan_pdf) as source, pikepdf.new() as pdf:
            pdf.pages.extend(source.pages)
            pdf.pages.extend(source.pages)
            pdf.save(combined)
        result = result_for(combined, "Water notice")
        result["items"]["pages"].reverse()
        monkeypatch.setattr(llama_parser, "_request_result", lambda *args: result)
        llama_parser.parse(combined, "application/pdf")
        with pikepdf.open(llama_parser.get_archive_path()) as pdf:
            assert len(pdf.pages) == 2

    def test_multipage_tiff_is_not_truncated(
        self,
        llama_parser,
        tmp_path,
        monkeypatch,
    ):
        image_path = tmp_path / "scans.tiff"
        image = Image.new("RGB", (180, 240), "white")
        second = Image.new("RGB", (180, 240), "gray")
        image.save(image_path, save_all=True, append_images=[second])

        def remote_result(source, config):
            with pikepdf.open(source) as pdf:
                assert len(pdf.pages) == 2
            return result_for(source, "Water Supply Notice")

        monkeypatch.setattr(llama_parser, "_request_result", remote_result)
        llama_parser.parse(image_path, "image/tiff")
        with pikepdf.open(llama_parser.get_archive_path()) as pdf:
            assert len(pdf.pages) == 2

    def test_blank_page_is_retained_when_other_pages_have_text(
        self,
        llama_parser,
        scan_pdf,
        tmp_path,
        monkeypatch,
    ):
        combined = tmp_path / "blank-and-scan.pdf"
        with pikepdf.open(scan_pdf) as source, pikepdf.new() as pdf:
            pdf.pages.extend(source.pages)
            pdf.add_blank_page(page_size=(180, 240))
            pdf.save(combined)
        result = result_for(combined, "Water notice")
        result["text"]["pages"][1]["text"] = ""
        result["markdown"]["pages"][1]["markdown"] = ""
        result["items"]["pages"][1]["items"] = []
        monkeypatch.setattr(llama_parser, "_request_result", lambda *args: result)
        llama_parser.parse(combined, "application/pdf")
        with pikepdf.open(llama_parser.get_archive_path()) as pdf:
            assert len(pdf.pages) == 2
        assert llama_parser.get_text() == "Water notice"

    @pytest.mark.parametrize(
        "change",
        ["missing", "duplicate", "failed", "dimensions", "missing-text"],
    )
    def test_invalid_page_results_fail(
        self,
        llama_parser,
        scan_pdf,
        monkeypatch,
        change,
    ):
        result = copy.deepcopy(result_for(scan_pdf))
        if change == "missing":
            result["items"]["pages"] = []
        elif change == "duplicate":
            result["items"]["pages"] *= 2
        elif change == "failed":
            result["items"]["pages"][0]["success"] = False
        elif change == "dimensions":
            result["items"]["pages"][0]["page_width"] = 0
        else:
            result["text"]["pages"] = []
            result["markdown"]["pages"] = []
        monkeypatch.setattr(llama_parser, "_request_result", lambda *args: result)
        with pytest.raises(ParseError):
            llama_parser.parse(scan_pdf, "application/pdf")
        assert llama_parser.get_archive_path() is None

    def test_unboxed_text_remains_searchable(self, llama_parser, scan_pdf, monkeypatch):
        result = result_for(scan_pdf, "Water notice\nAmount due 1234")
        result["items"]["pages"][0]["items"].append(
            {"type": "table", "md": "Amount due 1234"},
        )
        monkeypatch.setattr(llama_parser, "_request_result", lambda *args: result)
        llama_parser.parse(scan_pdf, "application/pdf")
        assert "1234" in extract_pdf_text(llama_parser.get_archive_path())
