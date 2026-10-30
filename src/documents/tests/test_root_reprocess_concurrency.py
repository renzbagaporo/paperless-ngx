"""Root requests select their current revision under the file lock."""

from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import override_settings
from filelock import FileLock as RealFileLock

from documents.file_updates import update_file
from documents.models import Document
from documents.ocr_queue import pending_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from documents.versioning import get_latest_version_for_root


@override_settings(ARCHIVE_FILE_GENERATION="always")
class RootReprocessRaceTests(RemoteOCRTestCase):
    def append_pending(self, root, width, name):
        self.failure = 402
        upload = write_pdf(self.dirs.scratch_dir / name, [width])
        result = update_file(root.pk, upload, append=True, keep_only_current_file=False)
        current = get_latest_version_for_root(Document.objects.get(pk=root.pk))
        self.assertNotEqual(current.pk, root.pk)
        self.assertEqual(result["document_id"], root.pk)
        self.failure = None
        return current

    def inject_before_root_lock(self, root, callback):
        expected = settings.DATA_DIR / f"document-file-{root.pk}.lock"
        invocations = []

        class BeforeAcquire:
            def __init__(self, lock):
                self.lock = lock

            def __enter__(self):
                invocations.append(True)
                callback()
                return self.lock.__enter__()

            def __exit__(self, *args):
                return self.lock.__exit__(*args)

        def factory(path, *args, **kwargs):
            lock = RealFileLock(path, *args, **kwargs)
            if Path(path) == expected and (not invocations):
                return BeforeAcquire(lock)
            return lock

        return (factory, invocations)

    def test_completed_root_request_reselects_new_pending_revision_after_wait(self):
        self.failure = None
        root, _ = self.consume()
        before_content = root.content
        self.assertFalse(pending_pages(root))
        injected = []

        def another_writer():
            child = self.append_pending(root, 300, "append_during_wait.pdf")
            injected.append(child.pk)
            self.assertEqual(len(pending_pages(child)), 1)
            self.response_text = "Latest second page OCR"

        factory, invocations = self.inject_before_root_lock(root, another_writer)
        with (
            mock.patch("documents.tasks.FileLock", side_effect=factory),
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file.run(root.pk, remote_ocr=True)
        root.refresh_from_db()
        latest = get_latest_version_for_root(root)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(latest.pk, injected[0])
        self.assertFalse(pending_pages(latest))
        self.assertEqual(root.content, before_content)

    def test_pending_root_request_reselects_child_created_while_waiting(self):
        self.failure = None
        root, _ = self.consume()
        with self.captureOnCommitCallbacks(execute=True):
            first = self.append_pending(root, 240, "first_pending.pdf")
        self.assertEqual(len(pending_pages(first)), 1)
        injected = []

        def another_writer():
            newest = self.append_pending(root, 300, "second_pending.pdf")
            injected.append(newest.pk)
            self.assertNotEqual(newest.pk, first.pk)
            self.assertEqual(len(pending_pages(newest)), 2)

        factory, invocations = self.inject_before_root_lock(root, another_writer)
        with (
            mock.patch("documents.ocr_queue.FileLock", side_effect=factory),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file.run(root.pk, remote_ocr=True)
        root.refresh_from_db()
        latest = get_latest_version_for_root(root)
        first.refresh_from_db()
        self.assertEqual(len(invocations), 1)
        self.assertEqual(latest.pk, injected[0])
        self.assertFalse(pending_pages(latest))
