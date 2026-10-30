"""Deferred OCR honors the storage mode captured when a revision was created."""

import copy
from unittest import mock

from django.test import override_settings

from documents.data_models import DocumentMetadataOverrides
from documents.file_updates import update_file
from documents.models import Document
from documents.ocr_queue import pending_pages
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_page_data import response
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from documents.versioning import get_latest_version_for_root


class OCRStoragePolicyTests(RemoteOCRTestCase):
    @override_settings(STORE_ONLY_CURRENT_FILE=False)
    def test_explicit_custom_compaction_survives_retry_and_full_reprocess(self):
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "new.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        self.failure = None
        self.retry(document)
        self.assertIsNone(document.archive_path)
        self.assertEqual(Document.objects.count(), 1)
        from documents.tasks import update_document_content_maybe_archive_file

        def full_result(*args):
            result = response("Reprocessed collection text")
            for section in ("items", "text", "markdown"):
                second = copy.deepcopy(result[section]["pages"][0])
                second["page_number"] = 2
                result[section]["pages"].append(second)
            return result

        with (
            override_settings(ARCHIVE_FILE_GENERATION="always"),
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            self.captureOnCommitCallbacks(execute=True),
            mock.patch(
                "paperless.parsers.llamaparse.LlamaParseDocumentParser._request_result",
                side_effect=full_result,
            ),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        self.assertIsNone(document.archive_path)
        self.assertFalse(pending_pages(document))

    @override_settings(STORE_ONLY_CURRENT_FILE=False)
    def test_custom_history_keeps_previous_revisions_and_one_pdf_per_revision(self):
        document, original = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "new.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=False)
        self.failure = None
        self.retry(document)
        self.assertEqual(Document.objects.count(), 2)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertIsNone(get_latest_version_for_root(document).archive_path)

    def test_ordinary_root_retention_survives_global_default_change(self):
        for index, initial_single in enumerate((False, True)):
            with self.subTest(initial_single=initial_single):
                self.failure = 402
                with override_settings(STORE_ONLY_CURRENT_FILE=initial_single):
                    document, original = self.consume(asn=1201 + index)
                self.failure = None
                with override_settings(STORE_ONLY_CURRENT_FILE=not initial_single):
                    self.retry(document)
                if initial_single:
                    self.assertIsNone(document.archive_path)
                else:
                    self.assertEqual(document.source_path.read_bytes(), original)
                    self.assertTrue(document.archive_path.is_file())

    def test_ordinary_version_preserves_uploaded_original_despite_global_compaction(
        self,
    ):
        self.failure = None
        root, _ = self.consume()
        root_bytes = root.source_path.read_bytes()
        self.failure = 402
        upload = write_pdf(self.dirs.consumption_dir / "native_version.pdf", [300])
        original = upload.read_bytes()
        with self.captureOnCommitCallbacks(execute=True):
            with self.get_consumer(
                upload,
                overrides=DocumentMetadataOverrides(),
            ) as consumer:
                consumer.input_doc.root_document_id = root.pk
                result = consumer.run()
        version = Document.objects.get(pk=result["document_id"])
        self.assertEqual(version.root_document_id, root.pk)
        self.assertTrue(pending_pages(version))
        self.failure = None
        self.retry(version)
        self.assertEqual(version.source_path.read_bytes(), original)
        self.assertTrue(version.archive_path.is_file())
        root.refresh_from_db()
        self.assertEqual(root.source_path.read_bytes(), root_bytes)
