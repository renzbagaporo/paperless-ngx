"""Durable, page-local OCR work on saved files, independent of broker delivery.

Pending markers travel with existing page evidence through append, page edits,
split, export and import. Retrying never uploads pages that already succeeded.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
from pathlib import Path

import pikepdf
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock

from documents.ingestion_checks import IngestionChecks
from documents.ingestion_checks import apply_ingestion_checks
from documents.ingestion_checks import collect_ingestion_checks
from documents.ingestion_checks import suspicious_number_pages
from documents.models import Document
from documents.models import Note
from documents.ocr_status import status_q
from documents.ocr_status import status_tag
from documents.page_data import checksum
from documents.page_data import has_manual_content
from documents.page_data import joined_text
from documents.page_data import parser_pages
from documents.page_data import pdf_pages
from documents.page_data import preserve_page_state
from documents.page_data import save_pages
from documents.page_data import stored_pages
from documents.utils import compute_checksum
from documents.versioning import get_latest_version_for_root
from paperless.config import OcrConfig
from paperless.config import RemoteOCRConfig
from paperless.models import ArchiveFileGenerationChoices
from paperless.parsers.llamaparse import LlamaParseDocumentParser

STATUS_TAGS = ("ocr-pending", "ocr-fail", "ocr-processing", "ocr-ok", "ocr-skip")


def after_page_commit(document_id: int) -> None:
    from documents.deferred_ingestion import complete
    from documents.deferred_ingestion import complete_file_event
    from documents.ocr_index import queue_complete_index

    sync_ocr_tags(document_id)
    complete(document_id)
    complete_file_event(document_id)
    queue_complete_index(document_id)


def defer_existing_ocr(
    document: Document,
    status: dict,
    rendition: Path | None = None,
) -> None:
    """Retain previous OCR and files when an explicit reprocess cannot finish."""
    with FileLock(settings.MEDIA_LOCK):
        document.refresh_from_db()
        pages = stored_pages(document) or pdf_pages(
            document.archive_path or rendition or document.source_path,
        )
    for index, page in enumerate(pages, 1):
        page["provenance"].update(ocr_status="pending", reason=status["reason"])
        if status.get("job"):
            page["provenance"]["ocr_job"] = LlamaParseDocumentParser.job_for_page(
                status["job"],
                index,
            )
        else:
            page["provenance"].pop("ocr_job", None)
    with transaction.atomic():
        Document.objects.filter(pk=document.pk).update(page_count=len(pages))
        document.refresh_from_db()
        save_pages(document, pages)


def pending_pages(document: Document) -> list[dict]:
    return [
        page
        for page in (stored_pages(document) or [])
        if page["provenance"].get("ocr_status") == "pending"
    ]


def enqueue_pending_ocr(document_ids) -> None:
    from documents.ocr_recovery import request_retry

    for document_id in document_ids:
        request_retry(document_id)


def sync_ocr_tags(document_id: int) -> None:
    """Run after file commit and media-lock release; tag signals can rename files."""
    document = Document.objects.filter(pk=document_id).first()
    if document is None:
        return
    root = document.root_document or document
    current = get_latest_version_for_root(root)
    if current.pk != document.pk:
        return
    pages = stored_pages(current) or []
    pending = [p for p in pages if p["provenance"].get("ocr_status") == "pending"]
    if pending:
        wanted = {"ocr-pending", "ocr-fail"}
        reasons = sorted({p["provenance"].get("reason", "provider") for p in pending})
        Note.objects.get_or_create(
            document=root,
            note="Remote OCR pending: "
            + ", ".join(reasons)
            + ". The original is saved. Remove ocr-fail or reprocess to retry.",
        )
    elif any(p["provenance"].get("engine") == "llamaparse" for p in pages):
        wanted = {"ocr-ok"}
    elif pages and (
        all(p["provenance"].get("ocr_status") == "skip" for p in pages)
        or root.tags.filter(
            status_q(("ocr-pending", "ocr-fail", "ocr-processing")),
        ).exists()
    ):
        wanted = {"ocr-skip"}
    else:
        return
    selected = [status_tag(name) for name in wanted]
    keep_ids = [tag.pk for tag in selected]
    if pending:
        keep_ids.extend(
            root.tags.filter(status_q(("ocr-processing",))).values_list(
                "pk",
                flat=True,
            ),
        )
    for tag in selected:
        root.tags.add(tag)
    remove = root.tags.filter(status_q(STATUS_TAGS)).exclude(pk__in=keep_ids)
    root.tags.remove(*remove)


def _retry_content(current: Document, pages: list[dict], previous: list[dict]):
    manual = has_manual_content(current, previous)
    content = joined_text(pages)
    if manual:
        new_text = [
            page["text"]
            for page, old in zip(pages, previous, strict=True)
            if old["provenance"].get("ocr_status") == "pending"
            and page["provenance"].get("ocr_status") != "pending"
            and page["text"]
        ]
        content = "\n\n".join(value for value in (current.content, *new_text) if value)
    return content, manual


def _install_text_page(
    document: Document,
    pages: list[dict],
    checks: IngestionChecks,
) -> None:
    """Commit completed OCR without changing any stored document rendition."""
    from documents.file_updates import _publish

    root_id = document.root_document_id or document.pk
    original_checksum = checksum(document)
    with transaction.atomic():
        current = Document.objects.select_for_update().get(pk=document.pk)
        if checksum(current) != original_checksum:
            raise ValueError("Document changed during OCR; result was not installed.")
        with FileLock(settings.MEDIA_LOCK):
            if (
                compute_checksum(current.archive_path or current.source_path)
                != original_checksum
            ):
                raise ValueError(
                    "Stored file changed during OCR; result not installed.",
                )
        previous = stored_pages(current) or []
        content, manual = _retry_content(current, pages, previous)
        Document.objects.filter(pk=current.pk).update(
            content=content,
            page_count=len(pages),
            modified=timezone.now(),
        )
        current.refresh_from_db()
        apply_ingestion_checks(Document.objects.get(pk=root_id), checks)
        save_pages(
            current,
            pages,
            automatic_content=joined_text(pages),
            manual_content=manual,
            index_pending=True,
        )
        transaction.on_commit(
            lambda: _publish(root_id, [], workflow_events=False),
            robust=True,
        )
    document.refresh_from_db(from_queryset=Document.global_objects.all())


def _install_page(
    document: Document,
    output: Path,
    pages: list[dict],
    thumbnail: Path,
    checks: IngestionChecks | None = None,
    *,
    reprocess_content: str | None = None,
    expected_content: str | None = None,
    expected_source_checksum: str | None = None,
) -> None:
    """Replace only this revision's bytes, preserving identity and file history."""
    from documents.file_handling import generate_unique_filename
    from documents.file_handling import validate_path_in_root
    from documents.file_updates import RollbackFile
    from documents.file_updates import _atomic_file_update
    from documents.file_updates import _cleanup_after_commit
    from documents.file_updates import _publish

    root_id = document.root_document_id or document.pk
    original_checksum = checksum(document)
    with _atomic_file_update() as rollback:
        current = Document.objects.select_for_update().get(pk=document.pk)
        if checksum(current) != original_checksum:
            raise ValueError("Document changed during OCR; result was not installed.")
        previous = stored_pages(current) or []
        if reprocess_content is not None:
            manual = current.content != expected_content
            content = current.content if manual else reprocess_content
        else:
            content, manual = _retry_content(current, pages, previous)
        policies = {
            page["provenance"]["store_only_current_file"]
            for page in previous
            if type(page["provenance"].get("store_only_current_file")) is bool
        }
        single = (
            policies.pop()
            if len(policies) == 1
            else bool(
                settings.STORE_ONLY_CURRENT_FILE and current.root_document_id is None,
            )
        )
        for page in pages:
            page["provenance"]["store_only_current_file"] = single
        existing_source = current.archive_path or current.source_path
        original_filename = current.original_filename
        if single and current.mime_type != "application/pdf" and original_filename:
            original_filename = str(Path(original_filename).with_suffix(".pdf"))
        old_paths = [
            p
            for p in (
                current.source_path if single else None,
                current.archive_path,
            )
            if p is not None
        ]
        if single:
            current.filename = None
            current.mime_type = "application/pdf"
            directory = settings.ORIGINALS_DIR
        else:
            current.archive_filename = None
            directory = settings.ARCHIVE_DIR
        thumb = current.thumbnail_path
        with FileLock(settings.MEDIA_LOCK):
            destination = directory / generate_unique_filename(
                current,
                archive_filename=not single,
            )
            from documents.file_updates import available_destination

            destination = available_destination(
                current,
                directory,
                candidate=destination,
            )
            validate_path_in_root(destination, directory)
            destination.parent.mkdir(parents=True, exist_ok=True)
            entry = RollbackFile(
                destination,
                thumb,
                thumb.read_bytes() if thumb.exists() else None,
                current.pk,
            )
            rollback.append(entry)
            if compute_checksum(existing_source) != (
                expected_source_checksum or original_checksum
            ):
                raise ValueError(
                    "Stored file changed during OCR; result not installed.",
                )
            shutil.copyfile(output, destination)
            entry.checksum = compute_checksum(destination)
            thumb.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=thumb.parent, delete=False) as stream:
                temporary_thumb = Path(stream.name)
                stream.write(thumbnail.read_bytes())
            temporary_thumb.replace(thumb)
            updates = {
                "content": content,
                "page_count": len(pages),
                "modified": timezone.now(),
            }
            if single:
                updates.update(
                    filename=str(destination.relative_to(directory)),
                    checksum=compute_checksum(destination),
                    archive_filename=None,
                    archive_checksum=None,
                    mime_type="application/pdf",
                    original_filename=original_filename,
                )
            else:
                updates.update(
                    archive_filename=str(destination.relative_to(directory)),
                    archive_checksum=compute_checksum(destination),
                )
            Document.objects.filter(pk=current.pk).update(**updates)
            current.refresh_from_db()
        from documents.file_cleanup import record_cleanup

        cleanup = record_cleanup(root_id, old_paths, [])
        transaction.on_commit(
            lambda: _cleanup_after_commit(
                old_paths,
                [],
                job_id=cleanup.pk if cleanup else None,
            ),
            robust=True,
        )
        if checks is not None:
            apply_ingestion_checks(Document.objects.get(pk=root_id), checks)
        save_pages(
            current,
            pages,
            automatic_content=reprocess_content
            if reprocess_content is not None
            else joined_text(pages),
            manual_content=manual,
            index_pending=True,
        )
        transaction.on_commit(
            lambda: _publish(root_id, [], workflow_events=False),
            robust=True,
        )
    document.refresh_from_db(from_queryset=Document.global_objects.all())


