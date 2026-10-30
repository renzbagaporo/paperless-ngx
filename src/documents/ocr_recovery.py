"""Durable, coalesced retry requests and restart reconciliation for saved OCR."""

from __future__ import annotations

import copy
import logging
import tempfile
from collections import defaultdict
from collections import deque
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from filelock import FileLock
from filelock import Timeout

from documents.models import Document
from documents.models import DocumentPage
from documents.models import Note
from documents.models import Tag
from documents.ocr_status import status_q
from documents.page_data import checksum
from documents.page_data import stored_pages
from documents.utils import compute_checksum
from documents.versioning import get_latest_version_for_root
from documents.versioning import root_document_ids

logger = logging.getLogger("paperless.ocr_recovery")
RETRY = "ocr_retry"
MAX_ATTEMPTS = 5


def due(state: dict) -> bool:
    value = parse_datetime(state["not_before"]) if state.get("not_before") else None
    return value is None or value <= timezone.now()


def store_requests(document: Document, pages: list[dict]) -> None:
    records = list(
        DocumentPage.global_objects.filter(
            document=document,
            file_checksum=checksum(document),
        ).order_by("page"),
    )
    if len(records) != len(pages):
        return
    changed = []
    for record in records:
        state = pages[record.page - 1]["provenance"].get(RETRY)
        if state is not None:
            record.provenance[RETRY] = copy.deepcopy(state)
            job = pages[record.page - 1]["provenance"].get("ocr_job")
            if job is None:
                record.provenance.pop("ocr_job", None)
            else:
                record.provenance["ocr_job"] = copy.deepcopy(job)
            changed.append(record)
    if changed:
        DocumentPage.global_objects.bulk_update(changed, ["provenance"])


def materialize_legacy_pending(document: Document) -> bool:
    from documents.ocr_queue import defer_existing_ocr
    from documents.ocr_queue import pending_pages

    pages = stored_pages(document)
    if pending_pages(document):
        return True
    if len("".join(document.content.split())) >= 10 or (
        pages
        and any(
            page["provenance"].get("engine") == "llamaparse"
            or page["provenance"].get("ocr_status") in ("ok", "skip")
            for page in pages
        )
    ):
        return False
    if document.mime_type != "application/pdf" and not document.mime_type.startswith(
        "image/",
    ):
        return False
    source = document.archive_path or document.source_path
    if not source.is_file():
        return False
    if compute_checksum(source) != checksum(document):
        raise ValueError("Stored legacy file checksum changed.")
    with tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR) as temporary:
        rendition = None
        if document.mime_type != "application/pdf" and document.archive_path is None:
            import img2pdf

            rendition = Path(temporary) / "legacy.pdf"
            rendition.write_bytes(img2pdf.convert(str(source)))
        defer_existing_ocr(
            document,
            {"status": "pending", "reason": "legacy"},
            rendition,
        )
    return True


def dispatch(document_id: int) -> None:
    from documents.tasks import update_document_content_maybe_archive_file

    root = Document.objects.filter(pk=document_id).first()
    if root is None:
        return
    current = get_latest_version_for_root(root)
    pages = stored_pages(current) or []
    if not any(
        page["provenance"].get(RETRY, {}).get("requested")
        and due(page["provenance"][RETRY])
        for page in pages
    ):
        return
    try:
        update_document_content_maybe_archive_file.delay(
            document_id=document_id,
            remote_ocr=True,
            pending_only=True,
        )
    except Exception:
        logger.warning("Saved OCR retry awaits broker recovery")
        return
    try:
        with FileLock(settings.DATA_DIR / f"document-file-{root.pk}.lock", timeout=0):
            root.refresh_from_db()
            current = get_latest_version_for_root(root)
            fresh = stored_pages(current) or []
            for page in fresh:
                state = page["provenance"].get(RETRY, {})
                if state.get("requested") and due(state):
                    state["sent_at"] = timezone.now().isoformat()
            store_requests(current, fresh)
    except Timeout:
        return  # An active worker owns the state; it needs no delivery stamp.


