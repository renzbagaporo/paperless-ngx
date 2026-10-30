"""LlamaParse OCR through the native document parser pipeline."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import math
import re
import socket
import subprocess
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
REQUEST_ATTEMPTS = 5

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
    "vie": "vi",  # codespell:ignore vie
    "tha": "th",  # codespell:ignore tha
}


class RemoteOCRUnavailable(ParseError):
    """Provider failure with a safe, durable reason for a saved document."""

    def __init__(self, message: str, *, reason: str = "provider"):
        super().__init__(message)
        self.reason = reason


class RemoteOCRConfigurationError(ParseError):
    """A rejected endpoint must never be silently used or bypassed."""


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

    @staticmethod
    def _page_identity(page) -> str:
        """Identify unchanged physical PDF content across splits and rotations."""
        import pikepdf

        with pikepdf.new() as pdf, io.BytesIO() as output:
            pdf.pages.append(page)
            pdf.pages[0].rotate(0, relative=False)
            pdf.remove_unreferenced_resources()
            pdf.save(
                output,
                static_id=True,
                compress_streams=False,
                normalize_content=True,
                object_stream_mode=pikepdf.ObjectStreamMode.disable,
            )
            return hashlib.sha256(output.getvalue()).hexdigest()

    @staticmethod
    def _job_config(config) -> str:
        value = [
            (config.remote_ocr_endpoint or DEFAULT_ENDPOINT).rstrip("/"),
            "agentic",
            "latest",
            language_codes(OcrConfig().language),
        ]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    @staticmethod
    def job_for_page(job: dict, index: int) -> dict:
        """Return portable private state for one physical source page."""
        if "pages" not in job:
            return copy.deepcopy(job)
        result = {
            key: copy.deepcopy(value) for key, value in job.items() if key != "pages"
        }
        result.update(job["pages"][index - 1], page=index)
        return result

    def _remember_job(self, job: dict) -> None:
        self._pending_job = copy.deepcopy(job)
        callback = getattr(self, "_job_state_callback", None)
        if callback is not None:
            callback(copy.deepcopy(job))

    def get_source_rendition(self) -> Path | None:
        """Return the validated working PDF, including converted raw images."""
        return getattr(self, "_source_rendition", None)

    def get_layout_rotation_deltas(self, source: Path) -> list[int]:
        """Map raw provider coordinates to the selected stored rendition."""
        rendered = self._archive_path is not None and Path(source) == Path(
            self._archive_path,
        )
        attribute = "_layout_archive_deltas" if rendered else "_layout_original_deltas"
        return list(getattr(self, attribute, [0] * len(self.get_review_pages())))

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
        auto_rotate: bool = True,
    ) -> None:
        import img2pdf
        import pikepdf

        config = RemoteOCRConfig()
        if config.remote_ocr_engine != "llamaparse" or not config.remote_ocr_api_key:
            raise ParseError(
                "LlamaParse OCR requires its engine and API key to be configured",
            )
        self._review_pages = []
        self.ocr_status = {"status": "ok"}
        self._pending_job = None
        self._source_rendition = None
        self._layout_original_deltas = []
        self._layout_archive_deltas = []

        # The consumer suppresses an archive for digital PDFs. Keep their exact
        # existing text and avoid uploading their contents to an external service.
        if not produce_archive and mime_type == "application/pdf":
            text = post_process_text(extract_pdf_text(document_path, log=logger))
            if text and text.strip():
                self._text = text
                self.ocr_status = {"status": "skip"}
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

        # Validate the local file independently of provider availability. A bad
        # upload still fails; a valid upload must survive an OCR outage.
        with pikepdf.open(source) as pdf:
            page_count = len(pdf.pages)
            geometry = [
                {
                    "identity": self._page_identity(page),
                    "original_rotation": int(page.get("/Rotate", 0)) % 360,
                }
                for page in pdf.pages
            ]
        if page_count == 0:
            raise ParseError("Cannot OCR a PDF with no pages")
        self._source_rendition = source
        try:
            resume = getattr(self, "_resume_job", None)
            correction = 0
            if resume:
                self._pending_job = copy.deepcopy(resume)
                if not resume.get("id"):
                    raise RemoteOCRUnavailable(
                        "A paid submission needs review",
                        reason="submission_review",
                    )
                if (
                    page_count != 1
                    or resume.get("identity") != geometry[0]["identity"]
                    or resume.get("config") != self._job_config(config)
                ):
                    self._remember_job({**resume, "state": "terminal"})
                    raise RemoteOCRUnavailable(
                        "Saved OCR job does not match this page or configuration",
                        reason="job_review",
                    )
                submitted = self._tempdir / "resumed_source.pdf"
                geometry[0]["submitted_rotation"] = resume["submitted_rotation"]
                with pikepdf.open(source) as pdf:
                    if not auto_rotate:
                        correction = (
                            geometry[0]["original_rotation"]
                            - resume["submitted_rotation"]
                        ) % 360
                    pdf.pages[0].rotate(resume["submitted_rotation"], relative=False)
                    pdf.save(submitted)
            else:
                submitted = self._orient(source) if auto_rotate else source
                with pikepdf.open(submitted) as pdf:
                    for item, page in zip(geometry, pdf.pages, strict=True):
                        item["submitted_rotation"] = int(page.get("/Rotate", 0)) % 360
                self._job_geometry = {
                    "config": self._job_config(config),
                    "source_checksum": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "count": page_count,
                    "pages": geometry,
                }
            self._layout_original_deltas = [
                (item["submitted_rotation"] - item["original_rotation"]) % 360
                for item in geometry
            ]
            self._layout_archive_deltas = [(-correction) % 360 for _ in geometry]
            submitted = self._without_existing_ocr(submitted)
            result = self._request_result(submitted, config)
            if resume:
                pages = self._validated_pages(result, resume["count"])
                selected = copy.deepcopy(pages[resume["page"] - 1])
                selected["page_number"] = 1
                result = {
                    "job": result.get("job", {}),
                    "text": {
                        "pages": [{"page_number": 1, "text": selected["plain_text"]}],
                    },
                    "markdown": {
                        "pages": [{"page_number": 1, "markdown": selected["markdown"]}],
                    },
                    "items": {"pages": [selected]},
                }
            self.page_provenance = {
                "engine": "llamaparse",
                "tier": "agentic",
                "version": "latest",
                "job_id": result.get("job", {}).get("id"),
            }
            try:
                pages = self._validated_pages(result, page_count)
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise RemoteOCRUnavailable(
                    "LlamaParse returned invalid page data",
                ) from error
            self._review_pages = pages
            self._text = "\n\n".join(page["text"] for page in pages if page["text"])
            if produce_archive:
                archive = self._tempdir / "archive.pdf"
                self._overlay(submitted, pages, archive)
                if correction:
                    corrected = self._tempdir / "corrected.pdf"
                    with pikepdf.open(archive) as pdf:
                        pdf.pages[0].rotate(correction, relative=True)
                        pdf.save(corrected)
                    corrected.replace(archive)
                self._archive_path = archive
        except RemoteOCRConfigurationError:
            raise
        except Exception as error:
            if (
                isinstance(error, ParseError)
                and not isinstance(error, RemoteOCRUnavailable)
                and self._pending_job
            ):
                self._remember_job({**self._pending_job, "state": "terminal"})
            self._review_pages = []
            self._archive_path = (
                source if produce_archive and mime_type != "application/pdf" else None
            )
            self._text = post_process_text(extract_pdf_text(source, log=logger)) or ""
            self.ocr_status = {
                "status": "pending",
                "reason": getattr(error, "reason", "provider"),
            }
            if self._pending_job is not None:
                self.ocr_status["job"] = copy.deepcopy(self._pending_job)
            logger.warning("Remote OCR deferred; the original document will be saved")

    def get_review_pages(self) -> list[dict]:
        """Return page-local evidence for optional native ingestion checks."""
        return getattr(self, "_review_pages", [])

    def get_ocr_status(self) -> dict:
        return self.ocr_status

    def _without_existing_ocr(self, source: Path) -> Path:
        """Replace invisible OCR while retaining printed text and compressed scans."""
        import pikepdf
        from ocrmypdf._graft import strip_invisible_text

        target = self._tempdir / "ocr_input.pdf"
        with pikepdf.open(source) as pdf:
            for page in pdf.pages:
                strip_invisible_text(pdf, page)
            pdf.remove_unreferenced_resources()
            pdf.save(target)
        return target

    def _orient(self, source: Path) -> Path:
        """Detect sideways scans locally; rotate PDF content without recompression."""
        import pikepdf

        config = OcrConfig()
        if not config.rotate:
            return source
        changed = False
        with pikepdf.open(source) as pdf:
            for page in pdf.pages:
                try:
                    preview_pdf = self._tempdir / "orientation.pdf"
                    with pikepdf.new() as preview:
                        preview.pages.append(page)
                        preview.save(preview_pdf)
                    preview_image = self._tempdir / "orientation.png"
                    subprocess.run(
                        [
                            "pdftoppm",
                            "-singlefile",
                            "-scale-to",
                            "1600",
                            "-png",
                            str(preview_pdf),
                            str(preview_image.with_suffix("")),
                        ],
                        capture_output=True,
                        check=True,
                        timeout=120,
                    )
                    result = subprocess.run(
                        ["tesseract", str(preview_image), "-", "--psm", "0"],
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                    )
                    rotation = re.search(r"Rotate: (\d+)", result.stdout)
                    confidence = re.search(
                        r"Orientation confidence: ([\d.]+)",
                        result.stdout,
                    )
                    if (
                        rotation
                        and confidence
                        and (float(confidence.group(1)) >= config.rotate_threshold)
                    ):
                        angle = int(rotation.group(1)) % 360
                        if angle in (90, 180, 270):
                            page.rotate(angle, relative=True)
                            changed = True
                except (OSError, subprocess.SubprocessError):
                    logger.warning("Optional scan orientation check unavailable")
            if changed:
                target = self._tempdir / "oriented.pdf"
                pdf.save(target)
                return target
        return source

    def _request_result(self, source: Path, config: RemoteOCRConfig) -> dict:
        """Upload, poll and delete the source file, including on failed jobs."""
        from django.conf import settings

        self._pending_job = getattr(self, "_pending_job", None)

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
            if isinstance(error.__cause__, socket.gaierror):
                raise RemoteOCRUnavailable(
                    "LlamaParse hostname could not be resolved",
                    reason="unavailable",
                ) from error
            raise RemoteOCRConfigurationError("Invalid LlamaParse endpoint") from error

        file_id = None
        with client:
            try:
                resume = getattr(self, "_resume_job", None)
                if resume:
                    job_id = quote(str(resume["id"]), safe="")
                else:
                    with source.open("rb") as handle:
                        response = self._send(
                            client,
                            "POST",
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
                    self._remember_job(
                        {**getattr(self, "_job_geometry", {}), "state": "submitting"},
                    )
                    response = self._send(
                        client,
                        "POST",
                        "/api/v2/parse",
                        json=payload,
                    )
                    response.raise_for_status()
                    identifier = response.json()["id"]
                    if (
                        not isinstance(identifier, str)
                        or not identifier.strip()
                        or len(identifier) > 1024
                        or any(
                            ord(character) < 32 or ord(character) == 127
                            for character in identifier
                        )
                    ):
                        raise RemoteOCRUnavailable(
                            "LlamaParse returned an invalid job identifier",
                        )
                    identifier.encode("utf-8", errors="strict")
                    self._remember_job(
                        {**self._pending_job, "state": "polling", "id": identifier},
                    )
                    job_id = quote(identifier, safe="")
                deadline = time.monotonic() + JOB_TIMEOUT
                while time.monotonic() < deadline:
                    response = self._send(
                        client,
                        "GET",
                        f"/api/v2/parse/{job_id}",
                        params={"expand": "markdown,text,items"},
                    )
                    response.raise_for_status()
                    result = response.json()
                    status = result.get("job", {}).get("status")
                    if status == "COMPLETED":
                        return result
                    if status in ("FAILED", "CANCELLED", "PARTIAL_SUCCESS"):
                        self._remember_job({**self._pending_job, "state": "terminal"})
                        raise RemoteOCRUnavailable(
                            "LlamaParse did not complete every page",
                        )
                    time.sleep(POLL_INTERVAL)
                raise RemoteOCRUnavailable(
                    "LlamaParse OCR exceeded its processing deadline",
                )
            except httpx.HTTPStatusError as error:
                code = error.response.status_code
                if self._pending_job and self._pending_job.get("id"):
                    if code in (404, 410):
                        self._remember_job({**self._pending_job, "state": "terminal"})
                elif 400 <= code < 500:
                    self._pending_job = None
                    callback = getattr(self, "_job_state_callback", None)
                    if callback is not None:
                        callback(None)
                raise RemoteOCRUnavailable(
                    f"LlamaParse request failed (HTTP {error.response.status_code})",
                    reason="quota"
                    if code == 402
                    else (
                        "authentication"
                        if code in (401, 403)
                        else (
                            "rate_limit"
                            if code == 429
                            else ("server_error" if code >= 500 else "provider")
                        )
                    ),
                ) from error
            except httpx.HTTPError as error:
                if (
                    isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
                    and self._pending_job
                    and not self._pending_job.get("id")
                ):
                    self._pending_job = None
                    callback = getattr(self, "_job_state_callback", None)
                    if callback is not None:
                        callback(None)
                raise RemoteOCRUnavailable(
                    "LlamaParse could not be reached",
                    reason="unavailable",
                ) from error
            except (AttributeError, ValueError, KeyError, TypeError) as error:
                raise RemoteOCRUnavailable(
                    "LlamaParse returned an invalid response",
                ) from error
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
    def _send(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(REQUEST_ATTEMPTS):
            if "files" in kwargs:
                for _, handle, _ in kwargs["files"].values():
                    handle.seek(0)
            try:
                response = client.request(method, path, **kwargs)
            except httpx.HTTPError as error:
                safe = isinstance(
                    error,
                    (httpx.ConnectError, httpx.ConnectTimeout),
                ) or (method == "GET" and isinstance(error, httpx.TransportError))
                if not safe or attempt == REQUEST_ATTEMPTS - 1:
                    raise
            else:
                # Job creation is paid and has no idempotency key. A server
                # error may arrive after creation; only poll requests can be
                # replayed safely after an ambiguous response.
                transient = method == "GET" and (
                    response.status_code == 429 or response.status_code >= 500
                )
                if not transient or attempt == REQUEST_ATTEMPTS - 1:
                    return response
                response.close()
            time.sleep(min(2**attempt, 8))
        raise AssertionError("Remote OCR request attempts exhausted without a result")

    @staticmethod
    def _validated_pages(result: dict, page_count: int) -> list[dict]:
        """Check completeness before accepting text or emitting a searchable PDF."""

        def contains_invalid_value(value):
            if isinstance(value, str):
                try:
                    value.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    return True
                return "\0" in value
            if isinstance(value, float):
                return not math.isfinite(value)
            if isinstance(value, dict):
                return any(
                    contains_invalid_value(key) or contains_invalid_value(item)
                    for key, item in value.items()
                )
            if isinstance(value, list):
                return any(contains_invalid_value(item) for item in value)
            return False

        if contains_invalid_value(result):
            raise ParseError("LlamaParse returned invalid provider data")
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
            try:
                valid_dimensions = all(
                    type(page.get(key)) in (int, float)
                    and math.isfinite(page[key])
                    and page[key] > 0
                    for key in ("page_width", "page_height")
                )
            except OverflowError:
                valid_dimensions = False
            if (
                type(number) is not int
                or number in indexed
                or not 1 <= number <= page_count
                or page.get("success") is False
                or not valid_dimensions
            ):
                raise ParseError("LlamaParse returned invalid or duplicate page data")
            if number not in text_pages and number not in markdown_pages:
                raise ParseError("LlamaParse did not return text for every source page")
            text = text_pages.get(number) or markdown_pages.get(number) or ""
            if not isinstance(text, str):
                raise ParseError("LlamaParse returned invalid page text")
            plain = text_pages.get(number, "")
            markdown = markdown_pages.get(number, "")
            if not isinstance(plain, str) or not isinstance(markdown, str):
                raise ParseError("LlamaParse returned invalid page text")
            indexed[number] = {
                **page,
                "text": text.strip(),
                "plain_text": plain,
                "markdown": markdown,
            }
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