def retry_pending_ocr(
    document_id: int,
    *,
    max_pages: int | None = None,
    requested_only: bool = False,
) -> int:
    """Explicit paid retry; preserve each successful page before attempting another."""
    if max_pages is not None and max_pages < 1:
        raise ValueError("OCR page limit must be positive.")
    document = Document.objects.get(pk=document_id)
    root = document.root_document or document
    completed = 0
    with FileLock(settings.DATA_DIR / f"document-file-{root.pk}.lock"):
        document = (
            get_latest_version_for_root(root)
            if document.pk == root.pk
            else Document.objects.get(pk=document_id)
        )
        pages = stored_pages(document)
        if pages is None or not pending_pages(document):
            return 0
        text_only = (
            OcrConfig().archive_file_generation == ArchiveFileGenerationChoices.NEVER
        )
        from documents.ocr_recovery import RETRY
        from documents.ocr_recovery import claim
        from documents.ocr_recovery import finish
        from documents.ocr_recovery import start_direct
        from documents.ocr_recovery import store_requests

        if RemoteOCRConfig().remote_ocr_engine != "llamaparse":
            if requested_only:
                finish(document)
                Note.objects.get_or_create(
                    document=root,
                    note="Saved LlamaParse OCR was not submitted because the configured engine changed. Choose the desired engine explicitly when reprocessing.",
                )
                sync_ocr_tags(document.pk)
                return 0
            raise ValueError(
                "Configure the LlamaParse engine before retrying its saved OCR work.",
            )
        selected = claim(document, pages) if requested_only else None
        if requested_only and not selected:
            return 0
        if not requested_only:
            start_direct(document, pages, bounded=max_pages is not None)
        processing = status_tag("ocr-processing")
        root.tags.add(processing)
        try:
            with tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR) as temporary:
                work = Path(temporary)
                for index, page in enumerate(pages):
                    if max_pages is not None and completed >= max_pages:
                        break
                    if page["provenance"].get("ocr_status") != "pending":
                        continue
                    if requested_only and index not in selected:
                        continue
                    state = page["provenance"][RETRY]
                    state.update(
                        requested=False,
                        running=True,
                        attempts=state.get("attempts", 0) + 1,
                    )
                    store_requests(document, pages)
                    with FileLock(settings.MEDIA_LOCK):
                        document.refresh_from_db()
                        source = document.archive_path or document.source_path
                        expected_checksum = checksum(document)
                        if compute_checksum(source) != expected_checksum:
                            raise ValueError(
                                "Stored file checksum changed before OCR retry.",
                            )
                        snapshot = work / "current_file"
                        shutil.copyfile(source, snapshot)
                    if (
                        document.mime_type != "application/pdf"
                        and document.archive_path is None
                    ):
                        import img2pdf

                        converted = work / "current.pdf"
                        converted.write_bytes(img2pdf.convert(str(snapshot)))
                        snapshot = converted
                    subset = work / "pending.pdf"
                    with pikepdf.open(snapshot) as pdf, pikepdf.new() as single:
                        single.pages.append(pdf.pages[index])
                        single.remove_unreferenced_resources()
                        single.save(subset)
                    with LlamaParseDocumentParser() as parser:
                        parser._resume_job = copy.deepcopy(
                            page["provenance"].get("ocr_job"),
                        )

                        def remember_job(job):
                            from documents.models import DocumentPage

                            if job is None:
                                page["provenance"].pop("ocr_job", None)
                            else:
                                page["provenance"]["ocr_job"] = parser.job_for_page(
                                    job,
                                    1,
                                )
                            record = DocumentPage.global_objects.get(
                                document=document,
                                file_checksum=checksum(document),
                                page=index + 1,
                            )
                            if job is None:
                                record.provenance.pop("ocr_job", None)
                            else:
                                record.provenance["ocr_job"] = copy.deepcopy(
                                    page["provenance"]["ocr_job"],
                                )
                            record.save(update_fields=["provenance"])

                        parser._job_state_callback = remember_job
                        parser.parse(
                            subset,
                            "application/pdf",
                            produce_archive=True,
                            auto_rotate=not (
                                page["rotation"]
                                or page["provenance"].get("manual_rotation")
                            ),
                        )
                        with FileLock(settings.MEDIA_LOCK):
                            document.refresh_from_db()
                            current_source = (
                                document.archive_path or document.source_path
                            )
                            if (
                                checksum(document) != expected_checksum
                                or compute_checksum(current_source) != expected_checksum
                            ):
                                raise ValueError(
                                    "Stored file changed during OCR; result not installed.",
                                )
                        evidence = parser_pages(
                            parser,
                            subset
                            if text_only
                            else parser.get_archive_path() or subset,
                        )[0]
                        evidence.update(
                            {
                                key: copy.deepcopy(page[key])
                                for key in ("origin", "source_page", "rotation")
                            },
                        )
                        evidence["provenance"]["layout_rotation"] = (
                            page["rotation"]
                            + evidence["provenance"].get("layout_rotation", 0)
                        ) % 360
                        preserve_page_state(page["provenance"], evidence["provenance"])
                        if evidence["provenance"].get("ocr_status") == "pending":
                            if "ocr_job" in evidence["provenance"]:
                                page["provenance"]["ocr_job"] = evidence["provenance"][
                                    "ocr_job"
                                ]
                            page["provenance"].update(
                                ocr_status="pending",
                                reason=evidence["provenance"]["reason"],
                            )
                            with transaction.atomic():
                                save_pages(document, pages)
                            break
                        output = work / "completed.pdf"
                        with (
                            pikepdf.open(snapshot) as pdf,
                            pikepdf.open(parser.get_archive_path()) as added,
                        ):
                            pdf.pages[index] = added.pages[0]
                            pdf.save(output)
                        pages[index] = evidence
                        checks = collect_ingestion_checks(parser, joined_text(pages))
                        suspicious = (
                            suspicious_number_pages(
                                [
                                    {"plain_text": p["text"], "markdown": p["markdown"]}
                                    for p in pages
                                ],
                            )
                            if settings.OCR_REVIEW_CROSS_PAGE_NUMBERS
                            else ()
                        )
                        if text_only:
                            _install_text_page(
                                document,
                                pages,
                                IngestionChecks(
                                    date_review=checks.date_review,
                                    suspicious_pages=suspicious,
                                ),
                            )
                            completed += 1
                            continue
                        # A fresh renderer has no single-page OCR archive that
                        # could take precedence over the merged document path.
                        with LlamaParseDocumentParser() as thumbnail_parser:
                            thumbnail = thumbnail_parser.get_thumbnail(
                                output,
                                "application/pdf",
                            )
                            _install_page(
                                document,
                                output,
                                pages,
                                thumbnail,
                                IngestionChecks(
                                    date_review=checks.date_review,
                                    suspicious_pages=suspicious,
                                ),
                            )
                        completed += 1
        finally:
            finish(document, indices=selected)
            root.tags.remove(processing)
            sync_ocr_tags(document.pk)
    return completed
