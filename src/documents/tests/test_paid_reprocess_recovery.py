"""Full reprocess durably tracks the current original/archive rendition."""

from unittest import mock

import pikepdf
from django.test import override_settings
from PIL import Image

from documents.file_updates import update_file
from documents.models import Document
from documents.ocr_queue import pending_pages
from documents.ocr_recovery import RETRY
from documents.ocr_recovery import reconcile
from documents.page_data import stored_pages
from documents.page_splitting import split_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from paperless.parsers.llamaparse import LlamaParseDocumentParser


class WorkerLost(BaseException):
    pass


@override_settings(STORE_ONLY_CURRENT_FILE=False, ARCHIVE_FILE_GENERATION="always")
class PaidReprocessRecoveryTests(RemoteOCRTestCase):
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

    def interrupted(self, document, *, ambiguous=False):
        original = LlamaParseDocumentParser._send

        def crash(client, method, path, **kwargs):
            if (ambiguous and path == "/api/v2/parse") or (
                not ambiguous and method == "GET"
            ):
                self.assertTrue(pending_pages(document))
                job = stored_pages(document)[0]["provenance"]["ocr_job"]
                self.assertEqual(job["state"], "submitting" if ambiguous else "polling")
                raise WorkerLost
            return original(client, method, path, **kwargs)

        with (
            mock.patch.object(LlamaParseDocumentParser, "_send", side_effect=crash),
            self.assertRaises(WorkerLost),
        ):
            self.reprocess(document)
        document.refresh_from_db()

    def test_retained_searchable_archive_job_resumes_without_another_paid_post(self):
        self.failure = None
        document, original = self.consume()
        previous_archive = document.archive_path.read_bytes()
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        self.reprocess(document)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(document.archive_path.read_bytes(), previous_archive)
        self.assertTrue(pending_pages(document))
        creates = self.create_requests
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, creates)
        self.assertEqual(document.source_path.read_bytes(), original)

    def test_rotated_raw_original_and_flattened_archive_resume_same_job(self):
        upload = self.dirs.consumption_dir / "rotated.pdf"
        with pikepdf.new() as pdf:
            page = pdf.add_blank_page(page_size=(180, 500))
            page.Contents = pikepdf.Stream(pdf, b"1 0 0 rg 10 20 40 70 re f")
            page.rotate(90, relative=False)
            pdf.save(upload)
        self.failure = None
        document, original = self.consume(upload=upload)
        with (
            pikepdf.open(document.source_path) as raw,
            pikepdf.open(document.archive_path) as archive,
        ):
            self.assertEqual(raw.pages[0].get("/Rotate"), 90)
            self.assertNotEqual(
                list(raw.pages[0].mediabox),
                list(archive.pages[0].mediabox),
            )
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        self.reprocess(document)
        creates = self.create_requests
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, creates)
        self.assertEqual(document.source_path.read_bytes(), original)
        with pikepdf.open(document.archive_path) as archive:
            self.assertEqual(list(archive.pages[0].mediabox), [0, 0, 500, 180])

    def test_image_original_reprocess_uses_pdf_archive_with_its_actual_mime(self):
        upload = self.dirs.consumption_dir / "scan.png"
        Image.new("RGB", (120, 180), "white").save(upload)
        self.failure = None
        document, original = self.consume(upload=upload)
        self.assertEqual(document.mime_type, "image/png")
        self.assertIsNotNone(document.archive_path)
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        self.reprocess(document)
        self.assertEqual(
            stored_pages(document)[0]["provenance"]["ocr_job"]["id"],
            "isolated-job",
        )
        creates = self.create_requests
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, creates)
        self.assertEqual(document.source_path.read_bytes(), original)

    def test_completed_document_crash_saves_known_job_and_preserves_manual_content(
        self,
    ):
        self.failure = None
        document, original = self.consume()
        old_archive = document.archive_path.read_bytes()
        old_page = stored_pages(document)[0]
        document.content = "Manually corrected transcript"
        document.save(update_fields=["content"])
        self.interrupted(document)
        saved = stored_pages(document)[0]
        self.assertEqual(saved["provenance"]["ocr_job"]["id"], "isolated-job")
        self.assertTrue(saved["provenance"][RETRY]["running"])
        self.assertEqual(saved["markdown"], old_page["markdown"])
        self.assertEqual(saved["layout"], old_page["layout"])
        self.assertEqual(document.content, "Manually corrected transcript")
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(document.archive_path.read_bytes(), old_archive)
        creates = self.create_requests
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, creates)
        self.assertIn("Manually corrected transcript", document.content)
        self.assertEqual(document.archive_serial_number, 1201)

    def test_completed_document_ambiguous_crash_requires_new_explicit_intent(self):
        self.failure = None
        document, original = self.consume()
        self.interrupted(document, ambiguous=True)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as dispatch:
            reconcile()
            reconcile()
            dispatch.assert_not_called()
        self.assertFalse(stored_pages(document)[0]["provenance"][RETRY]["requested"])
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertTrue(
            document.notes.filter(note__contains="paid job may already exist").exists(),
        )

    def test_ordinary_full_reprocess_quota_failure_does_not_auto_retry(self):
        self.failure = None
        document, _ = self.consume()
        self.failure = 402
        self.reprocess(document)
        creates = self.create_requests
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as dispatch:
            reconcile()
            reconcile()
            dispatch.assert_not_called()
        self.assertEqual(self.create_requests, creates)
        self.assertFalse(stored_pages(document)[0]["provenance"][RETRY]["running"])

    def test_archive_job_survives_later_duplicate_rotate_and_split(self):
        self.failure = None
        document, _ = self.consume()
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        self.reprocess(document)
        thumbnail = self.dirs.scratch_dir / "thumbnail.webp"
        thumbnail.write_bytes(b"thumbnail")
        with (
            mock.patch(
                "documents.file_updates.make_thumbnail_from_pdf",
                return_value=thumbnail,
            ),
            mock.patch(
                "documents.page_splitting.make_thumbnail_from_pdf",
                return_value=thumbnail,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1, "rotate": 90}, {"page": 1}],
            )
            result = split_pages(document.pk, [{"page": 1}])
        target = Document.objects.get(pk=result["new_document_id"])
        creates = self.create_requests
        self.failure = None
        self.assertEqual(self.retry(target), 1)
        document.refresh_from_db()
        self.assertEqual(self.retry(document), 2)
        self.assertEqual(self.create_requests, creates)
        self.assertEqual(target.content, self.response_text)
        with pikepdf.open(target.source_path) as pdf:
            self.assertEqual(pdf.pages[0].get("/Rotate"), 90)

    @override_settings(ARCHIVE_FILE_GENERATION="always")
    def test_manual_edit_before_paid_poll_checkpoint_survives_full_reprocess(self):
        self.failure = None
        document, _ = self.consume()
        original_remember = LlamaParseDocumentParser._remember_job
        edits = []

        def remember(parser, job):
            if job and job.get("state") == "polling":
                Document.objects.filter(pk=document.pk).update(
                    content="Concurrent manual archival annotation",
                )
                edits.append(True)
            return original_remember(parser, job)

        self.response_text = "New automatic OCR after explicit reprocess"
        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            mock.patch.object(
                LlamaParseDocumentParser,
                "_remember_job",
                autospec=True,
                side_effect=remember,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        self.assertTrue(edits)
        self.assertIn("Concurrent manual archival annotation", document.content)
