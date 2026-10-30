"""Independent adversarial cases retained as regression coverage."""

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from documents.models import Document
from documents.models import MatchingModel
from documents.models import Note
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowTrigger
from documents.ocr_index import PENDING
from documents.ocr_index import REQUEST
from documents.ocr_recovery import reconcile
from documents.page_data import stored_pages
from documents.tasks import bulk_update_documents
from documents.tasks import update_document_in_llm_index
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase as Base


class IndexAdversarialRegressionTests(Base):
    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_attempt_limit_review_note_does_not_reset_the_attempt_limit(self):
        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            self.retry(document)
        kwargs = enqueue.call_args.kwargs["kwargs"]
        now = timezone.now()
        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            side_effect=RuntimeError("embedding unavailable"),
        ) as indexer:
            for attempt in range(5):
                with (
                    mock.patch(
                        "documents.ocr_index.timezone.now",
                        return_value=now + timedelta(minutes=10 * attempt),
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    update_document_in_llm_index.run(**kwargs)
            with (
                mock.patch(
                    "documents.tasks.update_document_in_llm_index.apply_async",
                ) as recovery,
                mock.patch(
                    "documents.ocr_index.timezone.now",
                    return_value=now + timedelta(hours=2),
                ),
            ):
                for _ in range(3):
                    reconcile()
                    update_document_in_llm_index.run(**kwargs)
                recovery.assert_not_called()
            self.assertEqual(indexer.call_count, 5)
        self.assertEqual(
            stored_pages(document)[0]["provenance"][REQUEST]["attempts"],
            5,
        )
        self.assertTrue(
            document.notes.filter(note__startswith="LLM indexing reached").exists(),
        )

    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_deferred_bulk_index_migration_keeps_durable_work(self):
        from paperless_ai import indexing

        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            self.retry(document)
        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            return_value=True,
        ):
            update_document_in_llm_index.run(**enqueue.call_args.kwargs["kwargs"])
        self.assertNotIn(PENDING, stored_pages(document)[0]["provenance"])
        with (
            mock.patch("paperless_ai.indexing.write_store"),
            mock.patch(
                "paperless_ai.indexing._check_and_run_migrations",
                return_value=indexing.MigrationCheckResult.DEFERRED,
            ),
            self.assertRaisesRegex(RuntimeError, "deferred"),
        ):
            bulk_update_documents.run([document.pk])
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as recovery:
            reconcile()
        recovery.assert_called_once()

    def workflow(self, *, updated=False, title="ADDED title"):
        wf = Workflow.objects.create(name="review workflow " + title)
        wf.triggers.add(
            WorkflowTrigger.objects.create(
                type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED
                if updated
                else WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
                matching_algorithm=MatchingModel.MATCH_NONE,
            ),
        )
        wf.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                assign_title=title,
            ),
        )
        return wf

    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_duplicate_index_delivery_obeys_failure_backoff(self):
        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            self.retry(document)
        kwargs = enqueue.call_args.kwargs["kwargs"]
        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            side_effect=RuntimeError("embedding provider unavailable"),
        ) as indexer:
            with self.assertRaises(RuntimeError):
                update_document_in_llm_index.run(**kwargs)
            try:
                update_document_in_llm_index.run(**kwargs)
            except RuntimeError:
                pass
        assert indexer.call_count == 1

    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_migration_deferred_worker_does_not_ack_an_unwritten_index(self):
        from paperless_ai import indexing

        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            self.retry(document)
        with (
            mock.patch("paperless_ai.indexing.build_document_node", return_value=[]),
            mock.patch(
                "paperless_ai.indexing.get_configured_model_name",
                return_value="synthetic",
            ),
            mock.patch("paperless_ai.indexing.write_store") as store,
            mock.patch(
                "paperless_ai.indexing._check_and_run_migrations",
                return_value=indexing.MigrationCheckResult.DEFERRED,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "deferred"):
                update_document_in_llm_index.run(**enqueue.call_args.kwargs["kwargs"])
            store.return_value.__enter__.return_value.upsert_document.assert_not_called()
        assert stored_pages(document)[0]["provenance"].get(PENDING)

    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_lost_worker_redelivery_is_bounded_and_backs_off(self):

        class LostWorker(BaseException):
            pass

        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            self.retry(document)
        kwargs = enqueue.call_args.kwargs["kwargs"]
        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            side_effect=LostWorker,
        ) as indexer:
            for _ in range(8):
                try:
                    update_document_in_llm_index.run(**kwargs)
                except LostWorker:
                    pass
        assert indexer.call_count <= 5
        assert indexer.call_count == 1

    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_new_note_during_embedding_remains_pending_for_fresh_index(self):
        from paperless_ai.embedding import build_llm_index_text

        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            self.retry(document)
        texts = []

        def edit_during_index(root):
            texts.append(build_llm_index_text(root))
            Note.objects.create(
                document=root,
                note="Critical new note after embedding snapshot",
            )
            editor = Document.objects.get(pk=root.pk)
            editor.modified = timezone.now()
            editor.save(update_fields=["modified"])
            return True

        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            side_effect=edit_during_index,
        ):
            update_document_in_llm_index.run(**enqueue.call_args.kwargs["kwargs"])
        assert "Critical new note" not in texts[0]
        state = stored_pages(document)[0]["provenance"]
        assert state.get(PENDING)
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as recovery:
            reconcile()
        assert recovery.call_count == 1
