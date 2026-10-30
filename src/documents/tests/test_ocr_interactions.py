"""Cross-feature OCR regressions confirmed by independent adversarial probes."""

import subprocess
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

import img2pdf
import pikepdf
from django.conf import settings
from django.test import override_settings
from guardian.shortcuts import assign_perm
from PIL import Image

from documents.conditionals import metadata_etag
from documents.file_updates import _publish as native_publish
from documents.file_updates import update_file
from documents.models import Document
from documents.models import MatchingModel
from documents.models import Tag
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.ocr_queue import defer_existing_ocr
from documents.ocr_queue import pending_pages
from documents.ocr_queue import sync_ocr_tags
from documents.ocr_status import status_tag
from documents.page_data import stored_pages
from documents.page_enrichment import enrich_document
from documents.page_splitting import split_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_file_updates import FakeParser
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_page_data import response as cloud_response
from documents.tests.test_pdf_editor_updates import searchable_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase as Base
from documents.utils import compute_checksum
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless.parsers.remote import RemoteDocumentParser
from paperless.parsers.utils import extract_pdf_text
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory


class OCRInteractionTests(Base):
    @override_settings(
        REMOTE_OCR_MODE="workflow_only",
        ARCHIVE_FILE_GENERATION="always",
    )
    def test_local_ocr_recovery_obeys_current_only_storage(self):
        class LocalParser(FakeParser):
            requires_pdf_rendition = True

        document, _ = self.consume()
        old_source = document.source_path
        calls = self.create_requests
        with (
            mock.patch("documents.tasks.get_parser_registry") as registry,
            self.captureOnCommitCallbacks(execute=True),
        ):
            registry.return_value.get_parser_for_file.return_value = LocalParser
            update_document_content_maybe_archive_file(document.pk, remote_ocr=False)
        document.refresh_from_db()
        self.assertIsNone(document.archive_path)
        self.assertTrue(document.source_path.is_file())
        self.assertFalse(old_source.exists() and old_source != document.source_path)
        self.assertFalse(pending_pages(document))
        self.assertEqual(self.create_requests, calls)

    @override_settings(ARCHIVE_FILE_GENERATION="always", OCR_ROTATE_PAGES=True)
    def test_completed_reprocess_preserves_manual_rotation_and_page_identity(self):
        self.failure = None
        document, _ = self.consume()
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1, "rotate": 90}],
            )
        document.refresh_from_db()
        previous = stored_pages(document)[0]
        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            mock.patch.object(
                LlamaParseDocumentParser,
                "_orient",
                side_effect=AssertionError("manual rotation must be preserved"),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        current = stored_pages(document)[0]
        self.assertEqual(current["origin"], previous["origin"])
        self.assertEqual(current["rotation"], 90)
        self.assertEqual(current["provenance"]["layout_rotation"], 90)
        self.assertTrue(current["provenance"]["manual_rotation"])

    def test_malformed_remote_bbox_cannot_reject_valid_upload(self):
        self.failure = None

        def malformed_result(text):
            result = cloud_response(text)
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

    def test_immediate_success_exposes_ocr_ok_to_added_workflow(self):
        workflow = self.added_title_workflow()
        workflow.triggers.first().filter_has_tags.add(status_tag("ocr-ok"))
        self.failure = None
        document, _ = self.consume()
        self.assertEqual(document.title, "Independent workflow title")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)

    def test_layout_whitespace_does_not_duplicate_automatic_content(self):
        document, _ = self.consume(text="Short     prior     text")
        self.assertEqual(document.content, "Short prior text")
        self.failure = None
        self.response_text = "Short prior text\nNow complete OCR"
        self.retry(document)
        self.assertEqual(document.content, self.response_text)

    def test_automatic_osd_cannot_undo_pending_manual_rotation(self):
        document, _ = self.consume()
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1, "rotate": 90}],
            )
        document.refresh_from_db()
        with pikepdf.open(document.source_path) as before:
            box = before.pages[0].mediabox
            expected = (
                (float(box[3]), float(box[2]))
                if before.pages[0].rotation in (90, 270)
                else (float(box[2]), float(box[3]))
            )
        self.failure = None
        native_run = subprocess.run

        def deterministic_osd(command, *args, **kwargs):
            if command[0] == "tesseract":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "Rotate: 270\nOrientation confidence: 20\n",
                    "",
                )
            return native_run(command, *args, **kwargs)

        with (
            override_settings(OCR_ROTATE_PAGES=True),
            mock.patch(
                "paperless.parsers.llamaparse.subprocess.run",
                side_effect=deterministic_osd,
            ),
        ):
            self.retry(document)
        with pikepdf.open(document.source_path) as after:
            box = after.pages[0].mediabox
            actual = (
                (float(box[3]), float(box[2]))
                if after.pages[0].rotation in (90, 270)
                else (float(box[2]), float(box[3]))
            )
        self.assertEqual(actual, expected)
        self.assertEqual(stored_pages(document)[0]["rotation"], 90)

    def test_owned_legacy_failure_without_page_markers_can_retry_in_place(self):
        owner = UserFactory()
        legacy = TagFactory(
            name="ocr-fail",
            owner=owner,
            matching_algorithm=MatchingModel.MATCH_NONE,
        )
        document = DocumentFactory(
            mime_type="application/pdf",
            content="",
            page_count=1,
            original_filename="legacy_scan.pdf",
        )
        write_pdf(document.source_path, [180])
        original = document.source_path.read_bytes()
        document.thumbnail_path.write_bytes(b"thumbnail")
        Document.objects.filter(pk=document.pk).update(
            checksum=compute_checksum(document.source_path),
        )
        document.refresh_from_db()
        document.tags.add(legacy)
        self.assertFalse(pending_pages(document))
        with override_settings(OCR_STATUS_TAG_IDS={"ocr-fail": legacy.pk}):
            with (
                mock.patch(
                    "documents.tasks.update_document_content_maybe_archive_file.delay",
                ) as enqueue,
                self.captureOnCommitCallbacks(execute=True),
            ):
                document.tags.remove(legacy)
            self.assertTrue(pending_pages(document))
            self.assertEqual(document.source_path.read_bytes(), original)
            self.assertEqual(self.requests, [])
            self.assertTrue(enqueue.call_args.kwargs.get("pending_only"))
            self.failure = None
            with self.captureOnCommitCallbacks(execute=True):
                update_document_content_maybe_archive_file.run(
                    *enqueue.call_args.args,
                    **enqueue.call_args.kwargs,
                )
            document.refresh_from_db()
            self.assertFalse(pending_pages(document))
            self.assertFalse(document.tags.filter(pk=legacy.pk).exists())
        self.assertEqual(Tag.objects.get(pk=legacy.pk).owner_id, owner.pk)

    def test_successful_blank_page_cannot_be_requeued_as_legacy(self):
        def blank_result(text):
            result = cloud_response("")
            result["markdown"]["pages"][0]["markdown"] = ""
            result["items"]["pages"][0]["items"] = []
            return result

        document, _ = self.consume()
        self.failure = None
        with (
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                side_effect=blank_result,
            ),
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
        ):
            self.assertEqual(self.retry(document), 1)
        self.assertFalse(pending_pages(document))
        self.assertEqual(document.content, "")
        enqueue.assert_not_called()
        calls = self.create_requests
        self.assertEqual(self.retry(document), 0)
        self.assertEqual(self.create_requests, calls)

    def test_partial_progress_cannot_cancel_added_lifecycle_via_updated_workflow(self):
        added = self.added_title_workflow()
        updated = Workflow.objects.create(name="Intermediate update action")
        updated.triggers.add(
            WorkflowTrigger.objects.create(
                type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
                matching_algorithm=MatchingModel.MATCH_NONE,
            ),
        )
        updated.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                assign_title="Title changed by intermediate update",
            ),
        )
        upload = write_pdf(settings.CONSUMPTION_DIR / "two_pages.pdf", [180, 300])
        document, _ = self.consume(upload=upload)
        self.failure = None
        with mock.patch("documents.file_updates._publish", new=native_publish):
            self.retry(document, max_pages=1)
            self.retry(document)
        self.assertEqual(WorkflowRun.objects.filter(workflow=added).count(), 1)
        self.assertLessEqual(WorkflowRun.objects.filter(workflow=updated).count(), 1)

    def test_internal_status_alias_swap_cannot_request_paid_retry(self):
        document, _ = self.consume()
        legacy = TagFactory(
            name="ocr-fail",
            owner=UserFactory(),
            matching_algorithm=MatchingModel.MATCH_NONE,
        )
        with (
            override_settings(OCR_STATUS_TAG_IDS={"ocr-fail": legacy.pk}),
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            sync_ocr_tags(document.pk)
        enqueue.assert_not_called()
        self.assertTrue(document.tags.filter(pk=legacy.pk).exists())

    @override_settings(ARCHIVE_FILE_GENERATION="auto")
    def test_delayed_auto_retry_keeps_completed_rich_evidence(self):
        self.response_text = "\n".join(
            ["This is searchable document text for a water notice."] * 8,
        )

        def body_result(text):
            result = cloud_response(text)
            result["items"]["pages"][0]["items"] = [
                {
                    "type": "text",
                    "value": text,
                    "bbox": [{"x": 1, "y": 1, "w": 98, "h": 150}],
                },
            ]
            return result

        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            side_effect=body_result,
        ):
            document, _ = self.consume()
            with (
                mock.patch(
                    "documents.tasks.update_document_content_maybe_archive_file.delay",
                ) as enqueue,
                self.captureOnCommitCallbacks(execute=True),
            ):
                document.tags.remove(document.tags.get(name="ocr-fail"))
            self.failure = None
            self.retry(document)
            original_layout = stored_pages(document)[0]["layout"]
            self.assertIsNotNone(original_layout)
            calls = self.create_requests
            with mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ):
                update_document_content_maybe_archive_file.run(
                    *enqueue.call_args.args,
                    **enqueue.call_args.kwargs,
                )
        document.refresh_from_db()
        self.assertEqual(self.create_requests, calls)
        self.assertEqual(stored_pages(document)[0]["layout"], original_layout)

    def test_pending_retry_for_deleted_document_is_noop(self):
        document, _ = self.consume()
        with (
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            document.tags.remove(document.tags.get(name="ocr-fail"))
        pk = document.pk
        document.delete()
        calls = self.create_requests
        update_document_content_maybe_archive_file.run(
            *enqueue.call_args.args,
            **enqueue.call_args.kwargs,
        )
        self.assertEqual(self.create_requests, calls)
        self.assertTrue(Document.deleted_objects.filter(pk=pk).exists())
        self.assertFalse(Document.objects.exists())

    @override_settings(ARCHIVE_FILE_GENERATION="always")
    def test_delayed_failure_tag_job_cannot_reocr_completed_document(self):
        document, _ = self.consume()
        with (
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            document.tags.remove(document.tags.get(name="ocr-fail"))
        self.failure = None
        self.retry(document)
        calls = self.create_requests
        with mock.patch(
            "documents.tasks.get_parser_registry",
            return_value=self.registry.return_value,
        ):
            update_document_content_maybe_archive_file.run(
                *enqueue.call_args.args,
                **enqueue.call_args.kwargs,
            )
        self.assertEqual(self.create_requests, calls)

    def added_title_workflow(self):
        workflow = Workflow.objects.create(name="Independent content workflow")
        workflow.triggers.add(
            WorkflowTrigger.objects.create(
                type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
                matching_algorithm=MatchingModel.MATCH_LITERAL,
                match="Water Supply",
            ),
        )
        workflow.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                assign_title="Independent workflow title",
            ),
        )
        return workflow

    def test_deleting_last_pending_page_finishes_workflow_without_paid_ocr(self):
        workflow = self.added_title_workflow()
        document, _ = self.consume()
        upload = write_pdf(self.dirs.scratch_dir / "later.pdf", [300])
        with self.captureOnCommitCallbacks(execute=True):
            update_file(document.pk, upload, append=True, keep_only_current_file=True)
        self.failure = None
        self.retry(document, max_pages=1)
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 0)
        calls = self.create_requests
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1}],
            )
        document.refresh_from_db()
        self.assertEqual(document.title, "Independent workflow title")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)
        self.assertEqual(self.create_requests, calls)

    def test_split_target_does_not_inherit_original_ingestion_workflow(self):
        workflow = self.added_title_workflow()
        document, _ = self.consume()
        with (
            mock.patch(
                "documents.page_splitting.make_thumbnail_from_pdf",
                return_value=document.thumbnail_path,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = split_pages(
                document.pk,
                operations=[{"page": 1}],
                source_checksum=document.checksum,
                title="Chosen extracted title",
            )
        target = Document.objects.get(pk=result["new_document_id"])
        self.failure = None
        self.retry(target)
        self.assertEqual(target.title, "Chosen extracted title")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 0)
        document.refresh_from_db()
        self.assertEqual(document.title, "Water Supply Notices")

    def test_unrelated_tag_permission_cannot_skip_document_metadata(self):
        workflow = self.added_title_workflow()
        document, _ = self.consume()
        unrelated = Tag.objects.filter(pk=document.pk).first()
        if unrelated is None:
            unrelated = TagFactory(pk=document.pk)
        assign_perm("view_tag", UserFactory(), unrelated)
        self.failure = None
        self.retry(document)
        self.assertEqual(document.title, "Independent workflow title")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)

    def test_refresh_preserves_existing_visible_pdf_text(self):
        source = searchable_pdf(
            self.dirs.scratch_dir / "visible.pdf",
            ["Visible trusted text"],
        )
        self.failure = None
        with LlamaParseDocumentParser() as parser:
            parser.parse(source, "application/pdf", produce_archive=True)
            self.assertIn(
                "Visible trusted text",
                extract_pdf_text(parser.get_archive_path()),
            )

    def test_refresh_keeps_compressed_jpeg_stream_intact(self):
        image = Image.new("RGB", (180, 240), (241, 235, 214))
        encoded = BytesIO()
        image.save(encoded, format="JPEG", quality=75)
        upload = settings.CONSUMPTION_DIR / "jpeg_scan.pdf"
        upload.write_bytes(img2pdf.convert(encoded.getvalue()))
        with pikepdf.open(upload) as before:
            original = next(iter(before.pages[0].images.values())).read_raw_bytes()
        document, _ = self.consume(upload=upload)
        self.failure = None
        self.retry(document)
        with pikepdf.open(document.source_path) as after:
            result = next(iter(after.pages[0].images.values())).read_raw_bytes()
        self.assertEqual(original, result)

    def test_private_status_name_collision_does_not_block_retry(self):
        TagFactory(name="ocr-processing", owner=UserFactory())
        TagFactory(name="ocr-processing", owner=UserFactory())
        document, _ = self.consume()
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertTrue(
            document.tags.filter(name="ocr-ok", owner__isnull=True).exists(),
        )

    def test_later_page_retry_keeps_first_page_thumbnail(self):
        def make_thumbnail(source, work, *args):
            with pikepdf.open(source) as pdf:
                width = float(pdf.pages[0].mediabox[2])
            result = work / "thumbnail.webp"
            result.write_bytes(str(width).encode())
            return result

        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "get_thumbnail",
                new=RemoteDocumentParser.get_thumbnail,
            ),
            mock.patch(
                "documents.parsers.make_thumbnail_from_pdf",
                side_effect=make_thumbnail,
            ),
            mock.patch(
                "documents.file_updates.make_thumbnail_from_pdf",
                side_effect=make_thumbnail,
            ),
        ):
            document, _ = self.consume()
            upload = write_pdf(self.dirs.scratch_dir / "later.pdf", [300])
            with self.captureOnCommitCallbacks(execute=True):
                update_file(
                    document.pk,
                    upload,
                    append=True,
                    keep_only_current_file=True,
                )
            self.failure = None
            self.retry(document)
        self.assertEqual(document.thumbnail_path.read_bytes(), b"180.0")

    def test_reprocess_retry_replaces_old_pdf_text_layer(self):
        def result_with_printed_words(text):
            result = cloud_response(text)
            result["items"]["pages"][0]["items"] = [
                {
                    "type": "text",
                    "value": text,
                    "bbox": [
                        {
                            "x": 10,
                            "y": 10 if text.startswith("OLD") else 80,
                            "w": 80,
                            "h": 20,
                        },
                    ],
                },
            ]
            return result

        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            side_effect=result_with_printed_words,
        ):
            document, _ = self.consume()
            self.failure = None
            self.response_text = "OLD OCR WORDS"
            self.retry(document)
            with self.captureOnCommitCallbacks(execute=True):
                defer_existing_ocr(document, {"status": "pending", "reason": "quota"})
            self.response_text = "CORRECTED OCR WORDS"
            self.retry(document)
        text = extract_pdf_text(document.source_path)
        self.assertIn("CORRECTED OCR WORDS", text)
        self.assertNotIn("OLD OCR WORDS", text)

    @override_settings(FILENAME_FORMAT="{title}")
    def test_user_title_edit_renames_source_during_ocr(self):
        document, _ = self.consume()
        self.failure = None
        request = LlamaParseDocumentParser._request_result

        def edit_while_waiting(parser, *args):
            result = request(parser, *args)
            current = Document.objects.get(pk=document.pk)
            current.title = "User title while OCR runs"
            current.save()
            return result

        with mock.patch.object(
            LlamaParseDocumentParser,
            "_request_result",
            autospec=True,
            side_effect=edit_while_waiting,
        ):
            self.assertEqual(self.retry(document), 1)
        self.assertEqual(document.title, "User title while OCR runs")
        self.assertTrue(document.source_path.is_file())

    @override_settings(STORE_ONLY_CURRENT_FILE=False)
    def test_retry_invalidates_metadata_etag(self):
        document, _ = self.consume()
        before = metadata_etag(SimpleNamespace(query_params={}), document.pk)
        self.failure = None
        self.retry(document)
        after = metadata_etag(SimpleNamespace(query_params={}), document.pk)
        self.assertNotEqual(before, after)

    def test_legacy_image_can_be_queued_after_failed_reprocess(self):
        document = DocumentFactory(
            mime_type="image/png",
            original_filename="legacy.png",
            page_count=1,
            content="",
            archive_filename=None,
            archive_checksum=None,
        )
        Image.new("RGB", (180, 240), "white").save(
            document.source_path,
            format="PNG",
            dpi=(150, 150),
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
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertTrue(pending_pages(document))
        self.assertTrue(document.tags.filter(name="ocr-pending").exists())

    def test_pending_rotation_records_layout_coordinate_system(self):
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
        self.assertEqual(page["provenance"].get("layout_rotation", 0), 90)

    def test_retry_refreshes_orientation_thumbnail(self):
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
