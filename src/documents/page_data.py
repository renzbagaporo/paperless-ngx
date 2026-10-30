"""Persist page evidence and recover legacy PDF text without invoking OCR."""

from __future__ import annotations

import copy
import hashlib
import logging
import tempfile
import uuid
from pathlib import Path

import pikepdf
from django.conf import settings
from django.db import transaction
from filelock import FileLock

from documents.models import Document
from documents.models import DocumentPage
from documents.utils import compute_checksum
from documents.utils import run_subprocess
from paperless.parsers.utils import read_file_handle_unicode_errors

logger = logging.getLogger("paperless.page_data")
PAGE_FIELDS = (
    "origin",
    "source_page",
    "rotation",
    "text",
    "markdown",
    "layout",
    "provenance",
)
SERVER_PROVENANCE_KEYS = (
    "deferred_ingestion",
    "deferred_file_update",
    "ocr_retry",
    "manual_rotation",
    "automatic_content_hash",
    "manual_content",
    "llm_index_pending",
    "llm_index_request",
    "llm_index_ingestion",
    "store_only_current_file",
)
PRIVATE_PROVENANCE_KEYS = (
    "ocr_job",
    "deferred_ingestion",
    "deferred_file_update",
    "ocr_retry",
    "automatic_content_hash",
    "manual_content",
    "llm_index_pending",
    "llm_index_request",
    "llm_index_ingestion",
    "store_only_current_file",
)


def public_provenance(value: dict) -> dict:
    return {
        key: item for key, item in value.items() if key not in PRIVATE_PROVENANCE_KEYS
    }


def automatic_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def has_manual_content(document: Document, pages: list[dict]) -> bool:
    if any(page["provenance"].get("manual_content") for page in pages):
        return True
    hashes = {
        page["provenance"]["automatic_content_hash"]
        for page in pages
        if "automatic_content_hash" in page["provenance"]
    }
    if len(hashes) == 1:
        return automatic_content_hash(document.content) not in hashes
    if not document.content:
        return False
    return " ".join(document.content.replace("\0", " ").split()) != " ".join(
        joined_text(pages).replace("\0", " ").split(),
    )


def preserve_page_state(previous: dict, evidence: dict) -> None:
    for key in SERVER_PROVENANCE_KEYS:
        if key in previous:
            evidence[key] = copy.deepcopy(previous[key])


def checksum(document: Document) -> str:
    return document.archive_checksum or document.checksum


def pdf_pages(source: Path) -> list[dict]:
    """Keep blank pages and page boundaries; pdftotext never runs OCR."""
    with pikepdf.open(source) as pdf:
        count = len(pdf.pages)
    with tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR) as temporary:
        output = Path(temporary) / "pages.txt"
        run_subprocess(
            ["pdftotext", "-q", "-layout", "-enc", "UTF-8", str(source), str(output)],
            logger=logger,
        )
        chunks = read_file_handle_unicode_errors(output, log=logger).split("\f")
    if len(chunks) == count + 1 and not chunks[-1].strip():
        chunks.pop()
    if len(chunks) != count:
        raise ValueError("Extracted text does not match the PDF page count.")
    return [
        {
            "origin": uuid.uuid4(),
            "source_page": index,
            "rotation": 0,
            "text": text.strip(),
            "markdown": None,
            "layout": None,
            "provenance": {"engine": "pdf_text", "quality": "text_only"},
        }
        for index, text in enumerate(chunks, 1)
    ]


def _durable(value):
    """Do not retain presigned download URLs or provider file references."""
    if isinstance(value, dict):
        return {
            key: _durable(item)
            for key, item in value.items()
            if key not in {"url", "download_url", "presigned_url", "file_id"}
        }
    if isinstance(value, list):
        return [_durable(item) for item in value]
    return value


def parser_pages(parser, source: Path) -> list[dict]:
    getter = getattr(type(parser), "get_review_pages", None)
    rich = getter(parser) if getter else []
    if not rich:
        pages = pdf_pages(source)
        status_getter = getattr(type(parser), "get_ocr_status", None)
        status = status_getter(parser) if status_getter else None
        if status:
            for index, page in enumerate(pages, 1):
                page["provenance"].update(
                    ocr_status=status["status"],
                    **({"reason": status["reason"]} if "reason" in status else {}),
                )
                if status.get("job"):
                    page["provenance"]["ocr_job"] = parser.job_for_page(
                        status["job"],
                        index,
                    )
        return pages
    provenance = getattr(parser, "page_provenance", {"engine": "llamaparse"})
    with pikepdf.open(source) as pdf:
        if len(rich) != len(pdf.pages):
            raise ValueError("Parsed page count does not match the PDF.")
    rotation_getter = getattr(type(parser), "get_layout_rotation_deltas", None)
    rotations = rotation_getter(parser, source) if rotation_getter else None
    if rotations is not None and len(rotations) != len(rich):
        raise ValueError("Layout orientation count does not match the PDF.")
    return [
        {
            "origin": uuid.uuid4(),
            "source_page": page["page_number"],
            "rotation": 0,
            "text": page["text"],
            "markdown": page["markdown"],
            "layout": _durable(page),
            "provenance": {
                **copy.deepcopy(provenance),
                **(
                    {"layout_rotation": rotations[index] % 360}
                    if rotations is not None
                    else {}
                ),
            },
        }
        for index, page in enumerate(rich)
    ]