def request_retry(document_id: int) -> None:
    root = Document.objects.filter(pk=document_id, root_document__isnull=True).first()
    if root is None or root.tags.filter(status_q(("ocr-fail",))).exists():
        return
    try:
        with FileLock(settings.DATA_DIR / f"document-file-{root.pk}.lock", timeout=0):
            current = get_latest_version_for_root(root)
            if not materialize_legacy_pending(current):
                return
            pages = stored_pages(current)
            authorize_new_jobs(root, pages)
            for page in pages:
                if page["provenance"].get("ocr_status") == "pending":
                    page["provenance"][RETRY] = {
                        "requested": True,
                        "running": False,
                        "attempts": 0,
                        "sent_at": None,
                    }
            store_requests(current, pages)
    except Timeout:
        return  # An active attempt already covers this request.
    transaction.on_commit(lambda: dispatch(root.pk), robust=True)


def claim(document: Document, pages: list[dict]) -> list[int]:
    """Select due pages; an attempt is counted only when that page starts."""
    return [
        index
        for index, page in enumerate(pages)
        if page["provenance"].get("ocr_status") == "pending"
        and page["provenance"].get(RETRY, {}).get("requested")
        and due(page["provenance"][RETRY])
        and page["provenance"][RETRY].get("attempts", 0) < MAX_ATTEMPTS
    ]


def start_direct(document: Document, pages: list[dict], *, bounded: bool) -> None:
    """Persist explicit intent before submitting any paid page."""
    authorize_new_jobs(document.root_document or document, pages)
    for page in pages:
        if page["provenance"].get("ocr_status") == "pending":
            page["provenance"][RETRY] = {
                "requested": not bounded,
                "running": False,
                "attempts": 0,
                "resume": not bounded,
                "sent_at": None,
            }
    store_requests(document, pages)


def authorize_new_jobs(root: Document, pages: list[dict]) -> None:
    """Only a new explicit request may replace an unusable paid submission."""
    restarted = False
    for page in pages:
        job = page["provenance"].get("ocr_job")
        if (
            page["provenance"].get("ocr_status") == "pending"
            and job
            and (not job.get("id") or job.get("state") == "terminal")
        ):
            page["provenance"].pop("ocr_job", None)
            restarted = True
    if restarted:
        Note.objects.get_or_create(
            document=root,
            note="An explicit OCR retry authorized a new paid submission for a previous ambiguous or unusable job. Review the provider if the earlier job may still be chargeable.",
        )


def finish(
    document: Document,
    *,
    claimed_only: bool = False,
    indices: list[int] | None = None,
) -> None:
    pages = stored_pages(document) or []
    for index, page in enumerate(pages):
        if indices is not None and index not in indices:
            continue
        state = page["provenance"].get(RETRY)
        if state and (not claimed_only or state.get("running")):
            state.update(requested=False, running=False)
    store_requests(document, pages)


