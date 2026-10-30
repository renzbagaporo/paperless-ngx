"""Independent adversarial cases retained as regression coverage."""

from datetime import timedelta
from unittest import mock

import httpx
from django.utils import timezone

from documents.bulk_edit import remove_tag
from documents.file_updates import update_file
from documents.models import Document
from documents.ocr_queue import pending_pages
from documents.ocr_recovery import RETRY
from documents.ocr_recovery import reconcile
from documents.ocr_recovery import store_requests
from documents.page_data import stored_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from documents.utils import compute_checksum
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory


class AbruptTermination(BaseException):
    pass


class OCRAdversarialRegressionTests(RemoteOCRTestCase):
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

    def request(self, document):
        with (
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            document.tags.remove(document.tags.get(name="ocr-fail"))

    def stop_once(self, document):
        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_request_result",
                side_effect=AbruptTermination,
            ),
            mock.patch("documents.ocr_recovery.finish"),
            self.assertRaises(AbruptTermination),
        ):
            update_document_content_maybe_archive_file.run(
                document.pk,
                pending_only=True,
            )

    def test_parent_tag_removal_cannot_request_unrelated_scan(self):
        failed, _ = self.consume()
        unrelated = self.legacy()
        parent = TagFactory(name="Status group")
        failure = failed.tags.get(name="ocr-fail")
        failure.tn_parent = parent
        failure.save()
        failed.tags.add(parent)
        unrelated.tags.add(parent)
        with (
            mock.patch("documents.bulk_edit.bulk_update_documents.apply_async"),
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            remove_tag([failed.pk, unrelated.pk], parent.pk)
        dispatched = {c.kwargs["document_id"] for c in enqueue.call_args_list}
        self.assertEqual(dispatched, {failed.pk})
        self.assertFalse(pending_pages(unrelated))

    def test_duplicate_page_origins_retain_distinct_recovery_state(self):
        document, _ = self.consume()
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1}, {"page": 1}],
            )
        document.refresh_from_db()
        pages = stored_pages(document)
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["origin"], pages[1]["origin"])
        self.request(document)
        self.stop_once(document)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ):
            reconcile()
        after = [p["provenance"][RETRY] for p in stored_pages(document)]
        self.assertEqual([s["attempts"] for s in after], [1, 0])
        self.assertIn("not_before", after[0])

    def test_provider_nul_text_does_not_reject_valid_upload(self):
        self.failure = None
        self.response_text = "Safe provider text\x00with separator"
        document, _ = self.consume()
        self.assertTrue(document.source_path.exists())
        self.assertNotIn("\x00", document.content)

    def test_provider_nul_markdown_does_not_reject_valid_upload(self):
        self.failure = None
        self.response_markdown = "# Provider markdown\x00separator"
        document, _ = self.consume()
        self.assertTrue(document.source_path.exists())

    def test_duplicate_origins_cannot_erase_paid_attempts_repeatedly(self):
        document, _ = self.consume()
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1}, {"page": 1}],
            )
        document.refresh_from_db()
        self.request(document)
        charges = []
        active_pages = []

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.url.path == "/api/v1/beta/files":
                return httpx.Response(200, json={"id": "source"})
            if request.url.path == "/api/v2/parse":
                charges.append(str(request.url))
                active_pages.append(
                    [
                        i
                        for i, p in enumerate(stored_pages(document))
                        if p["provenance"][RETRY].get("running")
                    ],
                )
                return httpx.Response(200, json={"id": "known-paid-job"})
            raise AbruptTermination

        def client(endpoint, **kwargs):
            kwargs.pop("allow_internal", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch(
            "paperless.parsers.llamaparse.create_pinned_httpx_client",
            side_effect=client,
        ):
            for _ in range(6):
                pages = stored_pages(document)
                for page in pages:
                    page["provenance"][RETRY]["not_before"] = (
                        timezone.now() - timedelta(seconds=1)
                    ).isoformat()
                store_requests(document, pages)
                with (
                    mock.patch("documents.ocr_recovery.finish"),
                    self.assertRaises(AbruptTermination),
                ):
                    update_document_content_maybe_archive_file.run(
                        document.pk,
                        pending_only=True,
                    )
                with mock.patch(
                    "documents.tasks.update_document_content_maybe_archive_file.delay",
                ):
                    reconcile()
        states = [p["provenance"][RETRY] for p in stored_pages(document)]
        self.assertEqual([s["attempts"] for s in states], [5, 1])
        self.assertEqual(active_pages, [[0], [1]])
        self.assertEqual(len(charges), 2)

    def test_corrupt_legacy_processing_file_cannot_block_other_retries(self):
        corrupt = self.legacy()
        corrupt.tags.add(TagFactory(name="ocr-processing"))
        corrupt.source_path.write_bytes(b"damaged PDF bytes")
        other, _ = self.consume()
        self.request(other)
        pages = stored_pages(other)
        pages[0]["provenance"][RETRY]["sent_at"] = None
        store_requests(other, pages)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            reconcile()
        dispatched = {c.kwargs["document_id"] for c in enqueue.call_args_list}
        self.assertIn(other.pk, dispatched)
