from datetime import date
from unittest import mock

from django.test import override_settings

from documents.deferred_ingestion import CONTEXT
from documents.file_updates import update_file
from documents.models import Document
from documents.models import MatchingModel
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.page_data import stored_pages
from documents.tasks import apply_ai_suggestions
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from documents.versioning import get_latest_version_for_root
from paperless_testing.factories import CorrespondentFactory


class DeferredIngestionTests(RemoteOCRTestCase):
    def workflow(
        self,
        *,
        ai=False,
        match="",
        title="Named after OCR",
        trigger_type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
    ):
        action = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.APPLY_AI_SUGGESTIONS
            if ai
            else WorkflowAction.WorkflowActionType.ASSIGNMENT,
            assign_title=title if not ai else "",
            ai_suggestion_fields=[WorkflowAction.AISuggestionField.TITLE]
            if ai
            else None,
            ai_overwrite_existing=True,
        )
        workflow = Workflow.objects.create(
            name=f"Document automation {Workflow.objects.count()}",
        )
        workflow.triggers.add(
            WorkflowTrigger.objects.create(
                type=trigger_type,
                matching_algorithm=MatchingModel.MATCH_LITERAL
                if match
                else MatchingModel.MATCH_NONE,
                match=match,
            ),
        )
        workflow.actions.add(action)
        return workflow, action

    def state(self, document):
        document.refresh_from_db()
        return stored_pages(get_latest_version_for_root(document))[0]["provenance"][
            CONTEXT
        ]["state"]

    def test_content_filtered_added_workflow_runs_only_after_ocr_completes(self):
        workflow, _ = self.workflow(match="Water Supply")
        document, _ = self.consume()
        self.assertEqual(document.title, "Water Supply Notices")
        self.assertFalse(WorkflowRun.objects.filter(workflow=workflow).exists())
        self.failure = None
        self.retry(document)
        self.assertEqual(document.title, "Named after OCR")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)
        self.assertEqual(self.state(document), "completed")
        self.retry(document)
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)

    @override_settings(OCR_VALIDATE_PRINTED_DATES=True)
    def test_printed_date_selection_resumes_after_pending_upload(self):
        document, _ = self.consume()
        self.failure = None
        self.response_text = "Issued: 2024-06-28"
        self.retry(document)
        self.assertEqual(document.created, date(2024, 6, 28))

    @override_settings(OCR_VALIDATE_PRINTED_DATES=True)
    def test_explicit_uploader_date_is_retained(self):
        document, _ = self.consume(created=date(2020, 1, 2))
        self.failure = None
        self.response_text = "Issued: 2024-06-28"
        self.retry(document)
        self.assertEqual(document.created, date(2020, 1, 2))

    def test_user_edits_while_pending_skip_automatic_metadata_mutations(self):
        workflow, _ = self.workflow()
        document, _ = self.consume()
        document.title = "My deliberately chosen title"
        document.created = date(2019, 2, 3)
        document.save()
        self.failure = None
        self.retry(document)
        self.assertEqual(document.title, "My deliberately chosen title")
        self.assertEqual(document.created, date(2019, 2, 3))
        self.assertEqual(self.state(document), "skipped")
        self.assertFalse(WorkflowRun.objects.filter(workflow=workflow).exists())

    def test_classifier_rules_can_see_completed_ocr(self):
        correspondent = CorrespondentFactory(
            match="Water Supply",
            matching_algorithm=MatchingModel.MATCH_LITERAL,
        )
        document, _ = self.consume()
        self.assertIsNone(document.correspondent_id)
        self.failure = None
        self.retry(document)
        self.assertEqual(document.correspondent_id, correspondent.pk)

    @override_settings(AI_ENABLED=True)
    def test_configured_ai_title_job_is_deferred_and_then_applied(self):
        _, _ = self.workflow(ai=True)
        with mock.patch(
            "documents.tasks.apply_ai_suggestions.delay_on_commit",
        ) as enqueue:
            document, _ = self.consume()
            enqueue.assert_not_called()
            self.failure = None
            self.retry(document)
            enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        with (
            mock.patch(
                "documents.workflows.ai.get_ai_document_classification",
                return_value={"title": "Suggested water notices"},
            ) as qwen,
            mock.patch("documents.tasks.index_document.delay"),
        ):
            apply_ai_suggestions.run(**kwargs)
        document.refresh_from_db()
        self.assertEqual(document.title, "Suggested water notices")
        qwen.assert_called_once()

    @override_settings(AI_ENABLED=True)
    def test_ai_job_preserves_title_edited_during_qwen_request(self):
        _, _ = self.workflow(ai=True)
        with mock.patch(
            "documents.tasks.apply_ai_suggestions.delay_on_commit",
        ) as enqueue:
            document, _ = self.consume()
            self.failure = None
            self.retry(document)

        def edit_during_qwen(*args):
            latest = Document.objects.get(pk=document.pk)
            latest.title = "User title during Qwen"
            latest.save()
            return {"title": "Stale automatic title"}

        with (
            mock.patch(
                "documents.workflows.ai.get_ai_document_classification",
                side_effect=edit_during_qwen,
            ),
            mock.patch("documents.tasks.index_document.delay"),
        ):
            apply_ai_suggestions.run(**enqueue.call_args.kwargs)
        document.refresh_from_db()
        self.assertEqual(document.title, "User title during Qwen")

    def test_disabled_workflow_or_deleted_action_is_not_resurrected(self):
        workflow, action = self.workflow()
        document, _ = self.consume()
        workflow.enabled = False
        workflow.save()
        action.delete()
        self.failure = None
        self.retry(document)
        self.assertEqual(document.title, "Water Supply Notices")
        self.assertFalse(WorkflowRun.objects.exists())

    def test_partial_retry_and_first_page_removal_keep_deferred_intent(self):
        workflow, _ = self.workflow()
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "extra.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 2}],
            )
        self.assertEqual(self.state(document), "pending")
        self.failure = None
        self.retry(document)
        self.assertEqual(document.title, "Named after OCR")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)

    def test_history_mode_rules_use_latest_content_and_mutate_root_only(self):
        self.workflow(match="Water Supply")
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "extra.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=False)
        self.failure = None
        self.retry(document)
        self.assertEqual(document.content, "")
        self.assertIn("Water Supply", get_latest_version_for_root(document).content)
        self.assertEqual(document.title, "Named after OCR")
        self.assertEqual(self.state(document), "completed")
