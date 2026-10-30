"""Restart and broker-loss recovery must retain explicit spending intent."""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.utils import timezone
from filelock import FileLock

from documents.ocr_queue import pending_pages
from documents.ocr_recovery import RETRY
from documents.ocr_recovery import reconcile
from documents.ocr_recovery import store_requests
from documents.page_data import stored_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from paperless.parsers.llamaparse import LlamaParseDocumentParser


class InterruptedWorker(BaseException):
    """Model termination outside ordinary provider error handling."""


class OCRRecoveryTests(RemoteOCRTestCase):
    def test_repeated_first_page_crashes_do_not_exhaust_untouched_pages(self):
        document, _ = self.consume(
            upload=write_pdf(settings.CONSUMPTION_DIR / "crashes.pdf", [180, 240, 300]),
        )
        self.interrupt(document)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            for attempt in range(1, 6):
                reconcile()
                pages = stored_pages(document)
                self.assertEqual(pages[0]["provenance"][RETRY]["attempts"], attempt)
                self.assertEqual(
                    [page["provenance"][RETRY]["attempts"] for page in pages[1:]],
                    [0, 0],
                )
                if attempt == 5:
                    break
                for page in pages:
                    page["provenance"][RETRY]["not_before"] = (
                        timezone.now() - timedelta(seconds=1)
                    ).isoformat()
                    page["provenance"][RETRY]["sent_at"] = None
                store_requests(document, pages)
                reconcile()
                with (
                    mock.patch.object(
                        LlamaParseDocumentParser,
                        "_request_result",
                        side_effect=InterruptedWorker,
                    ),
                    mock.patch("documents.ocr_recovery.finish"),
                    self.assertRaises(InterruptedWorker),
                ):
                    update_document_content_maybe_archive_file.run(
                        **enqueue.call_args.kwargs,
                    )
            self.failure = None
            with self.captureOnCommitCallbacks(execute=True):
                update_document_content_maybe_archive_file.run(
                    **enqueue.call_args.kwargs,
                )
        document.refresh_from_db()
        pages = stored_pages(document)
        self.assertEqual(
            [page["provenance"].get("ocr_status") == "pending" for page in pages],
            [True, False, False],
        )
        self.assertEqual(
            [page["provenance"][RETRY]["attempts"] for page in pages],
            [5, 1, 1],
        )

    def test_restart_retains_all_pages_of_an_unbounded_direct_request(self):
        upload = write_pdf(
            settings.CONSUMPTION_DIR / "three_pages.pdf",
            [180, 240, 300],
        )
        document, _ = self.consume(upload=upload)
        self.interrupt(document)
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            self.assertEqual(reconcile(), 1)
            with self.captureOnCommitCallbacks(execute=True):
                update_document_content_maybe_archive_file.run(
                    **enqueue.call_args.kwargs,
                )
            document.refresh_from_db()
            self.assertEqual(len(pending_pages(document)), 1)
            pages = stored_pages(document)
            pages[0]["provenance"][RETRY]["not_before"] = (
                timezone.now() - timedelta(seconds=1)
            ).isoformat()
            store_requests(document, pages)
            self.assertEqual(reconcile(), 1)
            with self.captureOnCommitCallbacks(execute=True):
                update_document_content_maybe_archive_file.run(
                    **enqueue.call_args.kwargs,
                )
        document.refresh_from_db()
        self.assertFalse(pending_pages(document))
        self.assertEqual(self.create_requests, 4)

    def interrupt(self, document, *, bounded=False):
        def interrupted(parser, *args):
            saved = pending_pages(document)[0]["provenance"][RETRY]
            self.assertTrue(saved["running"])
            self.assertEqual(saved["attempts"], 1)
            raise InterruptedWorker

        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_request_result",
                autospec=True,
                side_effect=interrupted,
            ),
            # A terminated process cannot execute its finally cleanup.
            mock.patch("documents.ocr_recovery.finish"),
            self.assertRaises(InterruptedWorker),
        ):
            if bounded:
                self.retry(document, max_pages=1)
            else:
                update_document_content_maybe_archive_file(document.pk, remote_ocr=True)

    def test_direct_reprocess_survives_restart_with_backoff_and_bounded_attempts(self):
        document, original = self.consume()
        self.interrupt(document)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            self.assertEqual(reconcile(), 0)
            enqueue.assert_not_called()
            pages = stored_pages(document)
            state = pages[0]["provenance"][RETRY]
            self.assertTrue(state["requested"])
            self.assertFalse(state["running"])
            state["not_before"] = (timezone.now() - timedelta(seconds=1)).isoformat()
            store_requests(document, pages)
            self.assertEqual(reconcile(), 1)
            enqueue.assert_called_once_with(
                document_id=document.pk,
                remote_ocr=True,
                pending_only=True,
            )
            self.failure = None
            with self.captureOnCommitCallbacks(execute=True):
                update_document_content_maybe_archive_file.run(
                    **enqueue.call_args.kwargs,
                )
        document.refresh_from_db()
        self.assertFalse(pending_pages(document))
        self.assertEqual(document.archive_serial_number, 1201)
        self.assertEqual(
            self.create_requests,
            2,
        )  # Initial quota failure + recovered page.
        self.assertNotEqual(document.source_path.read_bytes(), original)

    def test_interrupted_cli_budget_requires_another_explicit_budget(self):
        document, original = self.consume()
        self.interrupt(document, bounded=True)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            self.assertEqual(reconcile(), 0)
            self.assertEqual(reconcile(), 0)
            enqueue.assert_not_called()
        state = pending_pages(document)[0]["provenance"][RETRY]
        self.assertFalse(state["running"])
        self.assertFalse(state["requested"])
        self.assertTrue(
            document.notes.filter(note__contains="new explicit retry").exists(),
        )
        self.assertEqual(document.source_path.read_bytes(), original)

    def test_restart_limit_never_starts_a_sixth_attempt(self):
        document, _ = self.consume()
        pages = stored_pages(document)
        pages[0]["provenance"][RETRY] = {"running": True, "attempts": 5}
        store_requests(document, pages)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            self.assertEqual(reconcile(), 0)
            enqueue.assert_not_called()
        self.assertFalse(pending_pages(document)[0]["provenance"][RETRY]["requested"])

    def test_reconcile_cannot_touch_an_active_attempt(self):
        document, _ = self.consume()
        pages = stored_pages(document)
        pages[0]["provenance"][RETRY] = {"running": True, "attempts": 1}
        store_requests(document, pages)
        before = stored_pages(document)
        with FileLock(settings.DATA_DIR / f"document-file-{document.pk}.lock"):
            self.assertEqual(reconcile(), 0)
        self.assertEqual(stored_pages(document), before)

    def test_provider_failure_does_not_create_an_automatic_paid_retry_loop(self):
        document, _ = self.consume()
        self.assertEqual(self.retry(document), 0)
        calls = self.create_requests
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            self.assertEqual(reconcile(), 0)
            enqueue.assert_not_called()
        self.assertEqual(self.create_requests, calls)

    def test_lost_delivery_is_republished_but_duplicate_messages_do_not_recharge(self):
        document, _ = self.consume()
        with (
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
                side_effect=ConnectionError,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            document.tags.remove(document.tags.get(name="ocr-fail"))
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            self.assertEqual(reconcile(), 1)
            self.assertEqual(reconcile(), 0)
            enqueue.assert_called_once()
        self.failure = None
        with self.captureOnCommitCallbacks(execute=True):
            update_document_content_maybe_archive_file.run(**enqueue.call_args.kwargs)
        calls = self.create_requests
        update_document_content_maybe_archive_file.run(**enqueue.call_args.kwargs)
        self.assertEqual(self.create_requests, calls)
