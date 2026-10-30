"""Durable, coalesced indexing of complete page-backed document revisions."""

import copy
import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
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
from documents.page_data import automatic_content_hash
from documents.page_data import checksum
from documents.page_data import stored_pages
from documents.versioning import get_latest_version_for_root
from paperless.config import AIConfig

logger = logging.getLogger("paperless.ocr_index")
PENDING = "llm_index_pending"
REQUEST = "llm_index_request"
INGESTION = "llm_index_ingestion"
MAX_ATTEMPTS = 5


class StaleIndexRequest(RuntimeError):
    """A changed or removed document no longer authorizes an index write."""


@contextmanager
def index_write_guard(root_id, token, expected):
    from documents.ocr_queue import pending_pages

    with (
        FileLock(settings.DATA_DIR / f"document-file-{root_id}.lock", timeout=5),
        FileLock(settings.DATA_DIR / f"document-index-{root_id}.lock"),
        transaction.atomic(),
    ):
        root = (
            Document.objects.select_for_update()
            .filter(pk=root_id, root_document__isnull=True)
            .first()
        )
        if root is None:
            raise StaleIndexRequest
        current = get_latest_version_for_root(root)
        pages = stored_pages(current) or []
        if (
            not any(
                page["provenance"].get(REQUEST, {}).get("token") == token
                for page in pages
            )
            or pending_pages(current)
            or signature(root, current) != expected
        ):
            raise StaleIndexRequest
        yield


def ingestion_lock(token):
    return FileLock(
        settings.DATA_DIR / f"document-index-ingestion-{uuid.UUID(token).hex}.lock",
        timeout=0,
    )


