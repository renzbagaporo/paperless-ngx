"""LlamaParse OCR through the native document parser pipeline."""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from documents.parsers import ParseError
from paperless.config import OcrConfig
from paperless.config import RemoteOCRConfig
from paperless.network import create_pinned_httpx_client
from paperless.parsers.remote import RemoteDocumentParser
from paperless.parsers.utils import extract_pdf_text
from paperless.parsers.utils import post_process_text

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger("paperless.parsing.llamaparse")

DEFAULT_ENDPOINT = "https://api.cloud.llamaindex.ai"
POLL_INTERVAL = 3.0
JOB_TIMEOUT = 1800.0
REQUEST_TIMEOUT = 60.0

LANGUAGES = {
    "eng": "en",
    "deu": "de",
    "fra": "fr",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "nld": "nl",
    "rus": "ru",
    "ara": "ar",
    "hin": "hi",
    "jpn": "ja",
    "kor": "ko",
    "chi_sim": "ch_sim",
    "chi_tra": "ch_tra",
    "pol": "pl",
    "ces": "cs",
    "dan": "da",
    "nor": "no",
    "swe": "sv",
    "tur": "tr",
    "ukr": "uk",
    "vie": "vi",
    "tha": "th",
}


def language_codes(language: str) -> list[str]:
    """Translate Paperless's Tesseract hints, preserving primary-language order."""
    result = []
    for code in language.split("+"):
        code = code.strip().removesuffix("_vert")
        mapped = LANGUAGES.get(code)
        if mapped and mapped not in result:
            result.append(mapped)
    return result


