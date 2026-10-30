"""Exercise real commits: on_commit ordering is part of the ingestion contract."""

from contextlib import contextmanager
from copy import deepcopy
from io import StringIO
from unittest import mock

import pikepdf
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.test import override_settings

from documents.deferred_ingestion import CONTEXT
from documents.file_updates import _publish
from documents.file_updates import update_file
from documents.models import Document
from documents.models import MatchingModel
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.ocr_recovery import RETRY
from documents.ocr_recovery import reconcile
from documents.page_data import stored_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRFixture
from paperless_testing.auditlog import preserve_auditlog_registry
from paperless_testing.dirs import paperless_environment
from paperless_testing.factories import CorrespondentFactory


@override_settings(
    REMOTE_OCR_ENGINE="llamaparse",
    REMOTE_OCR_API_KEY="isolated-provider-key",
    REMOTE_OCR_MODE="always",
    STORE_ONLY_CURRENT_FILE=True,
    OCR_ROTATE_PAGES=False,
)
class OCRLifecycleTransactionTests(RemoteOCRFixture, TransactionTestCase):
    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_initial_success_indexes_once_across_real_ingestion_and_compaction_commits(
        self,
    ):
        self.failure = None
        updated = self.workflow(updated=True, title="Must not run on maintenance")
        with (
            mock.patch("documents.file_updates._publish", new=_publish),
            mock.patch(
                "documents.tasks.update_document_in_llm_index.apply_async",
            ) as enqueue,
        ):
            document, _ = self.consume()
        enqueue.assert_called_once()
        self.assertIsNone(document.archive_path)
        self.assertFalse(WorkflowRun.objects.filter(workflow=updated).exists())

    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_born_digital_ingestion_indexes_once_without_archive_or_remote_ocr(self):
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            document, _ = self.consume(text="Native digital searchable content " * 20)
        enqueue.assert_called_once()
        self.assertIsNone(document.archive_path)
        self.assertEqual(self.create_requests, 0)

    def restore_export(self, target):
        # This class uses the Django test database and isolated media fixtures.
        with preserve_auditlog_registry():
            call_command("flush", interactive=False, verbosity=0)
            call_command(
                "document_importer",
                "--no-progress-bar",
                target,
                skip_checks=True,
                stdout=StringIO(),
            )

    def test_restore_export_preserves_audit_options_receivers_and_disabled_models(self):
        from auditlog.models import LogEntry
        from auditlog.registry import auditlog

        from documents.models import CustomField
        from documents.models import CustomFieldInstance
        from documents.models import Note
        from documents.models import Tag

        self.failure = None
        self.consume()
        target = settings.SCRATCH_DIR / "audit_registry_export"
        target.mkdir()
        call_command("document_exporter", target, skip_checks=True, stdout=StringIO())
        with preserve_auditlog_registry():
            auditlog.unregister(Note)
            auditlog.unregister(Document)
            auditlog.register(
                Document,
                exclude_fields=["content_length", "modified"],
                mapping_fields={"title": "Imported title"},
                mask_fields=["original_filename"],
                m2m_fields={"tags"},
                serialize_data=True,
                serialize_kwargs={"fields": ["title"]},
                serialize_auditlog_fields_only=True,
            )
            auditlog.register(CustomFieldInstance)
            expected_registry = deepcopy(auditlog._registry)
            expected_m2m = auditlog._m2m_signals[Document]["tags"]
            with paperless_environment():
                self.restore_export(target)
                self.assertEqual(auditlog._registry, expected_registry)
                self.assertIs(auditlog._m2m_signals[Document]["tags"], expected_m2m)
                self.assertFalse(auditlog.contains(Note))
                sentinel = Document.objects.create(
                    title="Audit sentinel",
                    content="Synthetic content",
                    checksum="audit-registry-sentinel",
                    mime_type="application/pdf",
                )
                self.assertEqual(
                    LogEntry.objects.get_for_object(sentinel)
                    .filter(
                        action=LogEntry.Action.CREATE,
                    )
                    .count(),
                    1,
                )
                sentinel.title = "Updated after import"
                sentinel.save(update_fields=["title"])
                self.assertEqual(
                    LogEntry.objects.get_for_object(sentinel)
                    .filter(
                        action=LogEntry.Action.UPDATE,
                    )
                    .count(),
                    1,
                )
                sentinel.tags.add(Tag.objects.create(name="Audit sentinel tag"))
                self.assertTrue(
                    LogEntry.objects.get_for_object(sentinel)
                    .filter(
                        changes__has_key="tags",
                    )
                    .exists(),
                )
                field = CustomField.objects.create(
                    name="Audit sentinel field",
                    data_type=CustomField.FieldDataType.STRING,
                )
                value = CustomFieldInstance.objects.create(
                    document=sentinel,
                    field=field,
                    value_text="Synthetic value",
                )
                self.assertEqual(LogEntry.objects.get_for_object(value).count(), 1)
                note = Note.objects.create(document=sentinel, note="Disabled audit")
                self.assertFalse(LogEntry.objects.get_for_object(note).exists())

    def test_real_pending_context_survives_export_import_and_finishes_once(self):
        workflow = self.workflow()
        document, _ = self.consume()
        document_id, workflow_id = document.pk, workflow.pk
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
            side_effect=ConnectionError,
        ):
            document.tags.remove(document.tags.get(name="ocr-fail"))
        self.assertTrue(stored_pages(document)[0]["provenance"][RETRY]["requested"])
        target = settings.SCRATCH_DIR / "pending_export"
        target.mkdir()
        call_command("document_exporter", target, skip_checks=True, stdout=StringIO())
        before = stored_pages(document)[0]["provenance"][CONTEXT]
        with paperless_environment():
            self.restore_export(target)
            restored = Document.objects.get(pk=document_id)
            self.assertEqual(stored_pages(restored)[0]["provenance"][CONTEXT], before)
            self.failure = None
            with mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue:
                self.assertEqual(reconcile(), 1)
                update_document_content_maybe_archive_file.run(
                    **enqueue.call_args.kwargs,
                )
            restored.refresh_from_db()
            self.retry(restored)
            self.assertEqual(restored.title, "Classified notice")
            self.assertEqual(
                WorkflowRun.objects.filter(workflow_id=workflow_id).count(),
                1,
            )

    def test_review_context_does_not_replay_actions_after_export_import(self):
        workflow = self.workflow(title=None)
        workflow.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.WEBHOOK,
            ),
        )
        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.signals.handlers.execute_webhook_action",
            side_effect=ValueError("isolated action failure"),
        ):
            self.retry(document)
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "review",
        )
        target = settings.SCRATCH_DIR / "review_export"
        target.mkdir()
        call_command("document_exporter", target, skip_checks=True, stdout=StringIO())
        with paperless_environment():
            self.restore_export(target)
            restored = Document.objects.get(pk=document.pk)
            with mock.patch(
                "documents.signals.handlers.execute_webhook_action",
            ) as webhook:
                reconcile()
                self.retry(restored)
            webhook.assert_not_called()
            self.assertEqual(
                stored_pages(restored)[0]["provenance"][CONTEXT]["state"],
                "review",
            )

    def test_completed_context_does_not_replay_after_export_import(self):
        workflow = self.workflow()
        document, _ = self.consume()
        self.failure = None
        self.retry(document)
        document_id, workflow_id = document.pk, workflow.pk
        target = settings.SCRATCH_DIR / "completed_export"
        self.assertEqual(WorkflowRun.objects.filter(workflow_id=workflow_id).count(), 1)
        target.mkdir()
        call_command("document_exporter", target, skip_checks=True, stdout=StringIO())
        calls = self.create_requests
        with paperless_environment():
            self.restore_export(target)
            restored = Document.objects.get(pk=document_id)
            self.retry(restored)
            reconcile()
            # Upstream exports omit the transient WorkflowRun table. The
            # durable terminal context must still prevent another action.
            self.assertEqual(
                WorkflowRun.objects.filter(workflow_id=workflow_id).count(),
                0,
            )
            self.assertEqual(
                stored_pages(restored)[0]["provenance"][CONTEXT]["state"],
                "completed",
            )
            self.assertEqual(self.create_requests, calls)

    @contextmanager
    def captureOnCommitCallbacks(self, *, execute=False):
        self.assertTrue(connection.get_autocommit())
        yield []  # Callbacks execute through real database commits.

    def workflow(self, *, updated=False, title="Classified notice", order=0):
        workflow = Workflow.objects.create(
            name=f"Automation {Workflow.objects.count()}",
            order=order,
        )
        workflow.triggers.add(
            WorkflowTrigger.objects.create(
                type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED
                if updated
                else WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
                matching_algorithm=MatchingModel.MATCH_LITERAL,
                match="Water Supply",
            ),
        )
        if title:
            workflow.actions.add(
                WorkflowAction.objects.create(
                    type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                    assign_title=title,
                ),
            )
        return workflow

    def test_partial_progress_defers_added_and_updated_actions_until_complete(self):
        added = self.workflow()
        updated = self.workflow(updated=True, title="Updated notice")
        upload = write_pdf(settings.CONSUMPTION_DIR / "two_pages.pdf", [180, 300])
        document, _ = self.consume(upload=upload)
        self.failure = None
        with mock.patch("documents.file_updates._publish", new=_publish):
            self.retry(document, max_pages=1)
            self.assertFalse(
                WorkflowRun.objects.filter(workflow__in=[added, updated]).exists(),
            )
            self.retry(document)
            self.retry(document)
        self.assertEqual(WorkflowRun.objects.filter(workflow=added).count(), 1)
        self.assertEqual(WorkflowRun.objects.filter(workflow=updated).count(), 0)
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "completed",
        )

    def test_append_owes_one_updated_event_after_all_pages_complete(self):
        self.failure = None
        document, _ = self.consume()
        updated = self.workflow(updated=True)
        self.failure = 402
        upload = write_pdf(settings.CONSUMPTION_DIR / "added_pages.pdf", [180, 300])
        with mock.patch("documents.file_updates._publish", new=_publish):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
            self.failure = None
            self.retry(document, max_pages=1)
            self.assertFalse(WorkflowRun.objects.filter(workflow=updated).exists())
            self.retry(document)
            self.retry(document)
        self.assertEqual(WorkflowRun.objects.filter(workflow=updated).count(), 1)

    @override_settings(FILENAME_FORMAT="{title}/{correspondent}")
    def test_email_and_webhook_attachments_survive_classifier_and_workflow_renames(
        self,
    ):
        CorrespondentFactory(
            name="Water utility",
            match="Water Supply",
            matching_algorithm=MatchingModel.MATCH_LITERAL,
        )
        self.workflow(order=0)
        attachments = self.workflow(title=None, order=1)
        attachments.actions.add(
            WorkflowAction.objects.create(type=WorkflowAction.WorkflowActionType.EMAIL),
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.WEBHOOK,
            ),
        )
        document, _ = self.consume()
        paths = []

        def open_attachment(*args):
            self.assertTrue(connection.get_autocommit())
            attachment = args[4]
            self.assertTrue(attachment.is_file())
            with pikepdf.open(attachment) as pdf:
                self.assertEqual(len(pdf.pages), 1)
            paths.append(attachment)

        self.failure = None
        with (
            mock.patch(
                "documents.signals.handlers.execute_email_action",
                side_effect=open_attachment,
            ) as email,
            mock.patch(
                "documents.signals.handlers.execute_webhook_action",
                side_effect=open_attachment,
            ) as webhook,
        ):
            self.retry(document)
        email.assert_called_once()
        webhook.assert_called_once()
        self.assertTrue(document.source_path.is_file())
        self.assertEqual(document.title, "Classified notice")
        self.assertTrue(all(not path.exists() for path in paths))

    @override_settings(FILENAME_FORMAT="{title}")
    def test_workflow_exception_after_rename_keeps_file_and_requires_review(self):
        self.workflow(order=0)
        second = self.workflow(title=None, order=1)
        second.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.WEBHOOK,
            ),
        )
        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.signals.handlers.execute_webhook_action",
            side_effect=ValueError("isolated action failure"),
        ) as webhook:
            self.retry(document)
            reconcile()
            reconcile()
            webhook.assert_called_once()
        document.refresh_from_db()
        self.assertEqual(document.title, "Classified notice")
        self.assertTrue(document.source_path.is_file())
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "review",
        )
        self.assertTrue(document.notes.filter(note__contains="needs review").exists())