def reconcile() -> int:
    from documents.deferred_ingestion import recover_interrupted
    from documents.file_cleanup import reconcile_cleanup
    from documents.ocr_queue import after_page_commit
    from documents.ocr_queue import pending_pages
    from documents.ocr_queue import sync_ocr_tags

    reconcile_cleanup()
    candidates = DocumentPage.objects.filter(
        Q(provenance__ocr_status="pending")
        | Q(provenance__llm_index_pending=True)
        | Q(provenance__deferred_ingestion__state__in=("pending", "running"))
        | Q(provenance__deferred_file_update__state__in=("pending", "running")),
    ).values_list("document_id", flat=True)
    processing = Document.objects.filter(
        tags__in=Tag.objects.filter(status_q(("ocr-processing",))),
    ).values_list("pk", flat=True)
    roots = (
        Document.objects.filter(root_document__isnull=True)
        .filter(
            Q(pk__in=root_document_ids(candidates)) | Q(pk__in=processing),
        )
        .distinct()
        .order_by("pk")
    )
    queued = []
    for root in roots.iterator(chunk_size=100):
        try:
            with FileLock(
                settings.DATA_DIR / f"document-file-{root.pk}.lock",
                timeout=0,
            ):
                with FileLock(
                    settings.DATA_DIR / f"document-ingestion-{root.pk}.lock",
                    timeout=0,
                ):
                    recover_interrupted(root)
                current = get_latest_version_for_root(root)
                pending = pending_pages(current)
                was_processing = root.tags.filter(
                    status_q(("ocr-processing",)),
                ).exists()
                if (
                    was_processing
                    and not pending
                    and materialize_legacy_pending(current)
                ):
                    pending = pending_pages(current)
                    for page in pending:
                        page["provenance"][RETRY] = {
                            "requested": True,
                            "running": False,
                            "attempts": 0,
                            "sent_at": None,
                        }
                for page in pending:
                    state = page["provenance"].get(RETRY, {})
                    if state.get("running"):
                        job = page["provenance"].get("ocr_job")
                        ambiguous = bool(
                            job
                            and (not job.get("id") or job.get("state") == "terminal"),
                        )
                        state.update(
                            requested=state.get("resume", True)
                            and not ambiguous
                            and state.get("attempts", 0) < MAX_ATTEMPTS,
                            running=False,
                            sent_at=None,
                            not_before=(
                                timezone.now()
                                + timedelta(
                                    seconds=min(
                                        60 * 2 ** max(state.get("attempts", 1) - 1, 0),
                                        900,
                                    ),
                                )
                            ).isoformat(),
                        )
                        if not state["requested"]:
                            Note.objects.get_or_create(
                                document=root,
                                note="OCR restart recovery requires a new explicit retry because the attempt was bounded or its restart limit was reached. The original and completed pages are saved; review the provider before retrying.",
                            )
                        if ambiguous:
                            Note.objects.get_or_create(
                                document=root,
                                note="Interrupted OCR submission needs review. Its paid job may already exist, so automatic recovery did not create another job. Reprocess or remove ocr-fail only when you explicitly want a new submission.",
                            )
                store_requests(current, pending_pages_as_all(current, pending))
                if was_processing:
                    root.tags.remove(*root.tags.filter(status_q(("ocr-processing",))))
                ready = False
                for page in pending:
                    state = page["provenance"].get(RETRY, {})
                    sent = (
                        parse_datetime(state["sent_at"])
                        if state.get("sent_at")
                        else None
                    )
                    if (
                        state.get("requested")
                        and due(state)
                        and (
                            sent is None or sent < timezone.now() - timedelta(minutes=5)
                        )
                    ):
                        ready = True
                if ready:
                    queued.append(root.pk)
                elif pending:
                    sync_ocr_tags(current.pk)
                else:
                    after_page_commit(current.pk)
                    from documents.file_updates import _publish

                    _publish(root.pk, [], workflow_events=False)
        except Timeout:
            continue
        except Exception:
            logger.warning(
                "OCR reconciliation requires review for document %s; continuing with other documents",
                root.pk,
            )
            continue
    for document_id in queued:
        dispatch(document_id)
    return len(queued)


def pending_pages_as_all(document, pending):
    pages = stored_pages(document) or []
    by_origin = defaultdict(deque)
    for page in pending:
        by_origin[(page["origin"], page["source_page"])].append(page)
    for page in pages:
        if page["provenance"].get("ocr_status") != "pending":
            continue
        matches = by_origin.get((page["origin"], page["source_page"]))
        match = matches.popleft() if matches else None
        if match and RETRY in match["provenance"]:
            page["provenance"][RETRY] = match["provenance"][RETRY]
    return pages
