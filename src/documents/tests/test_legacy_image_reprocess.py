"""Legacy image reprocess checkpoints use validated PDF working renditions."""

import copy
from unittest import mock

from django.test import override_settings
from PIL import Image

from documents.models import Document
from documents.ocr_queue import pending_pages
from documents.ocr_recovery import RETRY
from documents.ocr_recovery import reconcile
from documents.page_data import stored_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_page_data import response
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from documents.utils import compute_checksum
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless_testing.factories import DocumentFactory


class WorkerLost(BaseException):
    pass


@override_settings(STORE_ONLY_CURRENT_FILE=False, ARCHIVE_FILE_GENERATION="always")
class LegacyImageReprocessTests(RemoteOCRTestCase):
    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_upload_failure_before_checkpoint_keeps_legacy_image_pending_without_archive(
        self,
    ):
        for extension, count in (("png", 1), ("tiff", 2)):
            with self.subTest(extension=extension):
                document, original = self.legacy(extension, count)
                self.failure = 402
                self.failure_stage = "/api/v1/beta/files"
                self.reprocess(document)
                self.assertEqual(len(pending_pages(document)), count)
                self.assertIsNone(document.archive_path)
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertEqual(self.create_requests, 0)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_legacy_image_success_without_archive_retires_pending_checkpoint(self):
        document, original = self.legacy("png", 1)
        self.failure = None
        self.reprocess(document)
        self.assertEqual(document.content, self.response_text)
        self.assertIsNone(document.archive_path)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertFalse(pending_pages(document))
        self.assertNotIn("ocr_job", stored_pages(document)[0]["provenance"])
        creates = self.create_requests
        requests = len(self.requests)
        reconcile()
        reconcile()
        self.assertEqual(self.create_requests, creates)
        self.assertEqual(len(self.requests), requests)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_no_archive_reprocess_preserves_content_edited_during_cloud_wait(self):
        document, original = self.legacy("png", 1)
        self.failure = None
        send = LlamaParseDocumentParser._send

        def edit(client, method, path, **kwargs):
            if method == "GET":
                Document.objects.filter(pk=document.pk).update(
                    content="Edited while cloud OCR waits",
                )
            return send(client, method, path, **kwargs)

        with mock.patch.object(LlamaParseDocumentParser, "_send", side_effect=edit):
            self.reprocess(document)
        self.assertEqual(document.content, "Edited while cloud OCR waits")
        self.assertIsNone(document.archive_path)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertFalse(pending_pages(document))

    def legacy(self, extension, count, *, content=""):
        document = DocumentFactory(
            mime_type=f"image/{extension}",
            content=content,
            original_filename=f"legacy.{extension}",
            page_count=count,
            archive_filename=None,
            archive_checksum=None,
            archive_serial_number=1801 + count,
        )
        image = Image.new("RGB", (100, 160), "white")
        options = (
            {"save_all": True, "append_images": [Image.new("RGB", (100, 160), "gray")]}
            if count == 2
            else {}
        )
        image.save(document.source_path, format=extension, dpi=(150, 150), **options)
        document.thumbnail_path.write_bytes(b"legacy thumbnail")
        Document.objects.filter(pk=document.pk).update(
            checksum=compute_checksum(document.source_path),
            filename=document.source_path.name,
        )
        document.refresh_from_db()
        self.assertIsNone(stored_pages(document))
        return document, document.source_path.read_bytes()

    def cloud(self, count):
        def pages(text):
            result = response(text)
            for section in ("items", "text", "markdown"):
                template = result[section]["pages"][0]
                result[section]["pages"] = [
                    dict(copy.deepcopy(template), page_number=number + 1)
                    for number in range(count)
                ]
            return result

        return mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            side_effect=pages,
        )

    def reprocess(self, document):
        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file.run(document.pk, remote_ocr=True)
        document.refresh_from_db()

    def test_legacy_png_and_multipage_tiff_complete_on_first_healthy_attempt(self):
        for extension, count in (("png", 1), ("tiff", 2)):
            with self.subTest(extension=extension):
                document, original = self.legacy(extension, count)
                self.failure = None
                creates = self.create_requests
                with self.cloud(count):
                    self.reprocess(document)
                self.assertEqual(self.create_requests, creates + 1)
                self.assertEqual(
                    document.content,
                    "\n\n".join([self.response_text] * count),
                )
                self.assertFalse(pending_pages(document))
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertEqual(document.page_count, count)
                thumbnail_call = LlamaParseDocumentParser.get_thumbnail.call_args
                self.assertEqual(thumbnail_call.args[1].suffix, ".pdf")
                self.assertEqual(thumbnail_call.args[2], "application/pdf")

    def test_legacy_image_poll_crash_retains_known_job_and_prior_manual_content(self):
        for extension, count in (("png", 1), ("tiff", 2)):
            with self.subTest(extension=extension):
                document, original = self.legacy(
                    extension,
                    count,
                    content="Prior human transcript",
                )
                self.failure = None
                send = LlamaParseDocumentParser._send

                def crash(client, method, path, **kwargs):
                    if method == "GET":
                        pages = stored_pages(document)
                        self.assertEqual(len(pages), count)
                        self.assertTrue(
                            all(
                                page["provenance"]["ocr_job"]["id"] == "isolated-job"
                                for page in pages
                            ),
                        )
                        raise WorkerLost
                    return send(client, method, path, **kwargs)

                creates = self.create_requests
                with self.cloud(count):
                    with (
                        mock.patch.object(
                            LlamaParseDocumentParser,
                            "_send",
                            side_effect=crash,
                        ),
                        self.assertRaises(WorkerLost),
                    ):
                        self.reprocess(document)
                    document.refresh_from_db()
                    self.assertEqual(document.content, "Prior human transcript")
                    self.assertEqual(document.source_path.read_bytes(), original)
                    self.assertTrue(
                        all(
                            page["provenance"][RETRY]["attempts"] == 1
                            for page in stored_pages(document)
                        ),
                    )
                    self.assertEqual(self.retry(document), count)
                self.assertEqual(self.create_requests, creates + 1)
                self.assertIn("Prior human transcript", document.content)
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertEqual(document.archive_serial_number, 1801 + count)

    def test_legacy_image_quota_failure_keeps_pdf_evidence_without_auto_retry(self):
        for extension, count in (("png", 1), ("tiff", 2)):
            with self.subTest(extension=extension):
                document, original = self.legacy(extension, count)
                self.failure = 402
                creates = self.create_requests
                self.reprocess(document)
                self.assertEqual(self.create_requests, creates + 1)
                self.assertEqual(document.source_path.read_bytes(), original)
                pages = pending_pages(document)
                self.assertEqual(len(pages), count)
                self.assertTrue(
                    all(page["provenance"]["reason"] == "quota" for page in pages),
                )
                self.assertTrue(
                    all(not page["provenance"][RETRY]["running"] for page in pages),
                )
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as dispatch:
            reconcile()
            dispatch.assert_not_called()

    @override_settings(FILENAME_FORMAT="{title}")
    def test_stored_image_rename_during_failed_poll_cannot_remove_working_input(self):
        document, original = self.legacy("png", 1)
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        send = LlamaParseDocumentParser._send
        renamed = []

        def rename(client, method, path, **kwargs):
            if method == "GET" and not renamed:
                current = Document.objects.get(pk=document.pk)
                old_path = current.source_path
                current.title = "Renamed while OCR waits"
                current.save(update_fields=["title", "modified"])
                self.assertFalse(old_path.exists())
                renamed.append(current.source_path)
            return send(client, method, path, **kwargs)

        with mock.patch.object(LlamaParseDocumentParser, "_send", side_effect=rename):
            self.reprocess(document)
        self.assertTrue(renamed)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(
            pending_pages(document)[0]["provenance"]["ocr_job"]["id"],
            "isolated-job",
        )
        creates = self.create_requests
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, creates)
        self.assertEqual(document.title, "Renamed while OCR waits")
