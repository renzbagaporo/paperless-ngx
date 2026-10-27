from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest import mock

import pikepdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test import override_settings
from rest_framework.test import APITestCase

from documents.file_updates import compact_current_files
from documents.file_updates import update_file
from documents.models import Document
from documents.models import Note
from documents.utils import compute_checksum
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global

if TYPE_CHECKING:
    from pathlib import Path


def write_pdf(path: Path, widths: list[int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.new() as pdf:
        for width in widths:
            pdf.add_blank_page(page_size=(width, 500))
        pdf.save(path)
    return path


def widths(path: Path) -> list[int]:
    with pikepdf.open(path) as pdf:
        return [int(page.mediabox[2]) for page in pdf.pages]


class FakeParser:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def configure(self, _context):
        pass

    def parse(self, path, mime_type, *, produce_archive):
        self.source = path

    def get_archive_path(self):
        return self.source

    def get_text(self):
        return "New scan text"

    def get_thumbnail(self, path, mime_type):
        thumbnail = path.with_suffix(".webp")
        thumbnail.write_bytes(b"new thumbnail")
        return thumbnail


class FileUpdateTests(DirectoriesMixin, TestCase):
    def setUp(self):
        self.parser = mock.patch("documents.file_updates.get_parser_registry")
        self.registry = self.parser.start().return_value
        self.registry.get_parser_for_file.return_value = FakeParser
        self.addCleanup(self.parser.stop)
        self.publish = mock.patch("documents.file_updates._publish")
        self.publish.start()
        self.addCleanup(self.publish.stop)
        self.root = DocumentFactory(
            title="Water Supply Notices",
            content="Previous notice",
            mime_type="application/pdf",
            archive_serial_number=43,
        )
        write_pdf(self.root.source_path, [100])
        self.root.thumbnail_path.write_bytes(b"old thumbnail")
        Document.objects.filter(pk=self.root.pk).update(
            checksum=compute_checksum(self.root.source_path),
            page_count=1,
        )
        self.root.refresh_from_db()
        Note.objects.create(document=self.root, note="Keep this note")
        self.upload = write_pdf(self.dirs.scratch_dir / "new.pdf", [300])

    def run_update(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return update_file(
                self.root.pk,
                self.upload,
                append=True,
                keep_only_current_file=True,
                **kwargs,
            )

    def test_append_keeps_root_identity_and_metadata_and_only_current_file(self):
        original_id = self.root.pk
        old_path = self.root.source_path
        original_metadata = {
            name: getattr(self.root, name)
            for name in (
                "title",
                "created",
                "added",
                "owner_id",
                "archive_serial_number",
                "correspondent_id",
                "document_type_id",
                "storage_path_id",
            )
        }
        result = self.run_update()
        self.root.refresh_from_db()
        self.assertEqual(result, {"document_id": original_id})
        self.assertEqual(widths(self.root.source_path), [100, 300])
        self.assertEqual(self.root.content, "Previous notice\n\nNew scan text")
        self.assertEqual(self.root.page_count, 2)
        self.assertEqual(self.root.checksum, compute_checksum(self.root.source_path))
        self.assertEqual(
            {k: getattr(self.root, k) for k in original_metadata},
            original_metadata,
        )
        self.assertEqual(self.root.notes.get().note, "Keep this note")
        self.assertEqual(Document.objects.count(), 1)
        self.assertFalse(old_path.exists())
        self.assertFalse(self.root.has_archive_version)
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"old thumbnail")

    @override_settings(AUDIT_LOG_ENABLED=True)
    def test_populated_metadata_permissions_and_share_links_are_preserved(self):
        from documents.models import CustomField
        from documents.models import CustomFieldInstance
        from documents.models import ShareLink
        from documents.permissions import has_perms_owner_aware
        from paperless_testing.factories import CorrespondentFactory
        from paperless_testing.factories import DocumentTypeFactory
        from paperless_testing.factories import StoragePathFactory
        from paperless_testing.factories import TagFactory
        from paperless_testing.permissions import grant_object

        owner, viewer = UserFactory(), UserFactory()
        grant_global(owner, "change_document", "delete_document")
        correspondent, document_type = CorrespondentFactory(), DocumentTypeFactory()
        storage = StoragePathFactory(path="notices/{{ title }}")
        Document.objects.filter(pk=self.root.pk).update(
            owner=owner,
            correspondent=correspondent,
            document_type=document_type,
            storage_path=storage,
        )
        tag = TagFactory()
        self.root.tags.add(tag)
        field = CustomField.objects.create(name="Water account", data_type="string")
        custom_data = {
            "document": self.root,
            "field": field,
            "value_text": "ACCOUNT-123",
        }
        value = CustomFieldInstance.objects.create(**custom_data)
        share_data = {
            "document": self.root,
            "owner": owner,
            "slug": "water-supply-link",
        }
        link = ShareLink.objects.create(**share_data)
        grant_object(viewer, self.root, "view_document", "change_document")
        self.root.refresh_from_db()
        old = {
            name: getattr(self.root, name)
            for name in (
                "owner_id",
                "correspondent_id",
                "document_type_id",
                "storage_path_id",
                "title",
                "created",
                "added",
                "archive_serial_number",
            )
        }
        self.run_update(user_id=owner.pk)
        self.root.refresh_from_db()
        self.assertEqual({k: getattr(self.root, k) for k in old}, old)
        self.assertEqual(list(self.root.tags.values_list("pk", flat=True)), [tag.pk])
        value.refresh_from_db()
        self.assertEqual(value.value_text, "ACCOUNT-123")
        link.refresh_from_db()
        self.assertEqual(link.document_id, self.root.pk)
        self.assertEqual(link.slug, "water-supply-link")
        self.assertTrue(has_perms_owner_aware(viewer, "view_document", self.root))
        self.assertTrue(has_perms_owner_aware(viewer, "change_document", self.root))

        from auditlog.models import LogEntry

        audit_filter = {
            "object_pk": str(self.root.pk),
            "additional_data__reason": "Pages appended",
        }
        entry = LogEntry.objects.get(**audit_filter)
        self.assertEqual(entry.actor_id, owner.pk)

    def test_delete_permission_revoked_during_ocr_prevents_history_removal(self):
        from django.contrib.auth.models import Permission

        user = UserFactory()
        grant_global(user, "change_document", "delete_document")
        previous = self.root.source_path
        parse = FakeParser.parse

        def revoke(parser, *args, **kwargs):
            parse(parser, *args, **kwargs)
            user.user_permissions.remove(
                Permission.objects.get(codename="delete_document"),
            )

        with mock.patch.object(FakeParser, "parse", revoke):
            with self.assertRaisesMessage(PermissionError, "Removing earlier files"):
                self.run_update(user_id=user.pk)
        self.root.refresh_from_db()
        self.assertEqual(self.root.source_path, previous)
        self.assertEqual(widths(previous), [100])
        self.assertEqual(self.root.content, "Previous notice")

    def test_user_deactivated_during_ocr_cannot_update_the_document(self):
        user = UserFactory()
        grant_global(user, "change_document", "delete_document")
        previous = self.root.source_path
        parse = FakeParser.parse

        def deactivate(parser, *args, **kwargs):
            parse(parser, *args, **kwargs)
            type(user).objects.filter(pk=user.pk).update(is_active=False)

        with mock.patch.object(FakeParser, "parse", deactivate):
            with self.assertRaisesMessage(PermissionError, "change this document"):
                self.run_update(user_id=user.pk)
        self.root.refresh_from_db()
        self.assertEqual(self.root.source_path, previous)
        self.assertEqual(widths(previous), [100])

    def test_append_uses_latest_searchable_archive_and_discards_older_versions(self):
        version = DocumentFactory(
            root_document=self.root,
            content="Latest notice",
            mime_type="application/pdf",
            version_index=1,
        )
        write_pdf(version.source_path, [200])
        archive = self.dirs.archive_dir / "latest.pdf"
        write_pdf(archive, [250, 260])
        Document.objects.filter(pk=version.pk).update(
            archive_filename="latest.pdf",
            archive_checksum=compute_checksum(archive),
        )
        version.thumbnail_path.write_bytes(b"latest thumbnail")
        source = version.source_path
        self.run_update()
        self.root.refresh_from_db()
        self.assertEqual(widths(self.root.source_path), [250, 260, 300])
        self.assertEqual(self.root.content, "Latest notice\n\nNew scan text")
        self.assertFalse(Document.global_objects.filter(pk=version.pk).exists())
        self.assertFalse(source.exists())
        self.assertFalse(archive.exists())
        self.assertFalse(version.thumbnail_path.exists())
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"latest thumbnail")

    def test_replace_keeps_id_and_title_and_installs_new_thumbnail(self):
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                self.root.pk,
                self.upload,
                append=False,
                keep_only_current_file=True,
            )
        self.root.refresh_from_db()
        self.assertEqual(self.root.title, "Water Supply Notices")
        self.assertEqual(self.root.archive_serial_number, 43)
        self.assertEqual(widths(self.root.source_path), [300])
        self.assertEqual(self.root.content, "New scan text")
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"new thumbnail")

    def test_retaining_history_adds_a_version_and_keeps_root_original(self):
        old = self.root.source_path
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                self.root.pk,
                self.upload,
                append=True,
                keep_only_current_file=False,
            )
        self.root.refresh_from_db()
        self.assertTrue(old.exists())
        self.assertEqual(widths(old), [100])
        self.assertEqual(widths(self.root.versions.get().source_path), [100, 300])
        self.assertEqual(self.root.archive_serial_number, 43)

    def test_appending_image_to_image_keeps_both_distinct_pages(self):
        from PIL import Image

        root = DocumentFactory(mime_type="image/png", content="Previous image")
        Image.new("RGB", (20, 50), "white").save(root.source_path)
        root.thumbnail_path.write_bytes(b"thumbnail")
        upload = self.dirs.scratch_dir / "new-image.png"
        Image.new("RGB", (40, 60), "white").save(upload)
        with mock.patch.object(FakeParser, "get_archive_path", return_value=None):
            with self.captureOnCommitCallbacks(execute=True):
                update_file(root.pk, upload, append=True, keep_only_current_file=True)
        root.refresh_from_db()
        self.assertEqual(widths(root.source_path), [20, 40])
        self.assertEqual(root.page_count, 2)
        self.assertEqual(root.mime_type, "application/pdf")

    def test_image_fallback_honors_phone_photo_exif_orientation(self):
        from PIL import Image

        root = DocumentFactory(mime_type="image/jpeg", content="Previous image")
        orientation = Image.Exif()
        orientation[274] = 6
        Image.new("RGB", (20, 50), "white").save(root.source_path, exif=orientation)
        root.thumbnail_path.write_bytes(b"thumbnail")
        upload = self.dirs.scratch_dir / "new-phone-image.jpg"
        Image.new("RGB", (40, 60), "white").save(upload, exif=orientation)
        with mock.patch.object(FakeParser, "get_archive_path", return_value=None):
            with self.captureOnCommitCallbacks(execute=True):
                update_file(root.pk, upload, append=True, keep_only_current_file=True)
        root.refresh_from_db()
        self.assertEqual(widths(root.source_path), [50, 60])
        with pikepdf.open(root.source_path) as pdf:
            self.assertEqual([int(page.mediabox[3]) for page in pdf.pages], [20, 40])

    def test_parser_failure_leaves_files_and_database_unchanged(self):
        old = self.root.source_path
        with mock.patch.object(
            FakeParser,
            "parse",
            side_effect=ValueError("OCR failed"),
        ):
            with self.assertRaisesMessage(ValueError, "OCR failed"):
                self.run_update()
        self.root.refresh_from_db()
        self.assertEqual(self.root.source_path, old)
        self.assertEqual(widths(old), [100])
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"old thumbnail")
        self.assertEqual(self.root.content, "Previous notice")

    def test_database_failure_restores_thumbnail_and_keeps_previous_file(self):
        old = self.root.source_path
        with (
            override_settings(AUDIT_LOG_ENABLED=True),
            mock.patch(
                "auditlog.models.LogEntry.objects.log_create",
                side_effect=RuntimeError("database failed"),
            ),
        ):
            with self.assertRaisesMessage(RuntimeError, "database failed"):
                self.run_update()
        self.root.refresh_from_db()
        self.assertEqual(self.root.source_path, old)
        self.assertEqual(widths(old), [100])
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"old thumbnail")
        self.assertEqual(list(self.dirs.originals_dir.glob("*.pdf")), [old])

    def test_revoked_permissions_are_checked_before_ocr(self):
        user = UserFactory()
        with self.assertRaises(PermissionError):
            self.run_update(user_id=user.pk)
        self.registry.get_parser_for_file.assert_not_called()

    def test_compact_import_promotes_searchable_archive_without_changing_identity(self):
        archive = write_pdf(self.dirs.archive_dir / "scan.pdf", [150, 160])
        Document.objects.filter(pk=self.root.pk).update(
            archive_filename="scan.pdf",
            archive_checksum=compute_checksum(archive),
            content="OCR content",
            page_count=2,
        )
        old = self.root.source_path
        with self.captureOnCommitCallbacks(execute=True):
            compact_current_files(self.root.pk)
        self.root.refresh_from_db()
        self.assertEqual(widths(self.root.source_path), [150, 160])
        self.assertFalse(old.exists())
        self.assertFalse(archive.exists())
        self.assertFalse(self.root.has_archive_version)
        self.assertEqual(self.root.archive_serial_number, 43)
        self.assertEqual(self.root.content, "OCR content")