def discard_ingestion_lock(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove a completed ingestion coordination lock")


def stage_ingestion_index(function):
    """Publish committed page work after the consumer's final file maintenance."""

    @wraps(function)
    def run(consumer, *args, **kwargs):
        token = str(uuid.uuid4())
        lock = ingestion_lock(token)
        try:
            with lock:
                consumer._index_ingestion_token = token
                try:
                    result = function(consumer, *args, **kwargs)
                finally:
                    del consumer._index_ingestion_token
        finally:
            # Each token has one owner. A completed/failed owner never takes
            # this lock again, so leaving no empty per-ingestion files is safe.
            discard_ingestion_lock(lock.lock_file)
        root_id = consumer.input_doc.root_document_id or result["document_id"]
        transaction.on_commit(
            lambda: queue_complete_index(root_id, latest=True),
            robust=True,
        )
        return result

    return run


def ingestion_active(root_id):
    """Check all live ingestion owners before clearing their saved markers."""
    rows = list(records(root_id).filter(provenance__has_key=INGESTION))
    markers = [row.provenance[INGESTION] for row in rows]
    tokens = {
        marker["token"]
        for marker in markers
        if isinstance(marker, dict)
        and marker.get("root_id") == root_id
        and isinstance(marker.get("token"), str)
    }
    checked = []
    for token in tokens:
        try:
            lock = ingestion_lock(token)
        except (TypeError, ValueError, AttributeError):
            continue  # An imported invalid marker has no active local owner.
        try:
            with lock:
                pass
        except Timeout:
            return True
        checked.append(lock.lock_file)
    changed = []
    for row in rows:
        if INGESTION in row.provenance:
            row.provenance.pop(INGESTION)
            changed.append(row)
    if changed:
        DocumentPage.global_objects.bulk_update(changed, ["provenance"])
    for path in checked:
        discard_ingestion_lock(path)
    return False


def signature(root, current):
    from documents.deferred_ingestion import metadata_fingerprint
    from paperless_ai.embedding import build_llm_index_text
    from paperless_ai.indexing import _safe_related_name

    names = json.dumps(
        {
            "tags": list(root.tags.order_by("pk").values_list("name", flat=True)),
            **{
                field: _safe_related_name(root, field)
                for field in ("correspondent", "document_type", "storage_path")
            },
        },
        sort_keys=True,
    )
    value = f"{current.pk}:{checksum(current)}:{metadata_fingerprint(root)}:{root.filename}:{automatic_content_hash(build_llm_index_text(root))}:{names}"
    return hashlib.sha256(value.encode()).hexdigest()


def records(root_id):
    return DocumentPage.global_objects.select_for_update(of=("self",)).filter(
        Q(document_id=root_id) | Q(document__root_document_id=root_id),
    )


def retire(rows):
    changed = []
    for record in rows:
        if PENDING in record.provenance or REQUEST in record.provenance:
            record.provenance.pop(PENDING, None)
            record.provenance.pop(REQUEST, None)
            changed.append(record)
    if changed:
        DocumentPage.global_objects.bulk_update(changed, ["provenance"])


def save_request(current, state):
    rows = list(
        DocumentPage.global_objects.select_for_update().filter(
            document=current,
            file_checksum=checksum(current),
        ),
    )
    for row in rows:
        row.provenance[REQUEST] = copy.deepcopy(state)
    if rows:
        DocumentPage.global_objects.bulk_update(rows, ["provenance"])


def reset_lease(root_id, token, delay=0):
    with transaction.atomic():
        changed = []
        for row in records(root_id):
            state = row.provenance.get(REQUEST, {})
            if state.get("token") == token:
                state["lease_until"] = (
                    (timezone.now() + timedelta(seconds=delay)).isoformat()
                    if delay
                    else None
                )
                changed.append(row)
        if changed:
            DocumentPage.global_objects.bulk_update(changed, ["provenance"])


def queue_complete_index(document_id: int, *, latest: bool = False) -> None:
    from documents.ocr_queue import pending_pages
    from documents.tasks import update_document_in_llm_index

    document = Document.objects.filter(pk=document_id).first()
    if document is None:
        return
    root_id = document.root_document_id or document.pk
    delivery = None
    try:
        with (
            FileLock(settings.DATA_DIR / f"document-index-{root_id}.lock", timeout=0),
            transaction.atomic(),
        ):
            root = Document.objects.select_for_update().get(pk=root_id)
            if ingestion_active(root_id):
                return
            current = get_latest_version_for_root(root)
            if current.pk != document_id and not latest:
                retire(
                    DocumentPage.global_objects.select_for_update().filter(
                        document_id=document_id,
                    ),
                )
                return
            pages = stored_pages(current) or []
            if pending_pages(current) or not any(
                page["provenance"].get(PENDING) for page in pages
            ):
                return
            if not AIConfig().llm_index_enabled:
                retire(records(root_id))
                return
            # A live worker can take longer than the delivery lease. Its OS
            # lock prevents a second embedding job while it is still running.
            with FileLock(
                settings.DATA_DIR / f"document-index-work-{root_id}.lock",
                timeout=0,
            ):
                pass
            expected = signature(root, current)
            state = next(
                (
                    page["provenance"][REQUEST]
                    for page in pages
                    if REQUEST in page["provenance"]
                ),
                {},
            )
            if state.get("signature") != expected:
                state = {
                    "token": str(uuid.uuid4()),
                    "signature": expected,
                    "attempts": 0,
                }
            until = (
                parse_datetime(state["lease_until"])
                if state.get("lease_until")
                else None
            )
            if until and until > timezone.now():
                return
            not_before = (
                parse_datetime(state["not_before"]) if state.get("not_before") else None
            )
            if not_before and not_before > timezone.now():
                return
            if state["attempts"] >= MAX_ATTEMPTS:
                _, created = Note.objects.get_or_create(
                    document=root,
                    note="LLM indexing reached its recovery attempt limit. The document and OCR are saved. Review the embedding service and run an explicit reindex.",
                )
                if created:
                    state["signature"] = signature(root, current)
                    save_request(current, state)
                return
            state["lease_until"] = (timezone.now() + timedelta(minutes=5)).isoformat()
            save_request(current, state)
            delivery = root, state["token"]
    except (Timeout, Document.DoesNotExist):
        return
    if delivery:
        root, token = delivery
        try:
            update_document_in_llm_index.apply_async(
                kwargs={"document": root, "personal_index_token": token},
            )
        except Exception:
            reset_lease(root_id, token)
            logger.warning("Saved LLM index update awaits broker recovery")


def run_queued_index(document, token, indexer):
    from documents.ocr_queue import pending_pages

    root_id = document.root_document_id or document.pk
    try:
        with FileLock(
            settings.DATA_DIR / f"document-index-work-{root_id}.lock",
            timeout=0,
        ):
            with (
                FileLock(
                    settings.DATA_DIR / f"document-file-{root_id}.lock",
                    timeout=5,
                ),
                FileLock(settings.DATA_DIR / f"document-index-{root_id}.lock"),
                transaction.atomic(),
            ):
                root = (
                    Document.objects.select_for_update()
                    .filter(pk=root_id, root_document__isnull=True)
                    .first()
                )
                if root is None:
                    return
                current = get_latest_version_for_root(root)
                pages = stored_pages(current) or []
                state = next(
                    (
                        p["provenance"].get(REQUEST)
                        for p in pages
                        if p["provenance"].get(REQUEST, {}).get("token") == token
                    ),
                    None,
                )
                if (
                    state is None
                    or pending_pages(current)
                    or state["signature"] != signature(root, current)
                ):
                    return
                if not AIConfig().llm_index_enabled:
                    retire(records(root_id))
                    return
                if state.get("attempts", 0) >= MAX_ATTEMPTS:
                    return
                not_before = (
                    parse_datetime(state["not_before"])
                    if state.get("not_before")
                    else None
                )
                if not_before and not_before > timezone.now():
                    return
                state["attempts"] = state.get("attempts", 0) + 1
                state["not_before"] = (
                    timezone.now() + timedelta(minutes=5)
                ).isoformat()
                state["lease_until"] = (
                    timezone.now() + timedelta(minutes=5)
                ).isoformat()
                save_request(current, state)
            root._personal_index_write_guard = lambda: index_write_guard(
                root_id,
                token,
                state["signature"],
            )
            try:
                if indexer(root) is False:
                    raise RuntimeError("LLM index write was deferred")
            except StaleIndexRequest:
                reset_lease(root_id, token)
                return
            except Exception:
                delay = min(60 * 2 ** (state["attempts"] - 1), 900)
                with transaction.atomic():
                    for row in records(root_id):
                        request = row.provenance.get(REQUEST, {})
                        if request.get("token") == token:
                            request["not_before"] = (
                                timezone.now() + timedelta(seconds=delay)
                            ).isoformat()
                            row.save(update_fields=["provenance"])
                reset_lease(root_id, token, delay)
                raise
            with (
                FileLock(
                    settings.DATA_DIR / f"document-file-{root_id}.lock",
                    timeout=5,
                ),
                FileLock(settings.DATA_DIR / f"document-index-{root_id}.lock"),
                transaction.atomic(),
            ):
                root = (
                    Document.objects.select_for_update()
                    .filter(pk=root_id, root_document__isnull=True)
                    .first()
                )
                if root is None:
                    return
                current = get_latest_version_for_root(root)
                pages = stored_pages(current) or []
                owns_request = any(
                    p["provenance"].get(REQUEST, {}).get("token") == token
                    for p in pages
                )
                if (
                    owns_request
                    and not pending_pages(current)
                    and state["signature"] == signature(root, current)
                ):
                    retire(records(root_id))
                else:
                    reset_lease(root_id, token)
    except Timeout:
        return  # A duplicate delivery is covered by the live worker.


def mark_pending(document):
    """Capture metadata-only index changes before calling a batch indexer."""
    root = document.root_document or document
    current = get_latest_version_for_root(root)
    with transaction.atomic():
        rows = list(
            DocumentPage.global_objects.select_for_update().filter(
                document=current,
                file_checksum=checksum(current),
            ),
        )
        for row in rows:
            row.provenance[PENDING] = True
        if rows:
            DocumentPage.global_objects.bulk_update(rows, ["provenance"])
    return signature(root, current) if rows else None


def acknowledge_manual_index(document, expected):
    from documents.ocr_queue import pending_pages

    root_id = document.root_document_id or document.pk
    with (
        FileLock(settings.DATA_DIR / f"document-file-{root_id}.lock", timeout=5),
        transaction.atomic(),
    ):
        root = (
            Document.objects.select_for_update()
            .filter(pk=root_id, root_document__isnull=True)
            .first()
        )
        if root:
            current = get_latest_version_for_root(root)
            if not pending_pages(current) and expected == signature(root, current):
                retire(records(root_id))
