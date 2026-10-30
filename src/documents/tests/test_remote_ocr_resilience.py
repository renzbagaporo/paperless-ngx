"""User-visible guarantees shared by the old helper and native OCR ingestion.

Provider requests are simulated at the HTTP boundary; ingestion, file storage,
page persistence and retries use the real implementation and isolated storage.
"""

from __future__ import annotations

import socket
from io import StringIO
from unittest import mock

import httpx
import pikepdf
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.test import override_settings
from PIL import Image

from documents.bulk_edit import modify_tags
from documents.bulk_edit import remove_tag
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import DocumentSource
from documents.file_updates import update_file
from documents.models import Document
from documents.models import Note
from documents.ocr_queue import pending_pages
from documents.ocr_queue import retry_pending_ocr
from documents.page_data import PRIVATE_PROVENANCE_KEYS
from documents.page_data import stored_pages
from documents.page_enrichment import enrich_document
from documents.page_enrichment import missing_rich_pages
from documents.page_splitting import split_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_page_data import response
from documents.tests.test_pdf_editor_updates import searchable_pdf
from documents.tests.utils import GetConsumerMixin
from documents.utils import compute_checksum
from documents.versioning import get_latest_version_for_root
from paperless.network import create_pinned_httpx_client
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory


class RemoteOCRFixture(DirectoriesMixin, GetConsumerMixin):
    def setUp(self):
        mock.patch("paperless.parsers.llamaparse.time.sleep", return_value=None).start()
        self.user_tag = TagFactory(name="Personal bills")
        self.requests = []
        self.failure = 402
        self.failure_stage = "/api/v2/parse"
        self.create_requests = 0
        self.response_text = "Water Supply Notice"
        self.response_markdown = None
        self.registry = mock.patch("documents.consumer.get_parser_registry").start()
        self.registry.return_value.get_parser_for_file.return_value = (
            LlamaParseDocumentParser
        )

        def thumbnail(parser, path, mime_type):
            result = parser._tempdir / "thumbnail.webp"
            result.write_bytes(b"fixture thumbnail")
            return result

        mock.patch.object(
            LlamaParseDocumentParser,
            "get_thumbnail",
            autospec=True,
            side_effect=thumbnail,
        ).start()

        def handler(request):
            self.requests.append(request)
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.url.path == "/api/v1/beta/files":
                if self.failure_stage == request.url.path and self.failure:
                    return httpx.Response(self.failure)
                return httpx.Response(200, json={"id": "isolated-source"})
            if request.url.path == "/api/v2/parse":
                self.create_requests += 1
                if self.failure == "timeout":
                    raise httpx.ReadTimeout("private provider details", request=request)
                return httpx.Response(
                    self.failure or 200
                    if self.failure_stage == request.url.path
                    else 200,
                    json={"id": "isolated-job"},
                )
            if request.url.path == "/api/v2/parse/isolated-job":
                if self.failure_stage == request.url.path and self.failure:
                    return httpx.Response(self.failure)
                result = response(self.response_text)
                if self.response_markdown is not None:
                    result["markdown"]["pages"][0]["markdown"] = self.response_markdown
                return httpx.Response(200, json=result)
            return httpx.Response(500)

        def client(endpoint, **kwargs):
            kwargs.pop("allow_internal", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        mock.patch(
            "paperless.parsers.llamaparse.create_pinned_httpx_client",
            side_effect=client,
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.publish = mock.patch("documents.file_updates._publish").start()
        mock.patch(
            "documents.file_updates.get_parser_registry",
            return_value=self.registry.return_value,
        ).start()

    def retry(self, document, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            completed = retry_pending_ocr(document.pk, **kwargs)
        document.refresh_from_db()
        return completed

    def consume(
        self,
        source=DocumentSource.ApiUpload,
        *,
        asn=1201,
        text=None,
        upload=None,
        created=None,
    ):
        if upload is None:
            upload = settings.CONSUMPTION_DIR / f"notice-{asn}.pdf"
            if text is None:
                write_pdf(upload, [180])
            else:
                searchable_pdf(upload, [text])
        before = upload.read_bytes()
        metadata = DocumentMetadataOverrides(
            title="Water Supply Notices",
            asn=asn,
            tag_ids=[self.user_tag.pk],
            created=created,
        )
        with self.captureOnCommitCallbacks(execute=True):
            with self.get_consumer(
                upload,
                overrides=metadata,
                source=source,
            ) as consumer:
                result = consumer.run()
        return Document.objects.get(pk=result["document_id"]), before


@override_settings(
    REMOTE_OCR_ENGINE="llamaparse",
    REMOTE_OCR_API_KEY="isolated-provider-key",
    REMOTE_OCR_MODE="always",
    STORE_ONLY_CURRENT_FILE=True,
    OCR_ROTATE_PAGES=False,
)
class RemoteOCRTestCase(RemoteOCRFixture, TestCase):
    pass


class RemoteOCRIngestionTests(RemoteOCRTestCase):
    def test_ambiguous_job_creation_failure_is_not_replayed_automatically(self):
        self.failure = 500
        document, original = self.consume()
        self.assertEqual(self.create_requests, 1)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertTrue(pending_pages(document))

    def test_polling_transient_failure_reuses_the_same_job(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                503 if len(calls) < 3 else 200,
                json={"id": "same-job"},
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = LlamaParseDocumentParser._send(
                client,
                "GET",
                "https://fixture.invalid/parse/same-job",
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({str(request.url) for request in calls}), 1)

    def test_dashboard_upload_survives_exhausted_credits(self):
        document, original = self.consume()
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(document.archive_serial_number, 1201)
        self.assertEqual(document.title, "Water Supply Notices")
        self.assertEqual(
            set(document.tags.values_list("name", flat=True)),
            {"Personal bills", "ocr-pending", "ocr-fail"},
        )
        self.assertEqual(document.parsed_pages.count(), 1)
        self.assertEqual(
            document.parsed_pages.get().provenance["ocr_status"],
            "pending",
        )
        self.assertEqual(document.parsed_pages.get().provenance["reason"], "quota")
        self.assertEqual(document.content, "")
        self.assertEqual(self.create_requests, 1)
        self.assertEqual(self.requests[-1].method, "DELETE")

    def test_consume_folder_survives_exhausted_credits(self):
        document, original = self.consume(DocumentSource.ConsumeFolder)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())
        self.assertFalse((settings.CONSUMPTION_DIR / "notice-1201.pdf").exists())

    def test_provider_outages_keep_saved_original_and_user_metadata(self):
        for index, failure in enumerate((401, 429, 500, 503, "timeout"), 1):
            with self.subTest(failure=failure):
                self.failure = failure
                document, original = self.consume(asn=1300 + index)
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertTrue(document.tags.filter(name="ocr-fail").exists())
                self.assertTrue(document.tags.filter(pk=self.user_tag.pk).exists())
                self.assertFalse(
                    document.notes.filter(
                        note__contains="private provider details",
                    ).exists(),
                )

    def test_dns_outage_preserves_upload_before_http_client_is_created(self):
        with (
            mock.patch(
                "paperless.parsers.llamaparse.create_pinned_httpx_client",
                wraps=create_pinned_httpx_client,
            ),
            mock.patch(
                "paperless.network.socket.getaddrinfo",
                side_effect=socket.gaierror("private resolver details"),
            ),
        ):
            document, original = self.consume()
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())
        self.assertEqual(
            stored_pages(document)[0]["provenance"]["reason"],
            "unavailable",
        )
        self.assertEqual(self.requests, [])

    def test_digital_pdf_keeps_existing_text_without_remote_request(self):
        document, original = self.consume(text="Existing digital document text " * 20)
        self.assertIn("Existing digital document text", document.content)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(self.create_requests, 0)
        self.assertTrue(document.tags.filter(name="ocr-skip").exists())
        self.assertFalse(document.tags.filter(name="ocr-pending").exists())

    def test_quota_at_upload_or_poll_keeps_document(self):
        for index, stage in enumerate(
            ("/api/v1/beta/files", "/api/v2/parse/isolated-job"),
        ):
            with self.subTest(stage=stage):
                self.failure_stage = stage
                document, original = self.consume(asn=1500 + index)
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertTrue(document.tags.filter(name="ocr-pending").exists())

    def test_retry_keeps_id_asn_user_edits_notes_and_original_page_identity(self):
        document, _ = self.consume()
        original_id = document.pk
        original_origin = document.parsed_pages.get().origin
        document.title = "My edited title"
        document.save()
        Note.objects.create(document=document, note="My private note")
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        document.refresh_from_db()
        self.assertEqual(document.pk, original_id)
        self.assertEqual(document.archive_serial_number, 1201)
        self.assertEqual(document.title, "My edited title")
        self.assertTrue(document.notes.filter(note="My private note").exists())
        self.assertTrue(document.tags.filter(pk=self.user_tag.pk).exists())
        self.assertTrue(document.tags.filter(name="ocr-ok").exists())
        self.assertFalse(
            document.tags.filter(
                name__in=("ocr-fail", "ocr-pending", "ocr-processing"),
            ).exists(),
        )
        self.assertEqual(document.parsed_pages.get().origin, original_origin)
        self.assertIn(self.response_text, document.content)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(Document.deleted_objects.count(), 0)
        self.assertFalse(document.has_archive_version)
        calls = self.create_requests
        self.assertEqual(self.retry(document), 0)
        self.assertEqual(self.create_requests, calls)

    def test_repeated_failed_retry_retains_original_and_same_identity(self):
        document, original = self.consume()
        for _ in range(2):
            self.assertEqual(self.retry(document), 0)
            document.refresh_from_db()
            self.assertEqual(document.source_path.read_bytes(), original)
            self.assertEqual(document.archive_serial_number, 1201)
            self.assertTrue(document.tags.filter(name="ocr-fail").exists())
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(document.notes.count(), 1)

    def test_removing_failure_tag_queues_retry_after_commit(self):
        document, _ = self.consume()
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                document.tags.remove(document.tags.get(name="ocr-fail"))
            enqueue.assert_called_once_with(
                document_id=document.pk,
                remote_ocr=True,
                pending_only=True,
            )

    def test_bulk_tag_removal_queues_retry(self):
        for index, method in enumerate(("remove_tag", "modify_tags")):
            with self.subTest(method=method):
                document, _ = self.consume(asn=1800 + index)
                failure_tag = document.tags.get(name="ocr-fail")
                with (
                    mock.patch("documents.bulk_edit.bulk_update_documents.apply_async"),
                    mock.patch(
                        "documents.tasks.update_document_content_maybe_archive_file.delay",
                    ) as enqueue,
                    self.captureOnCommitCallbacks(execute=True),
                ):
                    if method == "remove_tag":
                        remove_tag([document.pk], failure_tag.pk)
                    else:
                        modify_tags([document.pk], [], [failure_tag.pk])
                enqueue.assert_called_once_with(
                    document_id=document.pk,
                    remote_ocr=True,
                    pending_only=True,
                )

    def test_clearing_tags_queues_retry_without_removing_durable_pending_state(self):
        document, _ = self.consume()
        with (
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            document.tags.clear()
        enqueue.assert_called_once_with(
            document_id=document.pk,
            remote_ocr=True,
            pending_only=True,
        )
        self.assertTrue(pending_pages(document))

    def test_reprocess_action_retries_saved_pages_in_place(self):
        document, _ = self.consume()
        self.failure = None
        with self.captureOnCommitCallbacks(execute=True):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        self.assertIn(self.response_text, document.content)
        self.assertEqual(document.archive_serial_number, 1201)
        self.assertEqual(Document.objects.count(), 1)

    def test_append_saves_new_pages_when_quota_exhausted_and_retries_only_them(self):
        document, _ = self.consume()
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        first_origin = document.parsed_pages.get().origin
        self.failure = 402
        upload = write_pdf(self.dirs.scratch_dir / "next-notice.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        document.refresh_from_db()
        self.assertEqual(document.page_count, 2)
        self.assertEqual(document.archive_serial_number, 1201)
        self.assertEqual(document.parsed_pages.first().origin, first_origin)
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())
        calls = self.create_requests
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, calls + 1)
        self.assertEqual(document.parsed_pages.first().origin, first_origin)
        self.assertEqual(Document.objects.count(), 1)

    def test_mixed_success_failure_saves_progress_and_does_not_bill_success_twice(self):
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "another.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        self.failure = None
        self.assertEqual(self.retry(document, max_pages=1), 1)
        document.refresh_from_db()
        first = stored_pages(document)[0]
        self.failure = 402
        self.assertEqual(self.retry(document), 0)
        current = stored_pages(document)[0]
        for key in PRIVATE_PROVENANCE_KEYS:
            current["provenance"].pop(key, None)
            first["provenance"].pop(key, None)
        self.assertEqual(current, first)
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        completed = stored_pages(document)[0]
        for key in PRIVATE_PROVENANCE_KEYS:
            first["provenance"].pop(key, None)
            completed["provenance"].pop(key, None)
        self.assertEqual(completed, first)
        self.assertEqual(self.retry(document), 0)

    def test_removing_pending_page_clears_pending_state_without_request(self):
        document, _ = self.consume()
        self.failure = None
        self.retry(document)
        self.failure = 402
        upload = write_pdf(self.dirs.scratch_dir / "pending.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        calls = self.create_requests
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                operations=[{"page": 1}],
                keep_only_current_file=True,
            )
        document.refresh_from_db()
        self.assertEqual(document.page_count, 1)
        self.assertTrue(document.tags.filter(name="ocr-ok").exists())
        self.assertFalse(document.tags.filter(name="ocr-pending").exists())
        self.assertEqual(self.create_requests, calls)

    @override_settings(OCR_REVIEW_CROSS_PAGE_NUMBERS=True)
    def test_page_local_retry_restores_cross_page_review_tag_and_note(self):
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "next.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        self.failure = None
        self.response_text = "Account 111111"
        self.response_markdown = "Account 222222"
        self.retry(document, max_pages=1)
        self.response_text = "Account 222222"
        self.retry(document)
        self.assertTrue(document.tags.filter(name="ocr-review").exists())
        self.assertTrue(document.notes.filter(note__startswith="OCR review:").exists())
        self.assertIn("111111", stored_pages(document)[0]["text"])

    def test_processing_tag_remains_until_all_queued_pages_are_attempted(self):
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "next.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        self.failure = None
        request = LlamaParseDocumentParser._request_result

        def inspect_status(parser, *args):
            self.assertTrue(document.tags.filter(name="ocr-processing").exists())
            return request(parser, *args)

        with mock.patch.object(
            LlamaParseDocumentParser,
            "_request_result",
            autospec=True,
            side_effect=inspect_status,
        ):
            self.retry(document)
        self.assertFalse(document.tags.filter(name="ocr-processing").exists())

    def test_removing_pending_append_clears_failure_on_legacy_text_pages(self):
        document, _ = self.consume(text="Existing digital text " * 20)
        document.parsed_pages.update(provenance={"engine": "pdf_text"})
        upload = write_pdf(self.dirs.scratch_dir / "pending.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                operations=[{"page": 1}],
                keep_only_current_file=True,
            )
        document.refresh_from_db()
        self.assertFalse(
            document.tags.filter(name__in=("ocr-pending", "ocr-fail")).exists(),
        )
        self.assertFalse(pending_pages(document))
        self.assertIn("Existing digital text", document.content)

    def test_split_transfers_pending_page_and_retry_work_to_new_document(self):
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "pending.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        document.refresh_from_db()
        with mock.patch(
            "documents.page_splitting.make_thumbnail_from_pdf",
            return_value=document.thumbnail_path,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                result = split_pages(
                    document.pk,
                    operations=[{"page": 2}],
                    source_checksum=document.checksum,
                )
        extracted = Document.objects.get(pk=result["new_document_id"])
        self.assertTrue(extracted.tags.filter(name="ocr-pending").exists())
        self.failure = None
        self.assertEqual(self.retry(extracted), 1)
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())

    def test_pending_queue_preview_never_spends_credits(self):
        document, _ = self.consume()
        before = self.create_requests
        output = StringIO()
        call_command("document_ocr", stdout=output)
        self.assertIn(str(document.pk), output.getvalue())
        self.assertIn("pending", output.getvalue())
        self.assertEqual(self.create_requests, before)

    def test_retry_requires_explicit_positive_budget(self):
        document, _ = self.consume()
        calls = self.create_requests
        for limit in (None, 0, -1):
            with self.subTest(limit=limit), self.assertRaises(CommandError):
                options = {"max_pages": limit} if limit is not None else {}
                call_command("document_ocr", apply=True, stdout=StringIO(), **options)
        self.assertEqual(self.create_requests, calls)
        self.assertTrue(pending_pages(document))

    def test_rich_enrichment_cannot_drop_pending_ocr_or_spend_its_credits(self):
        document, original = self.consume()
        calls = self.create_requests
        self.assertEqual(missing_rich_pages(document), 0)
        self.assertEqual(enrich_document(document.pk, max_pages=1), 0)
        self.assertTrue(pending_pages(document))
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(self.create_requests, calls)

    def test_bounded_queue_retry_survives_lost_broker_message(self):
        document, _ = self.consume()
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
            side_effect=ConnectionError("isolated broker unavailable"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                document.tags.remove(document.tags.get(name="ocr-fail"))
        # A fresh process can recover work from saved evidence, without broker state.
        self.assertTrue(pending_pages(Document.objects.get(pk=document.pk)))
        self.failure = None
        with self.captureOnCommitCallbacks(execute=True):
            call_command(
                "document_ocr",
                apply=True,
                max_pages=1,
                stdout=StringIO(),
            )
        document.refresh_from_db()
        self.assertFalse(pending_pages(document))
        self.assertTrue(document.tags.filter(name="ocr-ok").exists())

    @override_settings(STORE_ONLY_CURRENT_FILE=False)
    def test_retry_preserves_uploaded_original_when_archive_storage_is_enabled(self):
        document, original = self.consume()
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertTrue(document.archive_path.is_file())
        self.assertIn(self.response_text, document.content)
        self.assertTrue(document.tags.filter(name="ocr-ok").exists())

    def test_retry_preserves_explicit_file_history(self):
        document, original = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "next.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=False)
        document.refresh_from_db()
        latest = get_latest_version_for_root(document)
        self.assertNotEqual(latest.pk, document.pk)
        self.failure = None
        self.assertEqual(self.retry(document), 2)
        latest.refresh_from_db()
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(Document.objects.count(), 2)
        self.assertEqual(get_latest_version_for_root(document).pk, latest.pk)
        self.assertFalse(pending_pages(latest))
        self.assertTrue(document.tags.filter(name="ocr-ok").exists())

    def test_failed_reprocess_preserves_existing_ocr_file_and_rich_evidence(self):
        document, _ = self.consume()
        self.failure = None
        self.retry(document)
        original = document.source_path.read_bytes()
        previous = stored_pages(document)[0]
        text = document.content
        self.failure = 402
        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            mock.patch("documents.tasks.should_produce_archive", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        self.assertEqual(document.content, text)
        self.assertEqual(document.source_path.read_bytes(), original)
        failed = stored_pages(document)[0]
        for field in ("origin", "text", "markdown", "layout"):
            self.assertEqual(failed[field], previous[field])
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())
        self.assertEqual(self.retry(document), 0)
        self.assertEqual(document.content, text)
        self.assertEqual(stored_pages(document)[0]["layout"], previous["layout"])

    def test_retry_preserves_manually_edited_content(self):
        document, _ = self.consume()
        document.content = "My manual transcription"
        document.save()
        self.failure = None
        self.retry(document)
        self.assertIn("My manual transcription", document.content)
        self.assertIn(self.response_text, document.content)

    def test_retry_preserves_content_edited_while_provider_request_is_running(self):
        document, _ = self.consume()
        self.failure = None
        request = LlamaParseDocumentParser._request_result

        def edit_while_waiting(parser, *args):
            result = request(parser, *args)
            Document.objects.filter(pk=document.pk).update(
                title="Edited while OCR runs",
                content="New manual transcription",
            )
            return result

        with mock.patch.object(
            LlamaParseDocumentParser,
            "_request_result",
            autospec=True,
            side_effect=edit_while_waiting,
        ):
            self.retry(document)
        self.assertEqual(document.title, "Edited while OCR runs")
        self.assertIn("New manual transcription", document.content)
        self.assertIn(self.response_text, document.content)

    @override_settings(FILENAME_FORMAT="{title}")
    def test_retry_keeps_paid_result_when_metadata_save_renames_the_source(self):
        document, _ = self.consume()
        before = document.source_path
        self.failure = None
        request = LlamaParseDocumentParser._request_result

        def edit_while_waiting(parser, *args):
            result = request(parser, *args)
            document.title = "Renamed while OCR runs"
            document.save()
            return result

        with mock.patch.object(
            LlamaParseDocumentParser,
            "_request_result",
            autospec=True,
            side_effect=edit_while_waiting,
        ):
            self.assertEqual(self.retry(document), 1)
        self.assertEqual(document.title, "Renamed while OCR runs")
        self.assertNotEqual(document.source_path, before)
        self.assertIn(self.response_text, document.content)
        self.assertFalse(pending_pages(document))

    def test_retry_records_layout_coordinates_after_pending_page_rotation(self):
        document, _ = self.consume()
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1, "rotate": 90}],
            )
        self.failure = None
        self.retry(document)
        page = stored_pages(document)[0]
        self.assertEqual(page["rotation"], 90)
        self.assertEqual(page["provenance"]["layout_rotation"], 90)

    def test_retry_refreshes_thumbnail_if_ocr_changes_page_orientation(self):
        document, _ = self.consume()
        thumbnail = LlamaParseDocumentParser.get_thumbnail
        before = thumbnail.call_count
        self.failure = None

        def orient(parser, source):
            output = parser._tempdir / "rotated.pdf"
            with pikepdf.open(source) as pdf:
                pdf.pages[0].rotate(90, relative=True)
                pdf.save(output)
            return output

        with mock.patch.object(
            LlamaParseDocumentParser,
            "_orient",
            autospec=True,
            side_effect=orient,
        ):
            self.retry(document)
        with pikepdf.open(document.source_path) as pdf:
            self.assertGreater(
                float(pdf.pages[0].mediabox[2]),
                float(pdf.pages[0].mediabox[3]),
            )
        self.assertGreater(thumbnail.call_count, before)

    def test_legacy_raw_images_can_be_queued_and_recovered_without_losing_pages(self):
        for index, (extension, count) in enumerate((("png", 1), ("tiff", 2))):
            with self.subTest(format=extension):
                self.failure = 402
                document = DocumentFactory(
                    mime_type=f"image/{extension}",
                    original_filename=f"legacy.{extension}",
                    page_count=count,
                    content="",
                    archive_filename=None,
                    archive_checksum=None,
                )
                image = Image.new("RGB", (180, 240), (255, 240, 220))
                others = (
                    [Image.new("RGB", (180, 240), (220, 240, 255))]
                    if count == 2
                    else []
                )
                options = {"save_all": True, "append_images": others} if others else {}
                image.save(
                    document.source_path,
                    format=extension,
                    dpi=(150, 150),
                    **options,
                )
                document.thumbnail_path.write_bytes(b"thumbnail")
                Document.objects.filter(pk=document.pk).update(
                    checksum=compute_checksum(document.source_path),
                )
                document.refresh_from_db()
                original = document.source_path.read_bytes()
                with (
                    mock.patch(
                        "documents.tasks.get_parser_registry",
                        return_value=self.registry.return_value,
                    ),
                    self.captureOnCommitCallbacks(execute=True),
                ):
                    update_document_content_maybe_archive_file(
                        document.pk,
                        remote_ocr=True,
                    )
                document.refresh_from_db()
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertEqual(len(pending_pages(document)), count)
                self.failure = None
                self.assertEqual(self.retry(document), count)
                self.assertEqual(document.mime_type, "application/pdf")
                self.assertTrue(document.original_filename.endswith(".pdf"))
                with pikepdf.open(document.source_path) as pdf:
                    self.assertEqual(len(pdf.pages), count)
                self.assertTrue(document.tags.filter(name="ocr-ok").exists())

    def test_retry_rejects_source_changed_while_provider_request_is_running(self):
        document, _ = self.consume()
        previous = stored_pages(document)
        self.failure = None
        request = LlamaParseDocumentParser._request_result

        def change_while_waiting(parser, *args):
            result = request(parser, *args)
            document.source_path.write_bytes(b"external change during OCR")
            return result

        with mock.patch.object(
            LlamaParseDocumentParser,
            "_request_result",
            autospec=True,
            side_effect=change_while_waiting,
        ):
            with self.assertRaisesRegex(ValueError, "changed during OCR"):
                self.retry(document)
        document.refresh_from_db()
        self.assertEqual(
            document.source_path.read_bytes(),
            b"external change during OCR",
        )
        saved = stored_pages(document)
        for page in saved:
            retry = page["provenance"].pop("ocr_retry", None)
            self.assertFalse(retry and retry.get("running"))
            self.assertEqual(page["provenance"].pop("ocr_job")["id"], "isolated-job")
        self.assertEqual(saved, previous)

    def test_failed_database_write_does_not_replace_the_saved_original(self):
        document, original = self.consume()
        previous = stored_pages(document)
        self.failure = None
        with mock.patch(
            "documents.ocr_queue.save_pages",
            side_effect=RuntimeError("fixture failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                self.retry(document)
        document.refresh_from_db()
        self.assertEqual(document.source_path.read_bytes(), original)
        saved = stored_pages(document)
        for page in saved:
            retry = page["provenance"].pop("ocr_retry", None)
            self.assertFalse(retry and retry.get("running"))
            self.assertEqual(page["provenance"].pop("ocr_job")["id"], "isolated-job")
        self.assertEqual(saved, previous)
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())
        self.assertFalse(document.tags.filter(name="ocr-processing").exists())

    def test_retry_refuses_file_that_changed_outside_paperless(self):
        document, _ = self.consume()
        calls = self.create_requests
        document.source_path.write_bytes(b"changed outside paperless")
        with self.assertRaisesRegex(ValueError, "checksum changed"):
            self.retry(document)
        self.assertEqual(self.create_requests, calls)
        self.assertEqual(
            document.source_path.read_bytes(),
            b"changed outside paperless",
        )

    def test_pending_split_keeps_completed_source_ready(self):
        document, _ = self.consume()
        self.failure = None
        self.retry(document)
        self.failure = 402
        upload = write_pdf(self.dirs.scratch_dir / "next.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        document.refresh_from_db()
        calls = self.create_requests
        with (
            mock.patch(
                "documents.page_splitting.make_thumbnail_from_pdf",
                return_value=document.thumbnail_path,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = split_pages(
                document.pk,
                operations=[{"page": 2}],
                source_checksum=document.checksum,
                move=True,
                keep_only_current_file=True,
            )
        extracted = Document.objects.get(pk=result["new_document_id"])
        document.refresh_from_db()
        self.assertFalse(pending_pages(document))
        self.assertTrue(document.tags.filter(name="ocr-ok").exists())
        self.assertTrue(extracted.tags.filter(name="ocr-pending").exists())
        self.assertEqual(self.create_requests, calls)
        with pikepdf.open(document.source_path) as pdf:
            self.assertEqual(len(pdf.pages), 1)

    @override_settings(STORE_ONLY_CURRENT_FILE=False)
    def test_image_and_multipage_tiff_survive_quota_and_retry(self):
        for index, (extension, count) in enumerate((("png", 1), ("tiff", 2))):
            with self.subTest(format=extension):
                self.failure = 402
                upload = settings.CONSUMPTION_DIR / f"scan.{extension}"
                image = Image.new("RGB", (180, 240), (255, 240, 220))
                others = (
                    [Image.new("RGB", (180, 240), (220, 240, 255))]
                    if count == 2
                    else []
                )
                options = {"save_all": True, "append_images": others} if others else {}
                image.save(upload, dpi=(150, 150), **options)
                document, original = self.consume(upload=upload, asn=1700 + index)
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertEqual(len(pending_pages(document)), count)
                self.failure = None
                self.assertEqual(self.retry(document), count)
                self.assertEqual(document.source_path.read_bytes(), original)
                with pikepdf.open(document.archive_path) as pdf:
                    self.assertEqual(len(pdf.pages), count)
                self.assertTrue(document.tags.filter(name="ocr-ok").exists())

    def test_malformed_remote_bbox_cannot_reject_valid_upload(self):
        self.failure = None
        original_response = response

        def malformed_result(text):
            result = original_response(text)
            result["items"]["pages"][0]["items"][0]["bbox"] = {
                "x": 0,
                "y": 0,
                "w": 50,
                "h": 20,
            }
            return result

        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            side_effect=malformed_result,
        ):
            document, original = self.consume()
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertTrue(pending_pages(document))

    @override_settings(FILENAME_FORMAT="{title}")
    def test_enrichment_keeps_paid_result_when_title_renames_source(self):
        self.failure = None
        document, _ = self.consume()
        document.parsed_pages.update(layout=None, markdown=None)
        original = document.source_path.read_bytes()
        request = LlamaParseDocumentParser._request_result

        def edit_while_waiting(parser, *args):
            result = request(parser, *args)
            current = Document.objects.get(pk=document.pk)
            current.title = "Renamed during rich enrichment"
            current.save()
            return result

        with mock.patch.object(
            LlamaParseDocumentParser,
            "_request_result",
            autospec=True,
            side_effect=edit_while_waiting,
        ):
            self.assertEqual(enrich_document(document.pk, max_pages=1), 1)
        document.refresh_from_db()
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertIsNotNone(stored_pages(document)[0]["layout"])

    def test_manual_content_survives_pending_append_and_recovery(self):
        self.failure = None
        document, _ = self.consume()
        Document.objects.filter(pk=document.pk).update(
            content="My manual archival annotation",
        )
        document.refresh_from_db()
        self.failure = 402
        upload = write_pdf(settings.CONSUMPTION_DIR / "additional_scan.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        document.refresh_from_db()
        self.assertIn("My manual archival annotation", document.content)
        self.failure = None
        self.retry(document)
        self.assertIn("My manual archival annotation", document.content)

    @override_settings(ARCHIVE_FILE_GENERATION="always")
    def test_completed_reprocess_obeys_current_only_storage(self):
        self.failure = None
        document, _ = self.consume()
        self.assertIsNone(document.archive_path)
        old_source = document.source_path
        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        self.assertIsNone(document.archive_path)
        self.assertTrue(document.source_path.is_file())
        self.assertFalse(old_source.exists() and old_source != document.source_path)
