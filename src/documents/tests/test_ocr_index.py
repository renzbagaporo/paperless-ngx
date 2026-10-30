"""Index complete OCR independently of metadata workflows and broker health."""

from unittest import mock

from django.test import override_settings

from documents.deferred_ingestion import CONTEXT
from documents.file_updates import _publish
from documents.file_updates import update_file
from documents.ocr_index import PENDING
from documents.ocr_recovery import reconcile
from documents.page_data import stored_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tasks import update_document_in_llm_index
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase


@override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
class OCRIndexTests(RemoteOCRTestCase):
    def setUp(self):
        super().setUp()
        self.index = mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ).start()
        self.direct = mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
        ).start()

    @override_settings(ARCHIVE_FILE_GENERATION="always")
    def test_completed_full_reprocess_queues_one_index_update(self):
        self.failure = None
        document, _ = self.consume()
        self.index.reset_mock()
        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        self.index.assert_called_once()
        self.direct.assert_not_called()

    def test_reprocess_recovery_indexes_corrected_content_once(self):
        self.failure = None
        document, _ = self.consume()
        self.index.reset_mock()
        self.failure = 402
        with mock.patch(
            "documents.tasks.get_parser_registry",
            return_value=self.registry.return_value,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        self.index.assert_not_called()
        self.failure = None
        self.response_text = "Corrected searchable content"
        self.retry(document)
        self.index.assert_called_once()
        self.direct.assert_not_called()
        queued = self.index.call_args.kwargs["kwargs"]["document"]
        self.assertEqual(queued.get_effective_content(), "Corrected searchable content")
        reconcile()
        self.retry(document)
        self.index.assert_called_once()

    def test_partial_pages_wait_for_completion_and_clean_cron_does_not_reembed(self):
        upload = write_pdf(self.dirs.consumption_dir / "two_pages.pdf", [180, 300])
        document, _ = self.consume(upload=upload)
        self.index.assert_not_called()
        self.failure = None
        self.retry(document, max_pages=1)
        self.index.assert_not_called()
        self.retry(document)
        self.index.assert_called_once()
        reconcile()
        reconcile()
        self.index.assert_called_once()

    def test_broker_failure_keeps_durable_index_work_for_reconciliation(self):
        document, _ = self.consume()
        self.failure = None
        self.index.side_effect = ConnectionError("isolated index broker failure")
        self.retry(document)
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        self.index.reset_mock(side_effect=True)
        reconcile()
        self.index.assert_called_once()
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        update_document_in_llm_index.run(**self.index.call_args.kwargs["kwargs"])
        self.assertNotIn(PENDING, stored_pages(document)[0]["provenance"])
        reconcile()
        self.index.assert_called_once()

    def test_removing_final_pending_page_updates_index_once(self):
        self.failure = None
        document, _ = self.consume()
        self.index.reset_mock()
        self.failure = 402
        upload = write_pdf(self.dirs.scratch_dir / "append.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        self.index.assert_not_called()
        with (
            mock.patch("documents.file_updates._publish", new=_publish),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1}],
            )
        self.index.assert_called_once()
        reconcile()
        self.index.assert_called_once()

    def test_manual_metadata_and_reviewed_workflows_do_not_skip_indexing(self):
        for index, review in enumerate((False, True)):
            with self.subTest(review=review):
                self.failure = 402
                document, _ = self.consume(asn=1201 + index)
                self.index.reset_mock()
                self.failure = None
                if review:
                    with mock.patch(
                        "documents.signals.document_consumption_finished.send",
                        side_effect=ValueError("isolated workflow failure"),
                    ):
                        self.retry(document)
                else:
                    document.title = "My chosen title"
                    document.save()
                    self.retry(document)
                self.assertEqual(
                    stored_pages(document)[0]["provenance"][CONTEXT]["state"],
                    "review" if review else "skipped",
                )
                self.index.assert_called_once()

    @override_settings(AI_ENABLED=False)
    def test_disabled_index_retires_requests_without_a_cron_loop(self):
        document, _ = self.consume()
        self.failure = None
        self.retry(document)
        self.assertNotIn(PENDING, stored_pages(document)[0]["provenance"])
        reconcile()
        reconcile()
        self.index.assert_not_called()

    def test_historical_pending_evidence_cannot_reembed_current_document(self):
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "historical_append.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=False)
        self.failure = None
        self.retry(document)
        self.index.assert_called_once()
        reconcile()
        reconcile()
        self.index.assert_called_once()