class LlamaParseDocumentParser(RemoteDocumentParser):
    """Produce indexed text and a searchable PDF without rasterising PDF pages."""

    name = "Paperless-ngx LlamaParse OCR Parser"

    @classmethod
    def score(
        cls,
        mime_type: str,
        filename: str,
        path: Path | None = None,
    ) -> int | None:
        config = RemoteOCRConfig()
        if (
            config.remote_ocr_engine == "llamaparse"
            and config.remote_ocr_api_key
            and mime_type in cls.supported_mime_types()
        ):
            return 20
        return None

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        import img2pdf
        import pikepdf

        config = RemoteOCRConfig()
        if config.remote_ocr_engine != "llamaparse" or not config.remote_ocr_api_key:
            raise ParseError(
                "LlamaParse OCR requires its engine and API key to be configured",
            )

        # The consumer suppresses an archive for digital PDFs. Keep their exact
        # existing text and avoid uploading their contents to an external service.
        if not produce_archive and mime_type == "application/pdf":
            text = post_process_text(extract_pdf_text(document_path, log=logger))
            if text and text.strip():
                self._text = text
                return

        source = document_path
        if mime_type != "application/pdf":
            source = self._tempdir / "source.pdf"
            try:
                source.write_bytes(img2pdf.convert(str(document_path)))
            except Exception as error:
                raise ParseError(
                    "Unable to convert the image for LlamaParse OCR",
                ) from error

        try:
            with pikepdf.open(source) as pdf:
                page_count = len(pdf.pages)
            if page_count == 0:
                raise ParseError("Cannot OCR a PDF with no pages")
            result = self._request_result(source, config)
            pages = self._validated_pages(result, page_count)
            self._text = "\n\n".join(page["text"] for page in pages if page["text"])
            if produce_archive:
                archive = self._tempdir / "archive.pdf"
                self._overlay(source, pages, archive)
                self._archive_path = archive
        except ParseError:
            raise
        except Exception as error:
            raise ParseError(
                "LlamaParse returned an unusable document result",
            ) from error

    def _request_result(self, source: Path, config: RemoteOCRConfig) -> dict:
        """Upload, poll and delete the source file, including on failed jobs."""
        from django.conf import settings

        endpoint = (config.remote_ocr_endpoint or DEFAULT_ENDPOINT).rstrip("/")

        try:
            client = create_pinned_httpx_client(
                endpoint,
                allow_internal=settings.REMOTE_OCR_ALLOW_INTERNAL_ENDPOINTS,
                base_url=endpoint,
                headers={"Authorization": f"Bearer {config.remote_ocr_api_key}"},
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
            )
        except ValueError as error:
            raise ParseError("Invalid LlamaParse endpoint") from error

        file_id = None
        with client:
            try:
                with source.open("rb") as handle:
                    response = client.post(
                        "/api/v1/beta/files",
                        data={"purpose": "parse"},
                        files={"file": ("document.pdf", handle, "application/pdf")},
                    )
                response.raise_for_status()
                file_id = response.json()["id"]
                payload = {
                    "file_id": file_id,
                    "tier": "agentic",
                    "version": "latest",
                    "disable_cache": True,
                    "processing_control": {
                        "job_failure_conditions": {"fail_on_image_ocr_error": True},
                    },
                }
                languages = language_codes(OcrConfig().language)
                if languages:
                    payload["processing_options"] = {
                        "ocr_parameters": {"languages": languages},
                    }
                response = client.post(
                    "/api/v2/parse",
                    json=payload,
                )
                response.raise_for_status()
                job_id = quote(str(response.json()["id"]), safe="")
                deadline = time.monotonic() + JOB_TIMEOUT
                while time.monotonic() < deadline:
                    response = client.get(
                        f"/api/v2/parse/{job_id}",
                        params={"expand": "markdown,text,items"},
                    )
                    response.raise_for_status()
                    result = response.json()
                    status = result.get("job", {}).get("status")
                    if status == "COMPLETED":
                        return result
                    if status in ("FAILED", "CANCELLED", "PARTIAL_SUCCESS"):
                        raise ParseError("LlamaParse did not complete every page")
                    time.sleep(POLL_INTERVAL)
                raise ParseError("LlamaParse OCR exceeded its processing deadline")
            except httpx.HTTPStatusError as error:
                raise ParseError(
                    f"LlamaParse request failed (HTTP {error.response.status_code})",
                ) from error
            except httpx.HTTPError as error:
                raise ParseError("LlamaParse could not be reached") from error
            except (ValueError, KeyError, TypeError) as error:
                raise ParseError("LlamaParse returned an invalid response") from error
            finally:
                if file_id is not None:
                    try:
                        response = client.delete(
                            f"/api/v1/beta/files/{quote(str(file_id), safe='')}",
                        )
                        response.raise_for_status()
                    except (httpx.HTTPError, ValueError):
                        logger.warning("LlamaParse source-file deletion failed")

    @staticmethod
    def _validated_pages(result: dict, page_count: int) -> list[dict]:
        """Check completeness before accepting text or emitting a searchable PDF."""
        indexed = {}
        text_pages = {
            p["page_number"]: p.get("text", "")
            for p in result.get("text", {}).get("pages", [])
        }
        markdown_pages = {
            p["page_number"]: p.get("markdown", "")
            for p in result.get("markdown", {}).get("pages", [])
        }
        for page in result.get("items", {}).get("pages", []):
            number = page.get("page_number")
            if (
                type(number) is not int
                or number in indexed
                or not 1 <= number <= page_count
                or page.get("success") is False
                or not all(
                    isinstance(page.get(key), (int, float))
                    and math.isfinite(page[key])
                    and page[key] > 0
                    for key in ("page_width", "page_height")
                )
            ):
                raise ParseError("LlamaParse returned invalid or duplicate page data")
            if number not in text_pages and number not in markdown_pages:
                raise ParseError("LlamaParse did not return text for every source page")
            text = text_pages.get(number) or markdown_pages.get(number) or ""
            if not isinstance(text, str):
                raise ParseError("LlamaParse returned invalid page text")
            indexed[number] = {**page, "text": text.strip()}
        if set(indexed) != set(range(1, page_count + 1)):
            raise ParseError("LlamaParse did not return every source page")
        return [indexed[number] for number in range(1, page_count + 1)]

    @staticmethod
    def _text_boxes(items: list[dict]) -> Iterator[tuple[str, dict | None]]:
        for item in items:
            if not isinstance(item, dict):
                continue
            nested = item.get("items")
            if isinstance(nested, list):
                yield from LlamaParseDocumentParser._text_boxes(nested)
                continue
            text = item.get("value") or item.get("md") or ""
            if not text and isinstance(item.get("rows"), list):
                text = "\n".join(
                    " ".join(str(cell) for cell in row)
                    for row in item["rows"]
                    if isinstance(row, list)
                )
            boxes = item.get("bbox") or []
            if isinstance(text, str) and text.strip():
                yield text, boxes[0] if boxes else None

    def _overlay(self, source: Path, pages: list[dict], out_path: Path) -> None:
        """Add invisible text only; keep original vectors and compressed images."""
        import ocrmypdf
        import pikepdf
        from ocrmypdf.font import MultiFontManager
        from ocrmypdf.fpdf_renderer import Fpdf2PdfRenderer
        from ocrmypdf.models.ocr_element import BoundingBox
        from ocrmypdf.models.ocr_element import OcrElement

        fonts = MultiFontManager(Path(ocrmypdf.__file__).parent / "data")
        with pikepdf.open(source) as pdf:
            for index, (page, item_page) in enumerate(
                zip(pdf.pages, pages, strict=True),
            ):
                width, height = item_page["page_width"], item_page["page_height"]
                children = []
                boxes = list(self._text_boxes(item_page.get("items") or []))
                if not boxes and item_page["text"]:
                    boxes = [
                        (item_page["text"], {"x": 0, "y": 0, "w": width, "h": height}),
                    ]
                unboxed_lines = sum(
                    len(text.splitlines()) for text, box in boxes if box is None
                )
                unboxed_top = 0.0
                for text, box in boxes:
                    if box is None:
                        box_height = height * len(text.splitlines()) / unboxed_lines
                        box = {"x": 0, "y": unboxed_top, "w": width, "h": box_height}
                        unboxed_top += box_height
                    try:
                        x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
                        angle = float(box.get("r", 0))
                        if (
                            not all(math.isfinite(n) for n in (x, y, w, h, angle))
                            or min(w, h) <= 0
                        ):
                            raise ValueError
                    except (KeyError, ValueError, TypeError) as error:
                        raise ParseError(
                            "LlamaParse returned an invalid text bounding box",
                        ) from error
                    lines = [line.strip() for line in text.splitlines() if line.strip()]
                    band_height = min(h / max(len(lines), 1), 24.0)
                    for line_number, line in enumerate(lines):
                        top = max(0.0, y + line_number * h / len(lines))
                        bbox = BoundingBox(
                            max(0.0, x),
                            top,
                            min(width, x + w),
                            min(height, top + band_height),
                        )
                        if bbox.width <= 0 or bbox.height <= 0:
                            raise ParseError("LlamaParse text lies outside its page")
                        word = OcrElement(ocr_class="ocrx_word", text=line, bbox=bbox)
                        children.append(
                            # LlamaParse rotations are clockwise; hOCR uses
                            # counter-clockwise angles.
                            OcrElement(
                                ocr_class="ocr_line",
                                bbox=bbox,
                                children=[word],
                                textangle=-angle,
                            ),
                        )
                ocr_page = OcrElement(
                    ocr_class="ocr_page",
                    bbox=BoundingBox(0, 0, width, height),
                    children=children,
                )
                layer_path = self._tempdir / f"text-{index}.pdf"
                Fpdf2PdfRenderer(
                    ocr_page,
                    dpi=72,
                    multi_font_manager=fonts,
                    invisible_text=True,
                ).render(layer_path)
                # LlamaParse coordinates include /Rotate. Flatten it into the
                # original page content before applying the viewport text layer.
                page.flatten_rotation()
                with pikepdf.open(layer_path) as layer:
                    page.add_overlay(layer.pages[0], pikepdf.Rectangle(page.cropbox))
            pdf.save(out_path)
