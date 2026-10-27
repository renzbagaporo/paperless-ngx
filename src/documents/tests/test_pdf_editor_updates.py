from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest import mock

import pikepdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from guardian.shortcuts import assign_perm
from rest_framework.test import APITestCase

from documents.file_updates import update_file
from documents.models import Document
from documents.models import Note
from documents.tests.test_file_updates import FakeParser
from documents.tests.test_file_updates import widths
from documents.utils import compute_checksum
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global

if TYPE_CHECKING:
    from pathlib import Path


def searchable_pdf(path: Path, labels: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.new() as pdf:
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica,
            ),
        )
        for index, label in enumerate(labels, start=1):
            page = pdf.add_blank_page(page_size=(index * 100, 500))
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            page.Contents = pikepdf.Stream(
                pdf,
                f"BT /F1 4 Tf 10 100 Td ({label}) Tj ET".encode(),
            )
        pdf.save(path)
    return path


class PdfEditorUpdateTests(DirectoriesMixin, TestCase):
    def setUp(self):
        self.root = DocumentFactory(
            title="Water Supply Notices",
            mime_type="application/pdf",
            archive_serial_number=43,
            content="First notice\nDeleted notice\nThird notice",
        )
        searchable_pdf(
            self.root.source_path,
            ["First notice", "Deleted notice", "Third notice"],
        )
        self.root.thumbnail_path.write_bytes(b"old thumbnail")
        Document.objects.filter(pk=self.root.pk).update(
            checksum=compute_checksum(self.root.source_path),
            page_count=3,
        )
        self.root.refresh_from_db()
        Note.objects.create(document=self.root, note="Keep this note")
        self.registry = mock.patch("documents.file_updates.get_parser_registry").start()
        self.addCleanup(mock.patch.stopall)
        self.registry.return_value.get_parser_for_file.return_value = FakeParser
        self.publish = mock.patch("documents.file_updates._publish").start()

        def thumbnail(source, work):
            with pikepdf.open(source) as pdf:
                data = f"{pdf.pages[0].mediabox[2]}:{pdf.pages[0].get('/Rotate', 0)}".encode()
            target = work / "thumbnail.webp"
            target.write_bytes(data)
            return target

        self.thumbnail = mock.patch(
            "documents.file_updates.make_thumbnail_from_pdf",
            side_effect=thumbnail,
        ).start()

    def edit(self, **kwargs):
        defaults = {
            "append": False,
            "keep_only_current_file": True,
            "operations": [{"page": 3, "rotate": 90}, {"page": 1}],
            "source_checksum": self.root.checksum,
        }
        defaults.update(kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            return update_file(self.root.pk, None, **defaults)

    def test_page_edits_preserve_identity_metadata_and_searchable_text_without_ocr(
        self,
    ):
        original = self.root.source_path
        old_metadata = (
            self.root.title,
            self.root.created,
            self.root.added,
            self.root.archive_serial_number,
        )
        result = self.edit()
        self.root.refresh_from_db()
        self.assertEqual(result, {"document_id": self.root.pk})
        self.assertEqual(
            (
                self.root.title,
                self.root.created,
                self.root.added,
                self.root.archive_serial_number,
            ),
            old_metadata,
        )
        self.assertEqual(widths(self.root.source_path), [300, 100])
        with pikepdf.open(self.root.source_path) as pdf:
            self.assertEqual(pdf.pages[0].get("/Rotate"), 90)
        self.assertIn("Third notice", self.root.content)
        self.assertIn("First notice", self.root.content)
        self.assertNotIn("Deleted notice", self.root.content)
        self.assertLess(
            self.root.content.index("Third notice"),
            self.root.content.index("First notice"),
        )
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"300:90")
        self.assertEqual(self.root.page_count, 2)
        self.assertFalse(original.exists())
        self.assertEqual(self.root.notes.get().note, "Keep this note")
        self.registry.assert_not_called()

    def test_retaining_history_keeps_root_file_and_creates_searchable_version(self):
        original = self.root.source_path
        self.edit(keep_only_current_file=False)
        self.root.refresh_from_db()
        version = self.root.versions.get()
        self.assertEqual(self.root.source_path, original)
        self.assertTrue(original.exists())
        self.assertEqual(widths(version.source_path), [300, 100])
        self.assertNotIn("Deleted notice", version.content)
        self.assertFalse(version.has_archive_version)

    def test_atomic_append_keeps_editor_operations_and_only_parses_new_scan(self):
        upload = searchable_pdf(self.dirs.scratch_dir / "new.pdf", ["New notice"])
        old = self.root.source_path
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                self.root.pk,
                upload,
                append=True,
                keep_only_current_file=True,
                operations=[{"page": 3}, {"page": 1, "rotate": 180}],
                source_checksum=self.root.checksum,
                remote_ocr=False,
            )
        self.root.refresh_from_db()
        self.assertEqual(widths(self.root.source_path), [300, 100, 100])
        self.assertIn("New notice", self.root.content)
        self.assertNotIn("Deleted notice", self.root.content)
        with pikepdf.open(self.root.source_path) as pdf:
            self.assertEqual(pdf.pages[1].get("/Rotate"), 180)
        self.assertFalse(old.exists())
        call = self.registry.return_value.get_parser_for_file.call_args
        self.assertEqual(call.args[2], upload)
        self.assertFalse(call.kwargs["allow_remote"])
        self.assertEqual(self.root.title, "Water Supply Notices")

    def test_append_can_remove_all_existing_pages(self):
        upload = searchable_pdf(self.dirs.scratch_dir / "new.pdf", ["Only new notice"])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                self.root.pk,
                upload,
                append=True,
                keep_only_current_file=True,
                operations=[],
            )
        self.root.refresh_from_db()
        self.assertEqual(self.root.page_count, 1)
        self.assertIn("Only new notice", self.root.content)
        self.assertNotIn("First notice", self.root.content)

    def test_explicit_historical_selection_uses_searchable_archive(self):
        version = DocumentFactory(
            root_document=self.root,
            mime_type="application/pdf",
            page_count=1,
        )
        searchable_pdf(version.source_path, ["Unsearchable source"])
        archive = self.dirs.archive_dir / "historical.pdf"
        searchable_pdf(archive, ["Archived searchable notice"])
        Document.objects.filter(pk=version.pk).update(
            archive_filename=archive.name,
            archive_checksum=compute_checksum(archive),
        )
        version.refresh_from_db()
        self.edit(
            source_document_id=version.pk,
            source_mode="explicit_selection",
            source_checksum=version.archive_checksum,
            operations=[{"page": 1}],
        )
        self.root.refresh_from_db()
        self.assertIn("Archived searchable notice", self.root.content)
        self.assertNotIn("Unsearchable source", self.root.content)
        self.assertFalse(archive.exists())
        self.assertFalse(Document.global_objects.filter(pk=version.pk).exists())

    def test_stale_editor_checksum_preserves_current_file(self):
        old = self.root.source_path
        with self.assertRaisesMessage(ValueError, "Reload the editor"):
            self.edit(source_checksum="stale-checksum")
        self.root.refresh_from_db()
        self.assertEqual(self.root.source_path, old)
        self.assertEqual(widths(old), [100, 200, 300])
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"old thumbnail")

    def test_stale_append_is_rejected_before_ocr(self):
        upload = searchable_pdf(self.dirs.scratch_dir / "new.pdf", ["New notice"])
        with self.assertRaisesMessage(ValueError, "Reload the editor"):
            update_file(
                self.root.pk,
                upload,
                append=True,
                keep_only_current_file=True,
                operations=[{"page": 1}],
                source_checksum="stale",
            )
        self.registry.assert_not_called()

    def test_editor_task_keeps_stored_source_and_sends_private_progress(self):
        from documents.data_models import ConsumableDocument
        from documents.data_models import DocumentSource
        from documents.tasks import update_document_file

        user = UserFactory()
        grant_global(user, "change_document")
        original = self.root.source_path
        input_doc = ConsumableDocument(
            source=DocumentSource.ApiUpload,
            original_file=original,
            root_document_id=self.root.pk,
        )
        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch("documents.tasks.ProgressManager") as manager,
        ):
            result = update_document_file.run(
                input_doc,
                append=False,
                edit_only=True,
                keep_only_current_file=False,
                operations=[{"page": 3}],
                user_id=user.pk,
                source_checksum=self.root.checksum,
            )
        self.assertEqual(result["document_id"], self.root.pk)
        self.assertTrue(original.exists())
        self.assertTrue(original.parent.exists())
        calls = manager.return_value.__enter__.return_value.send_progress.call_args_list
        self.assertTrue(all(call.kwargs["owner_id"] == user.pk for call in calls))
        self.assertEqual(calls[-1].kwargs["document_id"], self.root.pk)
        self.registry.assert_not_called()

    def test_changed_current_version_rejects_queued_historical_edit(self):
        checksum = self.root.checksum
        newer = DocumentFactory(root_document=self.root, mime_type="application/pdf")
        searchable_pdf(newer.source_path, ["Concurrent new notice"])
        with self.assertRaisesMessage(ValueError, "Reload the editor"):
            self.edit(
                source_document_id=self.root.pk,
                source_mode="explicit_selection",
                source_checksum=checksum,
                expected_current_document_id=self.root.pk,
                expected_current_checksum=checksum,
            )
        self.assertTrue(Document.objects.filter(pk=newer.pk).exists())
        self.assertTrue(newer.source_path.exists())

    def test_current_version_is_checked_again_after_new_scan_ocr(self):
        upload = searchable_pdf(self.dirs.scratch_dir / "new.pdf", ["New notice"])
        concurrent = []

        class ConcurrentParser(FakeParser):
            def parse(parser, path, mime_type, *, produce_archive):
                super().parse(path, mime_type, produce_archive=produce_archive)
                version = DocumentFactory(
                    root_document=self.root,
                    mime_type="application/pdf",
                )
                searchable_pdf(version.source_path, ["Concurrent notice"])
                concurrent.append(version)

        self.registry.return_value.get_parser_for_file.return_value = ConcurrentParser
        with self.assertRaisesMessage(ValueError, "Reload the editor"):
            update_file(
                self.root.pk,
                upload,
                append=True,
                keep_only_current_file=True,
                operations=[{"page": 1}],
                source_checksum=self.root.checksum,
                expected_current_document_id=self.root.pk,
                expected_current_checksum=self.root.checksum,
            )
        self.assertTrue(concurrent[0].source_path.exists())
        self.assertTrue(Document.objects.filter(pk=concurrent[0].pk).exists())
        self.root.refresh_from_db()
        self.assertEqual(widths(self.root.source_path), [100, 200, 300])

    def test_text_extraction_failure_retains_file_and_thumbnail(self):
        old = self.root.source_path
        with mock.patch(
            "documents.file_updates.run_subprocess",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                self.edit()
        self.root.refresh_from_db()
        self.assertEqual(self.root.source_path, old)
        self.assertEqual(self.root.thumbnail_path.read_bytes(), b"old thumbnail")

    def test_explicit_remote_false_overrides_remote_default(self):
        upload = searchable_pdf(self.dirs.scratch_dir / "new.pdf", ["New notice"])
        with mock.patch("documents.file_updates.RemoteOCRConfig") as config:
            config.return_value.remote_ocr_by_default = True
            with self.captureOnCommitCallbacks(execute=True):
                update_file(
                    self.root.pk,
                    upload,
                    append=True,
                    keep_only_current_file=False,
                    remote_ocr=False,
                )
        self.assertFalse(
            self.registry.return_value.get_parser_for_file.call_args.kwargs[
                "allow_remote"
            ],
        )


class PdfEditorUpdateApiTests(DirectoriesMixin, APITestCase):
    def setUp(self):
        self.user = UserFactory()
        grant_global(self.user, "change_document")
        self.root = DocumentFactory(
            owner=self.user,
            mime_type="application/pdf",
            page_count=1,
        )
        searchable_pdf(self.root.source_path, ["Original notice"])
        self.root.thumbnail_path.write_bytes(b"thumbnail")
        Document.objects.filter(pk=self.root.pk).update(
            checksum=compute_checksum(self.root.source_path),
        )
        self.root.refresh_from_db()
        self.client.force_authenticate(self.user)

    def post_edit(self, **kwargs):
        data = {
            "documents": [self.root.pk],
            "operations": [{"page": 1}],
            "update_document": True,
            "keep_only_current_file": False,
        }
        data.update(kwargs)
        return self.client.post("/api/documents/edit_pdf/", data, format="json")

    def test_native_edit_queues_owned_task_with_checksum_without_add_permission(self):
        with mock.patch(
            "documents.tasks.update_document_file.apply_async",
            return_value=SimpleNamespace(id="edit-task"),
        ) as queue:
            response = self.post_edit(include_metadata=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"result": "OK", "task_id": "edit-task"})
        kwargs = queue.call_args.kwargs["kwargs"]
        self.assertTrue(kwargs["edit_only"])
        self.assertEqual(kwargs["user_id"], self.user.pk)
        self.assertEqual(kwargs["source_checksum"], self.root.checksum)
        self.assertEqual(kwargs["source_document_id"], self.root.pk)
        self.assertEqual(kwargs["expected_current_checksum"], self.root.checksum)
        self.assertEqual(kwargs["input_doc"].root_document_id, self.root.pk)

    def test_keep_current_edit_requires_delete_permission(self):
        with mock.patch("documents.tasks.update_document_file.apply_async") as queue:
            response = self.post_edit(keep_only_current_file=True)
        self.assertEqual(response.status_code, 403)
        queue.assert_not_called()

    def test_legacy_edit_returns_the_queued_task_without_nested_result(self):
        with mock.patch(
            "documents.tasks.update_document_file.apply_async",
            return_value=SimpleNamespace(id="legacy-task"),
        ) as queue:
            response = self.client.post(
                "/api/documents/bulk_edit/",
                {
                    "documents": [self.root.pk],
                    "method": "edit_pdf",
                    "parameters": {
                        "operations": [{"page": 1}],
                        "update_document": True,
                        "keep_only_current_file": False,
                    },
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"result": "OK", "task_id": "legacy-task"})
        self.assertEqual(queue.call_args.kwargs["kwargs"]["user_id"], self.user.pk)

    def test_shared_editor_needs_object_delete_permission_for_discarding_history(self):
        other = UserFactory()
        grant_global(other, "change_document", "delete_document")
        assign_perm("change_document", other, self.root)
        self.client.force_authenticate(other)
        with mock.patch(
            "documents.tasks.update_document_file.apply_async",
            return_value=SimpleNamespace(id="edit-task"),
        ) as queue:
            self.assertEqual(
                self.post_edit(keep_only_current_file=True).status_code,
                403,
            )
            self.assertEqual(self.post_edit().status_code, 200)
            assign_perm("delete_document", other, self.root)
            self.assertEqual(
                self.post_edit(keep_only_current_file=True).status_code,
                200,
            )
        self.assertEqual(queue.call_count, 2)

    def test_stale_native_edit_is_not_queued(self):
        with mock.patch("documents.tasks.update_document_file.apply_async") as queue:
            response = self.post_edit(source_checksum="stale")
        self.assertEqual(response.status_code, 400)
        queue.assert_not_called()

    def test_append_queues_json_operations_and_selected_version_atomically(self):
        operations = [{"page": 1, "rotate": 90}]
        with mock.patch(
            "documents.views.update_document_file.apply_async",
            return_value=SimpleNamespace(id="append-task"),
        ) as queue:
            response = self.client.post(
                f"/api/documents/{self.root.pk}/append/",
                {
                    "document": SimpleUploadedFile(
                        "new.pdf",
                        self.root.source_path.read_bytes(),
                    ),
                    "operations": json.dumps(operations),
                    "source_document_id": self.root.pk,
                    "source_mode": "explicit_selection",
                    "source_checksum": self.root.checksum,
                    "keep_only_current_file": "false",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 200)
        kwargs = queue.call_args.kwargs["kwargs"]
        self.assertEqual(kwargs["operations"], operations)
        self.assertEqual(kwargs["source_document_id"], self.root.pk)
        self.assertEqual(kwargs["expected_current_checksum"], self.root.checksum)

    def test_append_rejects_stale_editor_and_another_documents_source(self):
        other = DocumentFactory()
        for extra, expected in [
            ({"source_checksum": "stale"}, 409),
            (
                {"source_document_id": other.pk, "source_mode": "explicit_selection"},
                400,
            ),
            ({"operations": '[{"page": 1, "doc": 1}]'}, 400),
        ]:
            with self.subTest(extra=extra):
                with mock.patch(
                    "documents.views.update_document_file.apply_async",
                ) as queue:
                    response = self.client.post(
                        f"/api/documents/{self.root.pk}/append/",
                        {
                            "document": SimpleUploadedFile(
                                "new.pdf",
                                self.root.source_path.read_bytes(),
                            ),
                            "keep_only_current_file": "false",
                            **extra,
                        },
                        format="multipart",
                    )
                self.assertEqual(response.status_code, expected)
                queue.assert_not_called()

    def test_retention_default_follows_store_setting_for_edit_and_append(self):
        grant_global(self.user, "delete_document")
        for keep_only in [False, True]:
            with (
                self.subTest(keep_only=keep_only),
                self.settings(STORE_ONLY_CURRENT_FILE=keep_only),
            ):
                with mock.patch(
                    "documents.tasks.update_document_file.apply_async",
                    return_value=SimpleNamespace(id="task"),
                ) as queue:
                    data = {
                        "documents": [self.root.pk],
                        "operations": [{"page": 1}],
                        "update_document": True,
                    }
                    self.assertEqual(
                        self.client.post(
                            "/api/documents/edit_pdf/",
                            data,
                            format="json",
                        ).status_code,
                        200,
                    )
                    self.assertEqual(
                        queue.call_args.kwargs["kwargs"]["keep_only_current_file"],
                        keep_only,
                    )

                    response = self.client.post(
                        f"/api/documents/{self.root.pk}/append/",
                        {
                            "document": SimpleUploadedFile(
                                "new.pdf",
                                self.root.source_path.read_bytes(),
                            ),
                        },
                        format="multipart",
                    )
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(
                        queue.call_args.kwargs["kwargs"]["keep_only_current_file"],
                        keep_only,
                    )


@pytest.mark.django_db(transaction=True)
def test_editor_removes_deleted_page_text_from_real_search_index(paperless_dirs):
    from documents.search import get_backend

    root = DocumentFactory(
        mime_type="application/pdf",
        content="retainednotice deletednotice",
        owner=None,
    )
    searchable_pdf(root.source_path, ["retainednotice", "deletednotice"])
    root.thumbnail_path.write_bytes(b"thumbnail")
    backend = get_backend()
    backend.add_or_update(root)
    assert root.pk in backend.search_ids("deletednotice", user=None)
    thumbnail = paperless_dirs.scratch_dir / "edited.webp"
    thumbnail.write_bytes(b"edited thumbnail")
    with (
        mock.patch(
            "documents.file_updates.make_thumbnail_from_pdf",
            return_value=thumbnail,
        ),
        mock.patch("documents.file_updates.get_parser_registry") as registry,
    ):
        update_file(
            root.pk,
            None,
            append=False,
            keep_only_current_file=True,
            operations=[{"page": 1, "rotate": 180}],
        )
    assert root.pk in backend.search_ids("retainednotice", user=None)
    assert root.pk not in backend.search_ids("deletednotice", user=None)
    registry.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("commit_failure", [False, True])
def test_retained_history_can_be_compacted_with_native_signals_connected(
    paperless_dirs,
    commit_failure,
):
    from django.db import OperationalError
    from django.db import connection
    from django.db.models.signals import post_save

    from documents.models import CustomField
    from documents.models import CustomFieldInstance
    from documents.signals.handlers import update_filename_and_move_files

    # Keep the production rename receiver connected. Fixtures with filename=None
    # do not enter its file lock, unlike a genuinely retained editor version.
    post_save.connect(update_filename_and_move_files, sender=Document, weak=False)
    root = DocumentFactory(mime_type="application/pdf", archive_serial_number=43)
    original = searchable_pdf(root.source_path, ["First notice", "Second notice"])
    root.thumbnail_path.write_bytes(b"root thumbnail")
    Document.objects.filter(pk=root.pk).update(
        filename=str(original.relative_to(paperless_dirs.originals_dir)),
        checksum=compute_checksum(original),
        page_count=2,
    )
    root.refresh_from_db()
    Note.objects.create(document=root, note="Keep root note")
    thumbnail = paperless_dirs.scratch_dir / "updated.webp"
    thumbnail.write_bytes(b"updated thumbnail")
    with (
        mock.patch(
            "documents.file_updates.make_thumbnail_from_pdf",
            return_value=thumbnail,
        ),
        mock.patch("documents.file_updates._publish"),
    ):
        update_file(
            root.pk,
            None,
            append=False,
            keep_only_current_file=False,
            operations=[{"page": 2}, {"page": 1}],
        )
        version = root.versions.get()
        assert version.filename is not None
        previous = version.source_path
        previous_thumbnail = version.thumbnail_path
        archive = searchable_pdf(
            paperless_dirs.archive_dir / "version.pdf",
            ["Searchable archive"],
        )
        Document.objects.filter(pk=version.pk).update(
            archive_filename=archive.name,
            archive_checksum=compute_checksum(archive),
        )
        version.refresh_from_db()
        version_note = Note.objects.create(
            document=version,
            note="Obsolete version note",
        )
        field = CustomField.objects.create(
            name="Version field",
            data_type=CustomField.FieldDataType.STRING,
        )
        field_value = CustomFieldInstance.objects.create(
            document=version,
            field=field,
            value_text="obsolete",
        )
        kwargs = {
            "append": False,
            "keep_only_current_file": True,
            "operations": [{"page": 1}],
            "source_document_id": version.pk,
            "source_mode": "explicit_selection",
            "source_checksum": version.archive_checksum,
        }
        if commit_failure:
            with mock.patch.object(
                connection,
                "commit",
                side_effect=OperationalError("commit failed"),
            ):
                with pytest.raises(OperationalError, match="commit failed"):
                    update_file(root.pk, None, **kwargs)
        else:
            update_file(root.pk, None, **kwargs)
    root.refresh_from_db()
    assert root.archive_serial_number == 43
    assert root.notes.get().note == "Keep root note"
    if commit_failure:
        assert root.source_path == original
        assert original.exists()
        assert previous.exists()
        assert archive.exists()
        assert previous_thumbnail.exists()
        assert root.thumbnail_path.read_bytes() == b"root thumbnail"
        assert Document.objects.filter(pk=version.pk).exists()
        assert Note.objects.filter(pk=version_note.pk).exists()
        assert CustomFieldInstance.objects.filter(pk=field_value.pk).exists()
    else:
        assert "Searchable archive" in root.content
        assert not original.exists()
        assert not previous.exists()
        assert not archive.exists()
        assert not previous_thumbnail.exists()
        assert not Document.global_objects.filter(pk=version.pk).exists()
        assert not Note.global_objects.filter(pk=version_note.pk).exists()
        assert not CustomFieldInstance.global_objects.filter(pk=field_value.pk).exists()
        assert list(paperless_dirs.originals_dir.glob("*.pdf")) == [root.source_path]