class FileUpdateApiTests(DirectoriesMixin, APITestCase):
    def setUp(self):
        self.user = UserFactory()
        grant_global(self.user, "change_document")
        self.root = DocumentFactory(owner=self.user, mime_type="application/pdf")
        self.client.force_authenticate(self.user)
        self.file = write_pdf(self.dirs.scratch_dir / "upload.pdf", [100])

    def test_append_retaining_history_needs_change_permission_but_not_add_permission(
        self,
    ):
        with mock.patch(
            "documents.views.update_document_file.apply_async",
            return_value=SimpleNamespace(id="task-id"),
        ) as queue:
            response = self.client.post(
                f"/api/documents/{self.root.pk}/append/",
                {
                    "document": SimpleUploadedFile("new.pdf", self.file.read_bytes()),
                    "keep_only_current_file": "false",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, "task-id")
        self.assertEqual(
            queue.call_args.kwargs["kwargs"]["input_doc"].root_document_id,
            self.root.pk,
        )
        self.assertTrue(queue.call_args.kwargs["kwargs"]["append"])
        self.assertFalse(queue.call_args.kwargs["kwargs"]["keep_only_current_file"])

    def test_edit_only_user_cannot_remove_earlier_files(self):
        with mock.patch("documents.views.update_document_file.apply_async") as queue:
            response = self.client.post(
                f"/api/documents/{self.root.pk}/append/",
                {
                    "document": SimpleUploadedFile("new.pdf", self.file.read_bytes()),
                    "keep_only_current_file": "true",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 403)
        queue.assert_not_called()

    def test_keep_only_current_requires_delete_permission(self):
        grant_global(self.user, "delete_document")
        with mock.patch(
            "documents.views.update_document_file.apply_async",
            return_value=SimpleNamespace(id="task-id"),
        ) as queue:
            response = self.client.post(
                f"/api/documents/{self.root.pk}/append/",
                {
                    "document": SimpleUploadedFile("new.pdf", self.file.read_bytes()),
                    "keep_only_current_file": "true",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(queue.call_args.kwargs["kwargs"]["keep_only_current_file"])

    def test_another_owners_document_is_forbidden(self):
        other = DocumentFactory(owner=UserFactory())
        with mock.patch("documents.views.update_document_file.apply_async") as queue:
            response = self.client.post(
                f"/api/documents/{other.pk}/append/",
                {"document": SimpleUploadedFile("new.pdf", self.file.read_bytes())},
                format="multipart",
            )
        self.assertEqual(response.status_code, 403)
        queue.assert_not_called()

    def test_replace_can_retain_history(self):
        with mock.patch(
            "documents.views.update_document_file.apply_async",
            return_value=SimpleNamespace(id="task-id"),
        ) as queue:
            response = self.client.post(
                f"/api/documents/{self.root.pk}/replace_file/",
                {
                    "document": SimpleUploadedFile("new.pdf", self.file.read_bytes()),
                    "keep_only_current_file": "false",
                    "remote_ocr": "true",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 200)
        kwargs = queue.call_args.kwargs["kwargs"]
        self.assertFalse(kwargs["append"])
        self.assertFalse(kwargs["keep_only_current_file"])
        self.assertTrue(kwargs["remote_ocr"])

    def test_unsupported_upload_is_refused_before_queuing(self):
        with mock.patch("documents.views.update_document_file.apply_async") as queue:
            response = self.client.post(
                f"/api/documents/{self.root.pk}/append/",
                {
                    "document": SimpleUploadedFile("text.txt", b"not a scan"),
                    "keep_only_current_file": "false",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 400)
        queue.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_concurrent_appends_accumulate_pages(paperless_dirs):
    root = DocumentFactory(mime_type="application/pdf", content="old")
    write_pdf(root.source_path, [100])
    root.thumbnail_path.write_bytes(b"thumbnail")
    uploads = [
        write_pdf(paperless_dirs.scratch_dir / f"new-{i}.pdf", [i]) for i in (200, 300)
    ]
    with (
        mock.patch("documents.file_updates.get_parser_registry") as registry,
        mock.patch("documents.file_updates._publish"),
    ):
        registry.return_value.get_parser_for_file.return_value = FakeParser
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(
                workers.map(
                    lambda upload: update_file(
                        root.pk,
                        upload,
                        append=True,
                        keep_only_current_file=True,
                    ),
                    uploads,
                ),
            )
    root.refresh_from_db()
    actual = widths(root.source_path)
    assert actual[0] == 100
    assert sorted(actual[1:]) == [200, 300]
    assert len(actual) == 3
    assert all(result == {"document_id": root.pk} for result in results)


@pytest.mark.django_db(transaction=True)
def test_database_commit_failure_restores_previous_file_and_thumbnail(paperless_dirs):
    from django.db import OperationalError
    from django.db import connection

    root = DocumentFactory(mime_type="application/pdf", content="old")
    write_pdf(root.source_path, [100])
    root.thumbnail_path.write_bytes(b"original thumbnail")
    original = root.source_path
    upload = write_pdf(paperless_dirs.scratch_dir / "replacement.pdf", [200])
    with (
        mock.patch("documents.file_updates.get_parser_registry") as registry,
        mock.patch.object(
            connection,
            "commit",
            side_effect=OperationalError("commit failed"),
        ),
    ):
        registry.return_value.get_parser_for_file.return_value = FakeParser
        with pytest.raises(OperationalError, match="commit failed"):
            update_file(root.pk, upload, append=False, keep_only_current_file=True)
    root.refresh_from_db()
    assert root.source_path == original
    assert widths(original) == [100]
    assert root.content == "old"
    assert root.thumbnail_path.read_bytes() == b"original thumbnail"
    assert list(paperless_dirs.originals_dir.glob("*.pdf")) == [original]


@pytest.mark.django_db(transaction=True)
def test_file_update_refreshes_real_search_index(paperless_dirs):
    from documents.search import get_backend

    root = DocumentFactory(mime_type="application/pdf", content="oldnotice", owner=None)
    write_pdf(root.source_path, [100])
    root.thumbnail_path.write_bytes(b"thumbnail")
    upload = write_pdf(paperless_dirs.scratch_dir / "new.pdf", [200])
    backend = get_backend()
    backend.add_or_update(root)
    assert root.pk in backend.search_ids("oldnotice", user=None)
    with mock.patch("documents.file_updates.get_parser_registry") as registry:
        registry.return_value.get_parser_for_file.return_value = FakeParser
        update_file(root.pk, upload, append=False, keep_only_current_file=True)
    assert root.pk in backend.search_ids("New scan text", user=None)
    assert root.pk not in backend.search_ids("oldnotice", user=None)


@pytest.mark.django_db
def test_published_file_update_task_is_private_to_requester(paperless_dirs):
    import uuid

    from documents.data_models import ConsumableDocument
    from documents.data_models import DocumentSource
    from documents.models import PaperlessTask
    from documents.signals.handlers import before_task_publish_handler

    user = UserFactory()
    other = UserFactory()
    upload = write_pdf(paperless_dirs.scratch_dir / "private-scan.pdf", [100])
    task_id = str(uuid.uuid4())
    before_task_publish_handler(
        headers={"task": "documents.tasks.update_document_file", "id": task_id},
        body=(
            (),
            {
                "input_doc": ConsumableDocument(
                    source=DocumentSource.ApiUpload,
                    original_file=upload,
                ),
                "user_id": user.pk,
            },
            {},
        ),
    )
    task = PaperlessTask.objects.get(task_id=task_id)
    assert task.owner_id == user.pk
    from rest_framework.test import APIClient

    client = APIClient()
    grant_global(other, "view_paperlesstask")
    client.force_authenticate(other)
    assert client.get("/api/tasks/", {"task_id": task_id}).data["count"] == 0


@pytest.mark.django_db
def test_compaction_failure_keeps_successful_import_and_queues_retry():
    from documents.file_updates import compact_imported_document

    root = DocumentFactory()
    with (
        mock.patch(
            "documents.file_updates.compact_current_files",
            side_effect=OSError("disk full"),
        ),
        mock.patch("documents.tasks.compact_document_files.delay") as retry,
    ):
        compact_imported_document(root)
    assert Document.objects.filter(pk=root.pk).exists()
    retry.assert_called_once_with(root.pk)


@pytest.mark.django_db
def test_file_update_task_reports_private_progress_and_retains_root_id(paperless_dirs):
    from documents.data_models import ConsumableDocument
    from documents.data_models import DocumentSource
    from documents.tasks import update_document_file

    root = DocumentFactory()
    user = UserFactory()
    upload = write_pdf(paperless_dirs.scratch_dir / "upload.pdf", [100])
    input_doc = ConsumableDocument(
        source=DocumentSource.ApiUpload,
        original_file=upload,
        root_document_id=root.pk,
    )
    with (
        mock.patch(
            "documents.file_updates.update_file",
            return_value={"document_id": root.pk},
        ),
        mock.patch("documents.tasks.ProgressManager") as manager,
    ):
        result = update_document_file.run(
            input_doc,
            append=True,
            keep_only_current_file=True,
            user_id=user.pk,
        )
    assert result == {"document_id": root.pk}
    calls = manager.return_value.__enter__.return_value.send_progress.call_args_list
    assert all(call.kwargs["owner_id"] == user.pk for call in calls)
    assert calls[-1].kwargs["document_id"] == root.pk
    assert not upload.exists()
