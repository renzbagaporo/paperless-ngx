"""Extract or move searchable PDF pages and their parsing without invoking OCR."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pikepdf
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from filelock import FileLock
from guardian.models import GroupObjectPermission
from guardian.models import UserObjectPermission
from guardian.shortcuts import assign_perm

from documents.file_updates import _atomic_file_update
from documents.file_updates import _checked_source
from documents.file_updates import _edit_pdf_pages
from documents.file_updates import _install
from documents.file_updates import check_file_update_permissions
from documents.models import Document
from documents.models import Note
from documents.page_data import joined_text
from documents.page_data import pages_for_file
from documents.page_data import selected_pages
from documents.parsers import make_thumbnail_from_pdf
from documents.permissions import has_perms_owner_aware
from documents.utils import compute_checksum


def _copy_metadata(source: Document, target: Document) -> None:
    target.tags.set(source.tags.all())
    for field in source.custom_fields.all():
        field.pk = None
        field.document = target
        field.save()
    content_type = ContentType.objects.get_for_model(Document)
    for permission in UserObjectPermission.objects.filter(
        content_type=content_type,
        object_pk=source.pk,
    ).select_related("permission", "user"):
        assign_perm(permission.permission.codename, permission.user, target)
    for permission in GroupObjectPermission.objects.filter(
        content_type=content_type,
        object_pk=source.pk,
    ).select_related("permission", "group"):
        assign_perm(permission.permission.codename, permission.group, target)


def split_pages(
    root_id: int,
    operations: list[dict],
    *,
    move: bool = False,
    title: str | None = None,
    archive_serial_number: int | None = None,
    keep_only_current_file: bool = False,
    source_document_id: int | None = None,
    source_mode: str = "latest_version",
    source_checksum: str | None = None,
    expected_current_document_id: int | None = None,
    expected_current_checksum: str | None = None,
    user_id: int | None = None,
) -> dict:
    if not operations:
        raise ValueError("Select at least one page to extract.")
    page_numbers = [operation["page"] for operation in operations]
    if len(set(page_numbers)) != len(page_numbers):
        raise ValueError("A page can only be selected once.")
    with (
        FileLock(settings.DATA_DIR / f"document-file-{root_id}.lock"),
        tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR) as temporary,
        _atomic_file_update() as rollback,
    ):
        work = Path(temporary)
        root = Document.objects.select_for_update().get(
            pk=root_id,
            root_document__isnull=True,
        )
        actor = get_user_model().objects.get(pk=user_id) if user_id else None
        if actor is not None:
            if (
                not actor.has_perm("documents.add_document")
                or not actor.has_perm("documents.view_document")
                or not has_perms_owner_aware(actor, "view_document", root)
            ):
                raise PermissionError(
                    "Extracting pages requires document view and add permissions.",
                )
            if move:
                check_file_update_permissions(
                    root,
                    actor,
                    keep_only_current_file=keep_only_current_file,
                )
        source = _checked_source(
            root,
            source_document_id,
            source_mode,
            source_checksum,
            expected_current_document_id,
            expected_current_checksum,
        )
        target = Document.objects.create(
            title=title or f"{root.title} — extracted pages"[:128],
            owner=root.owner,
            created=root.created,
            correspondent=root.correspondent,
            document_type=root.document_type,
            storage_path=root.storage_path,
            archive_serial_number=archive_serial_number,
            mime_type="application/pdf",
            checksum="",
            content="",
        )
        # Assign metadata while filename is unset: rename receivers cannot move
        # a newly installed file out of the transaction's rollback ledger.
        _copy_metadata(root, target)
        with FileLock(settings.MEDIA_LOCK):
            if not source.has_archive_version and source.mime_type != "application/pdf":
                raise ValueError("Select a PDF document.")
            current = source.archive_path or source.source_path
            source_fingerprint = source.archive_checksum or source.checksum
            if compute_checksum(current) != source_fingerprint:
                raise ValueError("Stored file checksum does not match the document.")
            with pikepdf.open(current) as pdf:
                page_count = len(pdf.pages)
            if move and len(page_numbers) >= page_count:
                raise ValueError(
                    "Moving every page would leave an empty document; extract them instead.",
                )
            extracted = work / "extracted.pdf"
            _edit_pdf_pages(current, extracted, operations)
            evidence = pages_for_file(source, current)
            extracted_pages = selected_pages(evidence, operations)
            thumbnail = make_thumbnail_from_pdf(extracted, work)
            _install(
                target,
                extracted,
                thumbnail,
                text=joined_text(extracted_pages),
                page_count=len(extracted_pages),
                filename="extracted.pdf",
                keep_only_current_file=True,
                reason="Pages extracted",
                rollback=rollback,
                actor=actor,
                pages=extracted_pages,
            )
            if move:
                remaining = [
                    {"page": number}
                    for number in range(1, page_count + 1)
                    if number not in page_numbers
                ]
                retained = work / "retained.pdf"
                _edit_pdf_pages(current, retained, remaining)
                retained_pages = selected_pages(evidence, remaining)
                thumbnail = make_thumbnail_from_pdf(retained, work)
                _install(
                    root,
                    retained,
                    thumbnail,
                    text=joined_text(retained_pages),
                    page_count=len(retained_pages),
                    filename="document.pdf",
                    keep_only_current_file=keep_only_current_file,
                    reason="Pages moved",
                    rollback=rollback,
                    actor=actor,
                    pages=retained_pages,
                )
        target.refresh_from_db()
        Note.objects.create(
            document=target,
            user=actor,
            note=f"{'Moved' if move else 'Extracted'} pages {page_numbers} from document {root.pk} (file {source_fingerprint}).",
        )
    return {"document_id": root_id, "new_document_id": target.pk}