def stored_pages(document: Document) -> list[dict] | None:
    pages = list(
        DocumentPage.global_objects.filter(
            document=document,
            file_checksum=checksum(document),
        ),
    )
    if not pages or (document.page_count and len(pages) != document.page_count):
        return None
    if [page.page for page in pages] != list(range(1, len(pages) + 1)):
        return None
    return [
        {field: copy.deepcopy(getattr(page, field)) for field in PAGE_FIELDS}
        for page in pages
    ]


def pages_for_file(document: Document, source: Path) -> list[dict]:
    return stored_pages(document) or pdf_pages(source)


def save_pages(
    document: Document,
    pages: list[dict],
    *,
    file_event: bool = False,
    automatic_content: str | None = None,
    manual_content: bool | None = None,
    index_pending: bool = False,
) -> None:
    """Call in the same transaction as the file revision update."""
    from documents.deferred_ingestion import carry_context
    from documents.deferred_ingestion import prepare_file_event

    carry_context(document, pages)
    prepare_file_event(document, pages, file_event=file_event)
    if index_pending:
        for page in pages:
            page["provenance"]["llm_index_pending"] = True
    if automatic_content is not None:
        for page in pages:
            page["provenance"]["automatic_content_hash"] = automatic_content_hash(
                automatic_content,
            )
    if manual_content is not None:
        for page in pages:
            page["provenance"].pop("manual_content", None)
            if manual_content:
                page["provenance"]["manual_content"] = True
    DocumentPage.global_objects.filter(document=document).hard_delete()
    records = DocumentPage.objects.bulk_create(
        [
            DocumentPage(
                document=document,
                deleted_at=document.deleted_at,
                restored_at=document.restored_at,
                transaction_id=document.transaction_id,
                page=index,
                file_checksum=checksum(document),
                **page,
            )
            for index, page in enumerate(pages, 1)
        ],
    )
    from documents.ocr_queue import after_page_commit

    generation = records[0].pk if records else None
    document_id = document.pk

    def committed_page_state():
        # Publish lifecycle hooks for the surviving saved page generation.
        # Several OCR checkpoints can share a surrounding SQL transaction.
        if (
            generation is None
            or DocumentPage.global_objects.filter(
                pk=generation,
                document_id=document_id,
            ).exists()
        ):
            after_page_commit(document_id)

    transaction.on_commit(committed_page_state, robust=True)


def selected_pages(pages: list[dict], operations: list[dict]) -> list[dict]:
    result = []
    for operation in operations:
        page = copy.deepcopy(pages[operation["page"] - 1])
        page["rotation"] = (page["rotation"] + operation.get("rotate", 0)) % 360
        if operation.get("rotate", 0):
            page["provenance"]["manual_rotation"] = True
        result.append(page)
    return result


def joined_text(pages: list[dict]) -> str:
    return "\n\n".join(page["text"] for page in pages if page["text"])


def backfill_document(document_id: int) -> bool:
    document = Document.global_objects.get(pk=document_id)
    root_id = document.root_document_id or document.pk
    with (
        FileLock(settings.DATA_DIR / f"document-file-{root_id}.lock"),
        transaction.atomic(),
    ):
        document = Document.global_objects.select_for_update().get(pk=document_id)
        if stored_pages(document) is not None:
            return False
        source = document.archive_path or document.source_path
        if not document.has_archive_version and document.mime_type != "application/pdf":
            raise ValueError("Document has no PDF from which to recover page text.")
        with FileLock(settings.MEDIA_LOCK):
            if compute_checksum(source) != checksum(document):
                raise ValueError("Stored file checksum does not match the document.")
            pages = pdf_pages(source)
            if document.page_count and len(pages) != document.page_count:
                raise ValueError("Stored file page count does not match the document.")
            save_pages(document, pages)
        return True
