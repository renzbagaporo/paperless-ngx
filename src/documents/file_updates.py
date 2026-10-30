"""Append or replace stored document files while retaining the root identity.

New scans are parsed before changing any persisted document. The current file
is resolved under the root/media locks after OCR, so concurrent append jobs
include pages committed by earlier jobs. Obsolete files are removed on commit.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from contextlib import ExitStack
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import magic
import pikepdf
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from filelock import FileLock

from documents.caching import clear_document_caches
from documents.file_handling import generate_unique_filename
from documents.file_handling import validate_path_in_root
from documents.models import Document
from documents.page_data import pages_for_file
from documents.parsers import make_thumbnail_from_pdf
from documents.signals import document_updated
from documents.utils import compute_checksum
from documents.versioning import get_latest_version_for_root
from paperless.config import RemoteOCRConfig
from paperless.parsers import ParserContext
from paperless.parsers.registry import get_parser_registry

logger = logging.getLogger("paperless.file_updates")
SUPPORTED_UPLOAD_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/gif",
    "image/webp",
}


def _paths(document: Document, *, thumbnail: bool = True) -> list[Path]:
    return [
        p
        for p in (
            document.source_path,
            document.archive_path,
            document.thumbnail_path if thumbnail else None,
        )
        if p is not None
    ]


def _is_referenced(path: Path) -> bool:
    if path.is_relative_to(settings.ORIGINALS_DIR):
        return Document.global_objects.filter(
            filename=str(path.relative_to(settings.ORIGINALS_DIR)),
        ).exists()
    if path.is_relative_to(settings.ARCHIVE_DIR):
        return Document.global_objects.filter(
            archive_filename=str(path.relative_to(settings.ARCHIVE_DIR)),
        ).exists()
    if path.is_relative_to(settings.THUMBNAIL_DIR):
        return (
            path.stem.isdigit()
            and Document.global_objects.filter(pk=path.stem.lstrip("0") or "0").exists()
        )
    raise ValueError("Path is outside document storage")


def available_destination(
    document: Document,
    directory: Path,
    *,
    archive_filename: bool = False,
    candidate: Path | None = None,
) -> Path:
    """Reserve filesystem and committed SQL names while the caller holds MEDIA_LOCK."""
    base = (
        candidate
        if candidate is not None
        else directory
        / generate_unique_filename(document, archive_filename=archive_filename)
    )
    validate_path_in_root(base, directory)
    destination = base
    counter = 0
    while destination.exists() or _is_referenced(destination):
        counter += 1
        destination = base.with_stem(f"{base.stem}_{counter:02}")
    return destination


@dataclass
class RollbackFile:
    destination: Path
    thumbnail: Path
    previous_thumbnail: bytes | None
    document_id: int
    checksum: str | None = None


def _remove_obsolete(
    paths: list[Path],
    version_ids: list[int],
    *,
    identities=None,
    version_identities=None,
    root_id=None,
) -> None:
    """Cleanup follows commit; a failed unlink leaves unused bytes, never a broken root."""
    if version_ids:
        # Bind a cleanup intent to the same deletion lifetime at the actual
        # hard-delete boundary. Restore/retrash and imports can reuse IDs.
        with transaction.atomic():
            versions = Document.deleted_objects.select_for_update().filter(
                pk__in=version_ids,
            )
            if version_identities is not None:
                from documents.file_cleanup import version_identity

                versions = versions.filter(root_document_id=root_id)
                version_ids = [
                    version.pk
                    for version in versions
                    if version_identities.get(str(version.pk))
                    == version_identity(version)
                ]
            else:
                version_ids = list(versions.values_list("pk", flat=True))
            # The deleted manager performs a hard delete, as empty_trash() does.
            Document.deleted_objects.filter(pk__in=version_ids).delete()
        # Retired files are unlinked only after the hard-delete commit succeeds.
    failures = []
    with FileLock(settings.MEDIA_LOCK):
        for path in set(paths):
            # Another update may already have reused a name. Never unlink a path
            # currently referenced by any document, including documents in the trash.
            try:
                referenced = _is_referenced(path)
            except ValueError:
                logger.error("Refusing cleanup outside document storage: %s", path)
                continue
            if referenced:
                continue
            if identities is not None and path.exists():
                from documents.file_cleanup import file_identity

                if file_identity(path) != identities.get(path):
                    logger.error(
                        "Retired file identity changed; retaining it for review",
                    )
                    failures.append(path)
                    continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove obsolete document file %s", path)
                failures.append(path)
    if failures:
        raise OSError(f"Could not remove {len(failures)} obsolete document file(s)")
    for document_id in version_ids:
        clear_document_caches(document_id)


def _cleanup_after_commit(
    paths: list[Path],
    version_ids: list[int],
    *,
    job_id=None,
) -> None:
    if job_id is not None:
        from documents.file_cleanup import complete_cleanup

        complete_cleanup(job_id)
        return
    try:
        _remove_obsolete(paths, version_ids)
    except Exception:
        logger.exception("Queuing retry to remove obsolete document files")
        from documents.tasks import cleanup_obsolete_document_files

        filenames = [str(path) for path in paths]
        cleanup_obsolete_document_files.delay(filenames, version_ids)


def _publish(
    root_id: int,
    version_ids: list[int],
    *,
    workflow_events: bool = True,
) -> None:
    clear_document_caches(root_id)
    root = Document.objects.filter(pk=root_id).first()
    from documents.search import get_backend

    backend = get_backend()
    if root is None:
        backend.remove(root_id)
        return
    from documents.deferred_ingestion import suppress_updated_event

    if workflow_events and not suppress_updated_event(root):
        document_updated.send_robust(sender=None, document=root, skip_ai_index=True)
    else:
        from documents.signals.handlers import send_websocket_document_updated

        send_websocket_document_updated(sender=None, document=root)
    root.refresh_from_db(from_queryset=Document.global_objects.all())
    for version_id in version_ids:
        backend.remove(version_id)
    backend.add_or_update(root)


@contextmanager
def _atomic_file_update():
    rollback = []
    committed = False

    def acknowledge_commit():
        nonlocal committed
        committed = True

    try:
        with transaction.atomic():
            # Register before installation callbacks: a callback failure occurs
            # after COMMIT and must never trigger filesystem rollback.
            transaction.on_commit(acknowledge_commit)
            yield rollback
    except Exception:
        if committed:
            raise
        # Include errors raised by the database COMMIT itself, after _install
        # has returned. Original document bytes have not yet been removed.
        with FileLock(settings.MEDIA_LOCK):
            for entry in reversed(rollback):
                try:
                    if _is_referenced(entry.destination):
                        continue  # A post-commit callback failed; data is committed.
                except Exception:
                    logger.exception(
                        "Could not verify rollback file ownership; retaining bytes",
                    )
                    continue
                entry.destination.unlink(missing_ok=True)
                if entry.previous_thumbnail is None:
                    entry.thumbnail.unlink(missing_ok=True)
                else:
                    entry.thumbnail.write_bytes(entry.previous_thumbnail)
        raise


def _install(
    root: Document,
    source: Path,
    thumbnail: Path,
    *,
    text: str,
    page_count: int | None,
    filename: str,
    keep_only_current_file: bool,
    reason: str,
    rollback: list,
    actor=None,
    pages: list[dict] | None = None,
    manual_content: bool = False,
    file_event: bool = True,
    index_pending: bool = True,
    discard_history: bool = True,
) -> Document:
    """Called with the root row and MEDIA_LOCK held, in an atomic transaction."""
    from auditlog.models import LogEntry

    previous = (
        list(Document.global_objects.filter(root_document=root))
        if discard_history
        else []
    )
    old_paths = _paths(root, thumbnail=False) if keep_only_current_file else []
    old_ids = [v.pk for v in previous] if keep_only_current_file else []
    for version in previous if keep_only_current_file else []:
        old_paths.extend(_paths(version))
    if keep_only_current_file:
        target = root
    else:
        next_index = (
            Document.global_objects.filter(root_document=root).aggregate(
                n=Max("version_index"),
            )["n"]
            or 0
        ) + 1
        target = Document.objects.create(
            root_document=root,
            version_index=next_index,
            title=root.title,
            created=root.created,
            owner=root.owner,
            mime_type="application/pdf",
            checksum=compute_checksum(source),
            content=text,
            page_count=page_count,
            original_filename=filename,
            version_label=reason,
        )
    target.mime_type = "application/pdf"
    # A fresh, unreferenced path means a database rollback leaves the previous
    # file intact. Do not let generate_unique_filename return the current path.
    target.filename = None
    destination = available_destination(target, settings.ORIGINALS_DIR)
    validate_path_in_root(destination, settings.ORIGINALS_DIR)
    destination.parent.mkdir(parents=True, exist_ok=True)
    thumb_path = target.thumbnail_path
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    old_thumb = thumb_path.read_bytes() if thumb_path.exists() else None
    entry = RollbackFile(destination, thumb_path, old_thumb, target.pk)
    rollback.append(entry)
    shutil.copyfile(source, destination)
    entry.checksum = compute_checksum(destination)
    with tempfile.NamedTemporaryFile(dir=thumb_path.parent, delete=False) as output:
        temporary_thumb = Path(output.name)
        output.write(thumbnail.read_bytes())
    temporary_thumb.replace(thumb_path)
    updates = {
        "filename": str(destination.relative_to(settings.ORIGINALS_DIR)),
        "archive_filename": None,
        "archive_checksum": None,
        "mime_type": "application/pdf",
        "checksum": compute_checksum(destination),
        "content": text,
        "page_count": page_count,
        "original_filename": filename,
        "modified": timezone.now(),
    }
    Document.objects.filter(pk=target.pk).update(**updates)
    if keep_only_current_file:
        # Hide obsolete children in this transaction without model save signals:
        # native soft deletion saves each row and its rename receiver takes
        # MEDIA_LOCK again. Normal hard deletion/cascades run after commit,
        # outside the media lock, before the guarded file cleanup below.
        Document.global_objects.filter(pk__in=old_ids).update(
            deleted_at=timezone.now(),
            restored_at=None,
            transaction_id=uuid.uuid4(),
        )
    else:
        Document.objects.filter(pk=root.pk).update(modified=timezone.now())
    if settings.AUDIT_LOG_ENABLED:
        LogEntry.objects.log_create(
            instance=root,
            actor=actor,
            action=LogEntry.Action.UPDATE,
            changes={"File": ["previous", reason]},
            additional_data={
                "reason": reason,
                "keep_only_current_file": keep_only_current_file,
                "discard_history": discard_history,
            },
        )
    from documents.file_cleanup import record_cleanup

    cleanup = record_cleanup(root.pk, old_paths, old_ids)
    transaction.on_commit(
        lambda: _cleanup_after_commit(
            old_paths,
            old_ids,
            job_id=cleanup.pk if cleanup else None,
        ),
        robust=True,
    )
    target.refresh_from_db()
    if pages is not None:
        from documents.page_data import save_pages

        # Each custom revision is the final merged PDF. Keeping file history
        # retains prior revisions, not a raw/archive pair on this revision.
        for page in pages:
            page["provenance"]["store_only_current_file"] = True
        save_pages(
            target,
            pages,
            file_event=file_event,
            automatic_content=text,
            manual_content=manual_content,
            index_pending=index_pending,
        )
    transaction.on_commit(
        lambda: _publish(root.pk, old_ids, workflow_events=file_event),
        robust=True,
    )
    return target


def _pdf_source(source: Path, mime_type: str, work: Path) -> Path:
    if mime_type == "application/pdf":
        return source
    from PIL import Image
    from PIL import ImageOps
    from PIL import ImageSequence

    with tempfile.NamedTemporaryFile(dir=work, suffix=".pdf", delete=False) as output:
        target = Path(output.name)
    with Image.open(source) as image:
        images = [
            ImageOps.exif_transpose(frame).convert("RGB")
            for frame in ImageSequence.Iterator(image)
        ]
        images[0].save(target, "PDF", save_all=True, append_images=images[1:])
    return target


def check_file_update_permissions(
    root: Document,
    user,
    *,
    keep_only_current_file: bool,
) -> None:
    from documents.permissions import has_perms_owner_aware

    if not user.has_perm("documents.change_document") or not has_perms_owner_aware(
        user,
        "change_document",
        root,
    ):
        raise PermissionError("You can no longer change this document.")
    if keep_only_current_file and (
        not user.has_perm("documents.delete_document")
        or not has_perms_owner_aware(user, "delete_document", root)
    ):
        raise PermissionError(
            "Removing earlier files requires permission to delete this document.",
        )


def resolve_file_update_source(
    root: Document,
    source_document_id: int | None = None,
    source_mode: str = "latest_version",
) -> Document:
    """Match native source selection while restricting explicit versions to this root."""
    if source_mode not in {"latest_version", "explicit_selection"}:
        raise ValueError("Unknown source_mode.")
    selected = root
    if source_document_id is not None:
        selected = Document.objects.get(pk=source_document_id)
        if (selected.root_document_id or selected.pk) != root.pk:
            raise ValueError("The selected version belongs to another document.")
    if source_mode == "explicit_selection" or selected.pk != root.pk:
        return selected
    return get_latest_version_for_root(root)


def searchable_checksum(document: Document) -> str:
    return document.archive_checksum or document.checksum


def _checked_source(
    root,
    source_document_id,
    source_mode,
    source_checksum,
    expected_current_document_id,
    expected_current_checksum,
):
    current = get_latest_version_for_root(root)
    selected = resolve_file_update_source(root, source_document_id, source_mode)
    if (
        expected_current_document_id is not None
        and (
            current.pk != expected_current_document_id
            or searchable_checksum(current) != expected_current_checksum
        )
    ) or (
        source_checksum is not None and searchable_checksum(selected) != source_checksum
    ):
        raise ValueError("The document file changed. Reload the editor before saving.")
    return selected


def _edit_pdf_pages(source: Path, target: Path, operations: list[dict]) -> None:
    with pikepdf.open(source) as original, pikepdf.new() as edited:
        for operation in operations:
            page = operation["page"]
            rotation = operation.get("rotate", 0)
            if operation.get("doc", 0) != 0:
                raise ValueError("Multiple output documents specified.")
            if not isinstance(page, int) or not 1 <= page <= len(original.pages):
                raise ValueError("Page is out of bounds.")
            if not isinstance(rotation, int) or rotation % 90:
                raise ValueError("Rotation must be a multiple of 90 degrees.")
            edited.pages.append(original.pages[page - 1])
            if rotation:
                edited.pages[-1].rotate(rotation, relative=True)
        edited.remove_unreferenced_resources()
        edited.save(target)


def update_file(
    root_id: int,
    uploaded_file: Path | None,
    *,
    append: bool,
    keep_only_current_file: bool,
    remote_ocr: bool | None = None,
    user_id: int | None = None,
    operations: list[dict] | None = None,
    source_document_id: int | None = None,
    source_mode: str = "latest_version",
    source_checksum: str | None = None,
    expected_current_document_id: int | None = None,
    expected_current_checksum: str | None = None,
) -> dict[str, int]:
    from django.contrib.auth import get_user_model

    mime_type = magic.from_file(uploaded_file, mime=True) if uploaded_file else None
    if uploaded_file and mime_type not in SUPPORTED_UPLOAD_TYPES:
        raise ValueError("Upload a PDF or supported image file.")
    if uploaded_file is None and not operations:
        raise ValueError("At least one output page is required.")
    lock_path = settings.DATA_DIR / f"document-file-{root_id}.lock"
    with FileLock(lock_path):
        root = Document.objects.get(pk=root_id, root_document__isnull=True)
        user = get_user_model().objects.get(pk=user_id) if user_id else None
        if user is not None:
            check_file_update_permissions(
                root,
                user,
                keep_only_current_file=keep_only_current_file,
            )
        _checked_source(
            root,
            source_document_id,
            source_mode,
            source_checksum,
            expected_current_document_id,
            expected_current_checksum,
        )
        with ExitStack() as stack:
            work = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR),
                ),
            )
            new_pdf = None
            new_text = ""
            new_thumbnail = None
            checks = None
            new_pages = []
            if uploaded_file:
                parser_class = get_parser_registry().get_parser_for_file(
                    mime_type,
                    uploaded_file.name,
                    uploaded_file,
                    allow_remote=RemoteOCRConfig().remote_ocr_by_default
                    if remote_ocr is None
                    else remote_ocr,
                )
                if parser_class is None:
                    raise ValueError("No parser is available for this file.")
                parser = stack.enter_context(parser_class())
                parser.configure(ParserContext())
                parser.parse(uploaded_file, mime_type, produce_archive=True)
                parsed = parser.get_archive_path()
                new_pdf = (
                    Path(parsed)
                    if parsed
                    else _pdf_source(uploaded_file, mime_type, work)
                )
                new_text = parser.get_text()
                from documents.page_data import parser_pages

                new_pages = parser_pages(parser, new_pdf)
                from documents.ingestion_checks import collect_ingestion_checks

                checks = collect_ingestion_checks(parser, new_text)
                new_thumbnail = parser.get_thumbnail(uploaded_file, mime_type)
            with _atomic_file_update() as rollback:
                root = Document.objects.select_for_update().get(
                    pk=root_id,
                    root_document__isnull=True,
                )
                if user_id is not None:
                    # Reload to avoid cached global permissions while OCR was running.
                    user = get_user_model().objects.get(pk=user_id)
                    check_file_update_permissions(
                        root,
                        user,
                        keep_only_current_file=keep_only_current_file,
                    )
                with FileLock(settings.MEDIA_LOCK):
                    latest = _checked_source(
                        root,
                        source_document_id,
                        source_mode,
                        source_checksum,
                        expected_current_document_id,
                        expected_current_checksum,
                    )
                    result = new_pdf
                    text = new_text
                    thumbnail = Path(new_thumbnail) if new_thumbnail else None
                    retained_count = 0
                    pages = new_pages
                    manual_content = False
                    if append or uploaded_file is None:
                        current = latest.archive_path or latest.source_path
                        current_mime = (
                            "application/pdf"
                            if latest.has_archive_version
                            else latest.mime_type
                        )
                        current = _pdf_source(current, current_mime, work)
                        from documents.page_data import selected_pages

                        retained_pages = pages_for_file(latest, current)
                        from documents.page_data import has_manual_content
                        from documents.page_data import stored_pages

                        retains_all_pages = operations is None or {
                            op["page"] for op in operations
                        } == set(range(1, len(retained_pages) + 1))
                        manual_content = (
                            retains_all_pages
                            and (
                                source_mode != "explicit_selection"
                                or stored_pages(latest) is not None
                            )
                            and has_manual_content(
                                latest,
                                retained_pages,
                            )
                        )
                        if operations is not None:
                            edited = work / "edited.pdf"
                            _edit_pdf_pages(current, edited, operations)
                            current = edited
                            retained_pages = selected_pages(retained_pages, operations)
                        pages = retained_pages + new_pages
                        with pikepdf.open(current) as retained:
                            retained_count = len(retained.pages)
                        if uploaded_file is None:
                            result = current
                        else:
                            result = work / "appended.pdf"
                            with (
                                pikepdf.open(current) as existing,
                                pikepdf.open(new_pdf) as added,
                            ):
                                existing.pages.extend(added.pages)
                                existing.save(result)
                        if operations is None or manual_content:
                            text = "\n\n".join(
                                value for value in (latest.content, new_text) if value
                            )
                            thumbnail = (
                                latest.thumbnail_path
                                if operations is None
                                else make_thumbnail_from_pdf(result, work)
                            )
                        else:
                            from documents.page_data import joined_text

                            text = joined_text(pages)
                            thumbnail = make_thumbnail_from_pdf(result, work)
                    with pikepdf.open(result) as document_pdf:
                        count = len(document_pdf.pages)
                    name = (
                        root.original_filename
                        if append or uploaded_file is None
                        else uploaded_file.name
                    )
                    _install(
                        root,
                        result,
                        thumbnail,
                        text=text,
                        page_count=count,
                        filename=str(Path(name or "document.pdf").with_suffix(".pdf")),
                        keep_only_current_file=keep_only_current_file,
                        reason="PDF edited"
                        if uploaded_file is None
                        else ("Pages appended" if append else "File replaced"),
                        rollback=rollback,
                        actor=user,
                        pages=pages,
                        manual_content=manual_content,
                    )
                    if checks is not None:
                        from documents.ingestion_checks import apply_ingestion_checks

                        apply_ingestion_checks(
                            root,
                            checks,
                            page_offset=retained_count,
                            preserve_metadata=True,
                        )
    return {"document_id": root_id}


def _compact_files(
    root_id: int,
    *,
    discard_history: bool,
    expected_revision: tuple[str, str | None] | None = None,
) -> None:
    """Compact a selected revision while holding its root and media locks."""
    from documents.page_data import has_manual_content

    with (
        FileLock(settings.DATA_DIR / f"document-file-{root_id}.lock"),
        _atomic_file_update() as rollback,
    ):
        root = (
            Document.objects.select_for_update()
            .filter(
                pk=root_id,
                root_document__isnull=True,
            )
            .first()
        )
        if root is None:
            return
        with FileLock(settings.MEDIA_LOCK):
            current = get_latest_version_for_root(root) if discard_history else root
            if (
                expected_revision is not None
                and (
                    current.checksum,
                    current.archive_checksum,
                )
                != expected_revision
            ):
                return
            source = current.archive_path or current.source_path
            mime_type = (
                "application/pdf" if current.has_archive_version else current.mime_type
            )
            if mime_type != "application/pdf":
                return
            # A compact root that has no archive or child versions is already complete.
            if current.pk == root.pk and not current.has_archive_version:
                return
            _install(
                root,
                source,
                current.thumbnail_path,
                text=current.content,
                page_count=current.page_count,
                filename=str(
                    Path(current.original_filename or "document.pdf").with_suffix(
                        ".pdf",
                    ),
                ),
                keep_only_current_file=True,
                reason="Stored current file only"
                if discard_history
                else "Compacted ingested root file",
                rollback=rollback,
                pages=pages_for_file(current, source),
                manual_content=has_manual_content(
                    current,
                    pages_for_file(current, source),
                ),
                file_event=False,
                index_pending=False,
                discard_history=discard_history,
            )


def compact_current_files(root_id: int) -> None:
    """Keep the latest searchable file on the root and discard older file data."""
    _compact_files(root_id, discard_history=True)


def compact_ingested_root(
    root_id: int,
    file_checksum: str,
    archive_checksum: str | None,
) -> None:
    """Compact only the originally ingested root, preserving later file history."""
    _compact_files(
        root_id,
        discard_history=False,
        expected_revision=(file_checksum, archive_checksum),
    )


def compact_imported_document(document: Document) -> None:
    """Compaction is retryable maintenance after an import has safely succeeded."""
    expected_checksum = document.checksum
    expected_archive_checksum = document.archive_checksum
    try:
        compact_ingested_root(document.pk, expected_checksum, expected_archive_checksum)
    except Exception:
        logger.exception(
            "Imported document %s is saved; queuing file compaction retry",
            document.pk,
        )
        from documents.tasks import compact_document_files

        try:
            compact_document_files.delay(
                document.pk,
                expected_checksum,
                expected_archive_checksum,
            )
        except Exception:
            # A broker outage must not turn a persisted import into a failed
            # consume task whose input file has already been consumed.
            logger.exception("Could not queue compaction for document %s", document.pk)
