"""Cross-feature defects reproduced by the second independent review round."""

from datetime import date
from datetime import timedelta
from unittest import mock

import pikepdf
from django.test import override_settings
from django.utils import timezone

from documents.bulk_edit import modify_tags
from documents.bulk_edit import remove_tag
from documents.deferred_ingestion import CONTEXT
from documents.models import Document
from documents.models import MatchingModel
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.ocr_index import PENDING
from documents.ocr_index import REQUEST
from documents.ocr_queue import pending_pages
from documents.ocr_recovery import reconcile
from documents.page_data import stored_pages
from documents.tasks import bulk_update_documents
from documents.tasks import update_document_content_maybe_archive_file
from documents.tasks import update_document_in_llm_index
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from documents.utils import compute_checksum
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory


class OCRReviewRegressionTests(RemoteOCRTestCase):
    def legacy(self, content=""):
        document = DocumentFactory(
            mime_type="application/pdf",
            original_filename="legacy.pdf",
            content=content,
            page_count=1,
            archive_filename=None,
            archive_checksum=None,
        )
        write_pdf(document.source_path, [180])
        document.thumbnail_path.write_bytes(b"synthetic thumbnail")
        Document.objects.filter(pk=document.pk).update(
            checksum=compute_checksum(document.source_path),
            filename=document.source_path.name,
        )
        document.refresh_from_db()
        return document

    def workflow(self, *, updated=False, title="ADDED title"):
        workflow = Workflow.objects.create(name="Regression " + title)
        workflow.triggers.add(
            WorkflowTrigger.objects.create(
                type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED
                if updated
                else WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
                matching_algorithm=MatchingModel.MATCH_NONE,
            ),
        )
        workflow.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                assign_title=title,
            ),
        )
        return workflow

    def test_bulk_failure_removal_cannot_submit_an_unrelated_scan(self):
        failed, _ = self.consume()
        unrelated = self.legacy()
        failure = failed.tags.get(name="ocr-fail")
        with (
            mock.patch("documents.bulk_edit.bulk_update_documents.apply_async"),
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            modify_tags([failed.pk, unrelated.pk], [], [failure.pk])
        self.assertEqual(
            {c.kwargs["document_id"] for c in enqueue.call_args_list},
            {failed.pk},
        )
        self.assertFalse(pending_pages(unrelated))
        self.failure = None
        before = self.create_requests
        with self.captureOnCommitCallbacks(execute=True):
            update_document_content_maybe_archive_file.run(**enqueue.call_args.kwargs)
        self.assertEqual(self.create_requests, before + 1)

    def test_short_legacy_ocr_retains_helper_retry_contract(self):
        document = self.legacy(content="?  \n")
        failure = TagFactory(name="ocr-fail", owner=UserFactory())
        document.tags.add(failure)
        with (
            override_settings(OCR_STATUS_TAG_IDS={"ocr-fail": failure.pk}),
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            document.tags.remove(failure)
        enqueue.assert_called_once()
        self.assertEqual(len(pending_pages(document)), 1)

    @override_settings(FILENAME_FORMAT="{title}")
    def test_renamed_legacy_file_is_marked_pending_when_reprocess_fails(self):
        document = self.legacy()
        old_path = document.source_path
        request = LlamaParseDocumentParser._request_result

        def rename(parser, *args):
            current = Document.objects.get(pk=document.pk)
            current.title = "Renamed while waiting"
            current.save()
            self.assertFalse(old_path.exists())
            return request(parser, *args)

        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            mock.patch.object(
                LlamaParseDocumentParser,
                "_request_result",
                autospec=True,
                side_effect=rename,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        self.assertTrue(document.source_path.exists())
        self.assertEqual(len(pending_pages(document)), 1)
        self.assertTrue(document.tags.filter(name="ocr-fail").exists())

    def test_early_or_late_retry_bookkeeping_cannot_fire_updated_workflows(self):
        for index, late in enumerate((False, True)):
            with self.subTest(late=late):
                self.failure = 402
                added = self.workflow(title=f"ADDED {index}")
                updated = self.workflow(updated=True, title=f"Premature {index}")
                document, _ = self.consume(asn=1201 + index)
                with (
                    mock.patch(
                        "documents.tasks.update_document_content_maybe_archive_file.delay",
                    ),
                    mock.patch(
                        "documents.bulk_edit.bulk_update_documents.apply_async",
                    ) as bulk,
                    self.captureOnCommitCallbacks(execute=True),
                ):
                    remove_tag([document.pk], document.tags.get(name="ocr-fail").pk)
                if not late:
                    bulk_update_documents.run(**bulk.call_args.kwargs["kwargs"])
                self.failure = None
                self.retry(document)
                if late:
                    bulk_update_documents.run(**bulk.call_args.kwargs["kwargs"])
                self.assertEqual(
                    stored_pages(document)[0]["provenance"][CONTEXT]["state"],
                    "completed",
                )
                self.assertEqual(
                    WorkflowRun.objects.filter(
                        workflow=added,
                        document=document,
                    ).count(),
                    1,
                )
                self.assertEqual(
                    WorkflowRun.objects.filter(
                        workflow=updated,
                        document=document,
                    ).count(),
                    0,
                )
                Workflow.objects.all().delete()

    @override_settings(OCR_VALIDATE_PRINTED_DATES=True)
    def test_date_selection_cannot_overwrite_concurrent_user_edit(self):
        from documents.ingestion_checks import collect_ingestion_checks

        document, _ = self.consume()
        self.failure = None
        self.response_text = "Issued: 2024-06-28"

        def edit(parser, text):
            Document.objects.filter(pk=document.pk).update(created=date(2018, 2, 3))
            return collect_ingestion_checks(parser, text)

        with mock.patch(
            "documents.ingestion_checks.collect_ingestion_checks",
            side_effect=edit,
        ):
            self.retry(document)
        self.assertEqual(document.created, date(2018, 2, 3))
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "skipped",
        )

    @override_settings(AI_ENABLED=True, LLM_EMBEDDING_BACKEND="huggingface")
    def test_index_worker_failure_and_lost_delivery_remain_recoverable(self):
        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as enqueue:
            self.retry(document)
        kwargs = enqueue.call_args.kwargs["kwargs"]
        with (
            mock.patch(
                "documents.tasks.llm_index_add_or_update_document",
                side_effect=RuntimeError("transient embedding failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            update_document_in_llm_index.run(**kwargs)
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as retry:
            reconcile()
            retry.assert_not_called()
            self.expire_index_lease(document)
            reconcile()
            retry.assert_called_once()
        with mock.patch("documents.tasks.llm_index_add_or_update_document") as indexer:
            update_document_in_llm_index.run(**retry.call_args.kwargs["kwargs"])
            update_document_in_llm_index.run(**kwargs)
            indexer.assert_called_once()
        self.assertNotIn(PENDING, stored_pages(document)[0]["provenance"])

    def expire_index_lease(self, document):
        for page in document.parsed_pages.all():
            page.provenance[REQUEST]["lease_until"] = (
                timezone.now() - timedelta(seconds=1)
            ).isoformat()
            page.provenance[REQUEST]["not_before"] = (
                timezone.now() - timedelta(seconds=1)
            ).isoformat()
            page.save(update_fields=["provenance"])

    @override_settings(FILENAME_FORMAT="{title}")
    def test_history_updated_workflows_attach_current_immutable_pdf(self):
        from documents.file_updates import update_file

        self.failure = None
        document, _ = self.consume()
        workflow = self.workflow(updated=True, title="New title")
        workflow.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.WEBHOOK,
                order=1,
            ),
        )
        self.failure = 402
        upload = write_pdf(self.dirs.scratch_dir / "append.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=False)
        paths = []

        def read_attachment(action, root, context, logging_group, original_file):
            with pikepdf.open(original_file) as pdf:
                self.assertEqual(len(pdf.pages), 2)
            paths.append(original_file)

        self.failure = None
        with mock.patch(
            "documents.signals.handlers.execute_webhook_action",
            side_effect=read_attachment,
        ) as webhook:
            self.retry(document)
        webhook.assert_called_once()
        self.assertTrue(all(not p.exists() for p in paths))
