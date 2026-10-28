"""Explicit, bounded enrichment of stored pages; never rewrite PDF files."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pikepdf
from django.conf import settings
from django.db import transaction
from filelock import FileLock

from documents.models import Document
from documents.models import DocumentPage
from documents.page_data import backfill_document
from documents.page_data import checksum
from documents.page_data import parser_pages
from documents.page_data import stored_pages
from documents.utils import compute_checksum
from paperless.config import RemoteOCRConfig
from paperless.parsers.llamaparse import LlamaParseDocumentParser


def missing_rich_pages(document: Document) -> int:
    pages = stored_pages(document)
    if pages is None:
        if document.page_count:
            return document.page_count
        with pikepdf.open(document.archive_path or document.source_path) as pdf:
            return len(pdf.pages)
    return sum(page["layout"] is None or page["markdown"] is None for page in pages)


def enrich_document(document_id: int, *, max_pages: int) -> int:
    """Save each paid page immediately, so a later failure never repeats successes."""
    if max_pages < 1:
        raise ValueError("Enrichment requires a positive page limit.")
    config = RemoteOCRConfig()
    if config.remote_ocr_engine != "llamaparse" or not config.remote_ocr_api_key:
        raise ValueError(
            "Configure the LlamaParse engine and API key before enrichment.",
        )
    backfill_document(document_id)
    document = Document.objects.get(pk=document_id)
    root_id = document.root_document_id or document.pk
    completed = 0
    with FileLock(settings.DATA_DIR / f"document-file-{root_id}.lock"):
        document.refresh_from_db()
        expected_checksum = checksum(document)
        source = document.archive_path or document.source_path
        with tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR) as temporary:
            subset = Path(temporary) / "page.pdf"
            for page in document.parsed_pages.order_by("page"):
                if completed >= max_pages:
                    break
                if page.file_checksum != expected_checksum:
                    raise ValueError("Page data belongs to a different file revision.")
                if page.layout is not None and page.markdown is not None:
                    continue
                with FileLock(settings.MEDIA_LOCK):
                    if compute_checksum(source) != expected_checksum:
                        raise ValueError("Stored file checksum changed.")
                    with pikepdf.open(source) as original, pikepdf.new() as output:
                        output.pages.append(original.pages[page.page - 1])
                        output.remove_unreferenced_resources()
                        output.save(subset)
                # No overlay: the existing searchable PDF stays byte-for-byte intact.
                with LlamaParseDocumentParser() as parser:
                    result = parser._request_result(subset, config)
                    parser._review_pages = parser._validated_pages(result, 1)
                    parser.page_provenance = {
                        "engine": "llamaparse",
                        "tier": "agentic",
                        "version": "latest",
                        "job_id": result.get("job", {}).get("id"),
                        "job_page": 1,
                    }
                    evidence = parser_pages(parser, subset)[0]
                with transaction.atomic(), FileLock(settings.MEDIA_LOCK):
                    current = Document.objects.select_for_update().get(pk=document_id)
                    if (
                        checksum(current) != expected_checksum
                        or compute_checksum(source) != expected_checksum
                    ):
                        raise ValueError(
                            "Document changed during enrichment; result not attached.",
                        )
                    # Origin tracks the physical page through later splits/reorders.
                    evidence["origin"] = page.origin
                    evidence["source_page"] = page.source_page
                    evidence["rotation"] = page.rotation
                    evidence["provenance"]["layout_rotation"] = page.rotation
                    DocumentPage.objects.filter(
                        pk=page.pk,
                        file_checksum=expected_checksum,
                    ).update(**evidence)
                completed += 1
    return completed
