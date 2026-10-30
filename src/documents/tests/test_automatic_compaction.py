"""Automatic ingestion maintenance must preserve later explicit file history."""

from contextlib import contextmanager
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase
from django.test import override_settings

from documents.file_updates import compact_current_files
from documents.file_updates import compact_imported_document
from documents.file_updates import compact_ingested_root
from documents.file_updates import update_file
from documents.models import Document
from documents.models import Note
from documents.page_data import stored_pages
from documents.tasks import compact_document_files
from documents.tests.test_file_updates import widths
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRFixture
from documents.utils import compute_checksum
from documents.versioning import get_latest_version_for_root
from paperless_testing.factories import DocumentFactory


@override_settings(
    REMOTE_OCR_ENGINE="llamaparse",
    REMOTE_OCR_API_KEY="isolated-provider-key",
    REMOTE_OCR_MODE="always",
    STORE_ONLY_CURRENT_FILE=True,
    OCR_ROTATE_PAGES=False,
)
class AutomaticCompactionTests(RemoteOCRFixture, TransactionTestCase):
    @contextmanager
    def captureOnCommitCallbacks(self, *, execute=False):
        self.assertTrue(connection.get_autocommit())
        yield []

    def consume_pair(self):
        self.failure = None
        with mock.patch("documents.file_updates.compact_imported_document"):
            root, original = self.consume()
        self.assertTrue(root.has_archive_version)
        return root, original

    def append_history(self, root):
        upload = write_pdf(self.dirs.scratch_dir / "retained_append.pdf", [300])
        update_file(root.pk, upload, append=True, keep_only_current_file=False)
        current = get_latest_version_for_root(root)
        self.assertNotEqual(current.pk, root.pk)
        self.assertEqual(widths(current.source_path), [180, 300])
        return current

    def evidence(self, document):
        return list(
            document.parsed_pages.order_by("page").values_list(
                "pk",
                "page",
                "origin",
                "source_page",
                "rotation",
                "text",
                "markdown",
                "layout",
                "file_checksum",
            ),
        )

    def assert_compacted_root_and_retained_history(
        self,
        root,
        child,
        child_bytes,
        child_evidence,
    ):
        root.refresh_from_db()
        child.refresh_from_db()
        self.assertIsNone(root.archive_path)
        self.assertEqual(widths(root.source_path), [180])
        self.assertEqual(child.source_path.read_bytes(), child_bytes)
        self.assertEqual(self.evidence(child), child_evidence)
        self.assertEqual(get_latest_version_for_root(root).pk, child.pk)
        self.assertEqual(
            set(Document.objects.values_list("pk", flat=True)),
            {root.pk, child.pk},
        )
        self.assertEqual(root.archive_serial_number, 1201)

    def test_initial_compaction_preserves_an_append_committed_after_ingestion(self):
        self.failure = None
        child_state = {}

        def append_then_compact(document):
            self.assertTrue(connection.get_autocommit())
            old_paths = [document.source_path, document.archive_path]
            child = self.append_history(document)
            child_state.update(
                document=child,
                data=child.source_path.read_bytes(),
                evidence=self.evidence(child),
                old_paths=old_paths,
            )
            compact_imported_document(document)

        with mock.patch(
            "documents.file_updates.compact_imported_document",
            side_effect=append_then_compact,
        ) as automatic:
            root, _ = self.consume()
        automatic.assert_called_once()
        self.assert_compacted_root_and_retained_history(
            root,
            child_state["document"],
            child_state["data"],
            child_state["evidence"],
        )
        self.assertTrue(all(not path.exists() for path in child_state["old_paths"]))

    @override_settings(FILENAME_FORMAT="{title}")
    def test_queued_retry_preserves_later_history_and_manual_root_metadata(self):
        self.failure = None
        with (
            mock.patch(
                "documents.file_updates.compact_ingested_root",
                side_effect=OSError("synthetic storage error"),
            ),
            mock.patch("documents.tasks.compact_document_files.delay") as enqueue,
        ):
            root, _ = self.consume()
        enqueue.assert_called_once_with(root.pk, root.checksum, root.archive_checksum)
        child = self.append_history(root)
        child_bytes, child_evidence = (
            child.source_path.read_bytes(),
            self.evidence(child),
        )
        root.title = "User selected collection title"
        root.content = "User corrected original transcript"
        root.save(update_fields=["title", "content"])
        Note.objects.create(document=root, note="Retain this collection note")
        compact_document_files.run(*enqueue.call_args.args)
        self.assert_compacted_root_and_retained_history(
            root,
            child,
            child_bytes,
            child_evidence,
        )
        self.assertEqual(root.title, "User selected collection title")
        self.assertEqual(root.content, "User corrected original transcript")
        self.assertEqual(root.notes.get().note, "Retain this collection note")
        self.assertTrue(stored_pages(root)[0]["provenance"]["manual_content"])

    def test_stale_retry_checks_both_original_and_archive_identity(self):
        for changed in ("original", "archive"):
            with self.subTest(changed=changed):
                root, _ = self.consume_pair()
                expected = (root.checksum, root.archive_checksum)
                path = root.source_path if changed == "original" else root.archive_path
                write_pdf(path, [500])
                field = "checksum" if changed == "original" else "archive_checksum"
                Document.objects.filter(pk=root.pk).update(
                    **{field: compute_checksum(path)},
                )
                root.refresh_from_db()
                before = (root.source_path.read_bytes(), root.archive_path.read_bytes())
                with mock.patch("documents.file_updates._install") as install:
                    compact_document_files.run(root.pk, *expected)
                install.assert_not_called()
                root.refresh_from_db()
                self.assertEqual(
                    (root.source_path.read_bytes(), root.archive_path.read_bytes()),
                    before,
                )
                # Each subtest needs its own ASN while retaining its saved files.
                Document.objects.filter(pk=root.pk).update(archive_serial_number=None)

    def test_stale_retry_does_not_compact_a_reparented_document(self):
        root, _ = self.consume_pair()
        expected = (root.checksum, root.archive_checksum)
        parent = DocumentFactory()
        Document.objects.filter(pk=root.pk).update(
            root_document=parent,
            version_index=1,
        )
        with mock.patch("documents.file_updates._install") as install:
            compact_document_files.run(root.pk, *expected)
        install.assert_not_called()
        root.refresh_from_db()
        self.assertEqual(root.root_document_id, parent.pk)
        self.assertTrue(root.source_path.is_file())
        self.assertTrue(root.archive_path.is_file())

    def test_explicit_current_only_compaction_still_promotes_and_prunes_history(self):
        root, _ = self.consume_pair()
        child = self.append_history(root)
        latest_bytes = child.source_path.read_bytes()
        compact_ingested_root(root.pk, root.checksum, root.archive_checksum)
        self.assertTrue(Document.objects.filter(pk=child.pk).exists())
        compact_current_files(root.pk)
        root.refresh_from_db()
        self.assertEqual(root.source_path.read_bytes(), latest_bytes)
        self.assertEqual(widths(root.source_path), [180, 300])
        self.assertFalse(Document.global_objects.filter(pk=child.pk).exists())
        self.assertEqual(
            list(self.dirs.originals_dir.rglob("*.pdf")),
            [root.source_path],
        )
        self.assertEqual(root.archive_serial_number, 1201)

    def test_stale_retry_does_not_compact_a_deleted_document(self):
        root, _ = self.consume_pair()
        expected = (root.checksum, root.archive_checksum)
        original, archive = root.source_path, root.archive_path
        root.delete()
        with mock.patch("documents.file_updates._install") as install:
            compact_document_files.run(root.pk, *expected)
        install.assert_not_called()
        self.assertTrue(Document.deleted_objects.filter(pk=root.pk).exists())
        self.assertTrue(original.is_file())
        self.assertTrue(archive.is_file())

    def test_queued_request_without_revision_identity_cannot_prune_new_history(self):
        root, _ = self.consume_pair()
        child = self.append_history(root)
        before = (
            root.source_path.read_bytes(),
            root.archive_path.read_bytes(),
            child.source_path.read_bytes(),
        )
        with mock.patch("documents.file_updates._install") as install:
            compact_document_files.run(root.pk)
        install.assert_not_called()
        root.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(
            (
                root.source_path.read_bytes(),
                root.archive_path.read_bytes(),
                child.source_path.read_bytes(),
            ),
            before,
        )
        self.assertEqual(get_latest_version_for_root(root).pk, child.pk)
