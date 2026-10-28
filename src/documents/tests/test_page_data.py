from __future__ import annotations

import json
import uuid
from io import StringIO
from types import SimpleNamespace
from unittest import mock

import pikepdf
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from guardian.shortcuts import assign_perm
from rest_framework.test import APITestCase

from documents.file_updates import compact_current_files
from documents.file_updates import update_file
from documents.models import Document
from documents.models import DocumentPage
from documents.page_data import backfill_document
from documents.page_data import pages_for_file
from documents.page_data import parser_pages
from documents.page_data import save_pages
from documents.page_data import stored_pages
from documents.page_enrichment import enrich_document
from documents.page_splitting import split_pages
from documents.tests.test_file_updates import FakeParser
from documents.tests.test_pdf_editor_updates import searchable_pdf
from documents.utils import compute_checksum
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global


def rich_pages(labels):
    return [
        {
            "origin": uuid.uuid4(),
            "source_page": index,
            "rotation": 0,
            "text": label,
            "markdown": f"# {label}\n\n| Water | Cost |\n|---|---|\n| 5 | 10 |",
            "layout": {
                "items": [
                    {
                        "type": "table",
                        "rows": [["5", "10"]],
                        "bbox": [{"x": 10, "y": 20, "w": 30, "h": 40}],
                    },
                ],
            },
            "provenance": {"engine": "llamaparse", "job_id": "fixture-job"},
        }
        for index, label in enumerate(labels, 1)
    ]


def response(text="Enriched notice"):
    return {
        "job": {"id": "fixture-job", "status": "COMPLETED"},
        "text": {"pages": [{"page_number": 1, "text": text}]},
        "markdown": {
            "pages": [
                {
                    "page_number": 1,
                    "markdown": f"# {text}\n\n| Water | Cost |\n|---|---|\n|5|10|",
                },
            ],
        },
        "items": {
            "pages": [
                {
                    "page_number": 1,
                    "page_width": 100,
                    "page_height": 500,
                    "items": [{"type": "table", "rows": [["5", "10"]]}],
                },
            ],
        },
    }


class PageFixture(DirectoriesMixin):
    def make_document(self):
        self.document = DocumentFactory(
            title="Water Supply Notices",
            archive_serial_number=1000001,
            content="Manual document content",
            mime_type="application/pdf",
        )
        searchable_pdf(
            self.document.source_path,
            ["First notice", "Second notice", "Third notice"],
        )
        self.document.thumbnail_path.write_bytes(b"thumbnail")
        Document.objects.filter(pk=self.document.pk).update(
            checksum=compute_checksum(self.document.source_path),
            page_count=3,
        )
        self.document.refresh_from_db()
        self.fingerprint = self.document.checksum
        self.old_bytes = self.document.source_path.read_bytes()
        self.publish = mock.patch("documents.file_updates._publish").start()
        self.addCleanup(mock.patch.stopall)

        def thumbnail(source, work):
            target = work / "thumbnail.webp"
            target.write_bytes(b"new thumbnail")
            return target

        mock.patch(
            "documents.page_splitting.make_thumbnail_from_pdf",
            side_effect=thumbnail,
        ).start()
        mock.patch(
            "documents.file_updates.make_thumbnail_from_pdf",
            side_effect=thumbnail,
        ).start()
        self.ocr = mock.patch("documents.file_updates.get_parser_registry").start()
        self.enrich_request = mock.patch(
            "paperless.parsers.llamaparse.LlamaParseDocumentParser._request_result",
            return_value=response(),
        ).start()
        mock.patch(
            "documents.page_enrichment.RemoteOCRConfig",
            return_value=SimpleNamespace(
                remote_ocr_engine="llamaparse",
                remote_ocr_api_key="fixture-only",
            ),
        ).start()

    def split(self, **kwargs):
        arguments = dict(
            operations=[{"page": 3, "rotate": 90}, {"page": 1}],
            source_checksum=self.document.checksum,
        )
        arguments.update(kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            return split_pages(self.document.pk, **arguments)


class PageDataTests(PageFixture, TestCase):
    def setUp(self):
        self.make_document()

    def test_trash_and_restore_preserve_rich_page_records(self):
        pages = rich_pages(["First", "Second", "Third"])
        save_pages(self.document, pages)
        identifiers = list(DocumentPage.objects.values_list("pk", flat=True))
        self.document.delete()
        self.assertEqual(DocumentPage.objects.count(), 0)
        self.assertEqual(DocumentPage.global_objects.count(), 3)
        trashed = Document.deleted_objects.get(pk=self.document.pk)
        trashed.restore(strict=False)
        self.document.refresh_from_db()
        self.assertEqual(
            list(DocumentPage.objects.values_list("pk", flat=True)),
            identifiers,
        )
        self.assertEqual(stored_pages(self.document), pages)

    def test_permanent_deletion_removes_rich_page_records(self):
        save_pages(self.document, rich_pages(["First", "Second", "Third"]))
        self.document.delete()
        Document.deleted_objects.filter(pk=self.document.pk).delete()
        self.assertEqual(DocumentPage.global_objects.count(), 0)

    def test_backfill_is_lossless_local_and_idempotent(self):
        metadata = (
            self.document.pk,
            self.document.archive_serial_number,
            self.document.created,
            self.document.modified,
            self.document.content,
        )
        self.assertTrue(backfill_document(self.document.pk))
        self.assertFalse(backfill_document(self.document.pk))
        self.document.refresh_from_db()
        self.assertEqual(
            metadata,
            (
                self.document.pk,
                self.document.archive_serial_number,
                self.document.created,
                self.document.modified,
                self.document.content,
            ),
        )
        self.assertEqual(self.document.source_path.read_bytes(), self.old_bytes)
        pages = stored_pages(self.document)
        self.assertEqual(
            [page["text"] for page in pages],
            ["First notice", "Second notice", "Third notice"],
        )
        self.assertTrue(
            all(page["markdown"] is None and page["layout"] is None for page in pages),
        )
        self.ocr.assert_not_called()
        self.enrich_request.assert_not_called()

    def test_enriched_subset_can_be_split_and_enriched_without_repeating_saved_pages(
        self,
    ):
        enrich_document(self.document.pk, max_pages=2)
        result = self.split(operations=[{"page": 2}, {"page": 3}])
        self.assertEqual(self.enrich_request.call_count, 2)
        target = Document.objects.get(pk=result["new_document_id"])
        self.assertIsNotNone(stored_pages(target)[0]["layout"])
        self.assertIsNone(stored_pages(target)[1]["layout"])
        self.assertEqual(enrich_document(target.pk, max_pages=10), 1)
        self.assertEqual(self.enrich_request.call_count, 3)
        self.assertEqual(enrich_document(target.pk, max_pages=10), 0)

    def test_enrichment_after_rotation_records_the_layout_orientation(self):
        save_pages(self.document, rich_pages(["First", "Second", "Third"]))
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                self.document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1, "rotate": 90}, {"page": 2}],
            )
        self.document.parsed_pages.update(layout=None, markdown=None)
        self.assertEqual(enrich_document(self.document.pk, max_pages=1), 1)
        self.document.refresh_from_db()
        page = stored_pages(self.document)[0]
        self.assertEqual(page["rotation"], 90)
        self.assertEqual(page["provenance"]["layout_rotation"], 90)

    def test_second_install_failure_rolls_back_a_completed_first_install(self):
        from documents.file_updates import _install

        old_files = set(self.dirs.originals_dir.rglob("*.pdf"))
        calls = 0

        def install(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return _install(*args, **kwargs)

        with mock.patch("documents.page_splitting._install", side_effect=install):
            with self.assertRaises(OSError):
                self.split(move=True, keep_only_current_file=True)
        self.document.refresh_from_db()
        self.assertEqual(self.document.source_path.read_bytes(), self.old_bytes)
        self.assertEqual(set(self.dirs.originals_dir.rglob("*.pdf")), old_files)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(DocumentPage.objects.count(), 0)

    def test_backfill_keeps_blank_pages(self):
        searchable_pdf(self.document.source_path, ["First", "", "Last"])
        Document.objects.filter(pk=self.document.pk).update(
            checksum=compute_checksum(self.document.source_path),
        )
        backfill_document(self.document.pk)
        self.assertEqual(
            list(self.document.parsed_pages.values_list("text", flat=True)),
            ["First", "", "Last"],
        )

    def test_stale_records_are_not_returned_or_reused(self):
        save_pages(self.document, rich_pages(["Wrong", "Wrong", "Wrong"]))
        Document.objects.filter(pk=self.document.pk).update(checksum="new-checksum")
        self.document.refresh_from_db()
        self.assertIsNone(stored_pages(self.document))
        recovered = pages_for_file(self.document, self.document.source_path)
        self.assertEqual(recovered[0]["text"], "First notice")

    def test_backfill_rejects_changed_pdf(self):
        self.document.source_path.write_bytes(b"damaged")
        with self.assertRaisesMessage(ValueError, "checksum"):
            backfill_document(self.document.pk)
        self.assertEqual(DocumentPage.objects.count(), 0)

    def test_backfill_command_preview_and_resumption(self):
        output = StringIO()
        call_command("document_page_data", "backfill", stdout=output)
        self.assertEqual(DocumentPage.objects.count(), 0)
        call_command("document_page_data", "backfill", "--apply", stdout=output)
        identifiers = list(DocumentPage.objects.values_list("pk", flat=True))
        call_command("document_page_data", "backfill", "--apply", stdout=output)
        self.assertEqual(
            list(DocumentPage.objects.values_list("pk", flat=True)),
            identifiers,
        )

    def test_enrichment_retains_files_identity_and_existing_content(self):
        count = enrich_document(self.document.pk, max_pages=2)
        self.assertEqual(count, 2)
        self.document.refresh_from_db()
        pages = stored_pages(self.document)
        self.assertEqual(pages[0]["layout"]["items"][0]["rows"], [["5", "10"]])
        self.assertIn("| Water |", pages[0]["markdown"])
        self.assertIsNone(pages[2]["layout"])
        self.assertEqual(self.document.source_path.read_bytes(), self.old_bytes)
        self.assertEqual(self.document.checksum, self.fingerprint)
        self.assertEqual(self.document.archive_serial_number, 1000001)
        self.assertEqual(self.document.content, "Manual document content")
        self.assertEqual(enrich_document(self.document.pk, max_pages=10), 1)
        self.assertEqual(self.enrich_request.call_count, 3)
        self.assertEqual(enrich_document(self.document.pk, max_pages=10), 0)

    def test_enrichment_failure_saves_previous_success_and_resume_skips_it(self):
        self.enrich_request.side_effect = [response(), RuntimeError("HTTP 402")]
        with self.assertRaisesMessage(RuntimeError, "402"):
            enrich_document(self.document.pk, max_pages=3)
        self.assertEqual(DocumentPage.objects.filter(markdown__isnull=False).count(), 1)
        self.enrich_request.side_effect = None
        self.assertEqual(enrich_document(self.document.pk, max_pages=3), 2)

    def test_enrichment_rejects_concurrent_revision_change(self):
        def changed(*args):
            Document.objects.filter(pk=self.document.pk).update(checksum="changed")
            return response()

        self.enrich_request.side_effect = changed
        with self.assertRaisesMessage(ValueError, "changed"):
            enrich_document(self.document.pk, max_pages=1)
        self.assertFalse(DocumentPage.objects.filter(markdown__isnull=False).exists())

    def test_paid_command_requires_explicit_apply_and_page_limit(self):
        call_command("document_page_data", "enrich", stdout=StringIO())
        self.enrich_request.assert_not_called()
        with self.assertRaises(CommandError):
            call_command("document_page_data", "enrich", "--apply")
        call_command(
            "document_page_data",
            "enrich",
            "--apply",
            "--max-pages",
            "1",
            stdout=StringIO(),
        )
        self.assertEqual(self.enrich_request.call_count, 1)

    def test_extract_preserves_rich_data_permissions_and_source(self):
        owner, viewer = UserFactory(), UserFactory()
        Document.objects.filter(pk=self.document.pk).update(owner=owner)
        self.document.refresh_from_db()
        assign_perm("view_document", viewer, self.document)
        pages = rich_pages(["First notice", "Second notice", "Third notice"])
        save_pages(self.document, pages)
        result = self.split()
        target = Document.objects.get(pk=result["new_document_id"])
        self.document.refresh_from_db()
        self.assertEqual(self.document.source_path.read_bytes(), self.old_bytes)
        self.assertEqual(target.content, "Third notice\n\nFirst notice")
        self.assertIsNone(target.archive_serial_number)
        self.assertEqual(target.owner, owner)
        self.assertTrue(viewer.has_perm("documents.view_document", target))
        selected = stored_pages(target)
        self.assertEqual(
            [page["origin"] for page in selected],
            [pages[2]["origin"], pages[0]["origin"]],
        )
        self.assertEqual(selected[0]["layout"], pages[2]["layout"])
        self.assertEqual(selected[0]["rotation"], 90)
        with pikepdf.open(target.source_path) as pdf:
            self.assertEqual(len(pdf.pages), 2)
            self.assertEqual(pdf.pages[0].get("/Rotate"), 90)
        self.ocr.assert_not_called()
        self.enrich_request.assert_not_called()

    def test_move_updates_current_source_without_ocr(self):
        save_pages(self.document, rich_pages(["First", "Second", "Third"]))
        result = self.split(
            move=True,
            keep_only_current_file=True,
            archive_serial_number=1000002,
        )
        self.document.refresh_from_db()
        target = Document.objects.get(pk=result["new_document_id"])
        self.assertEqual(self.document.content, "Second")
        self.assertEqual(self.document.archive_serial_number, 1000001)
        self.assertEqual(self.document.page_count, 1)
        self.assertEqual(target.archive_serial_number, 1000002)
        self.assertEqual(len(stored_pages(self.document)), 1)
        self.ocr.assert_not_called()

    def test_move_with_history_keeps_each_revisions_page_data(self):
        save_pages(self.document, rich_pages(["First", "Second", "Third"]))
        self.split(move=True, keep_only_current_file=False)
        self.document.refresh_from_db()
        version = self.document.versions.get()
        self.assertEqual(len(stored_pages(self.document)), 3)
        self.assertEqual(stored_pages(version)[0]["text"], "Second")
        with self.captureOnCommitCallbacks(execute=True):
            compact_current_files(self.document.pk)
        self.document.refresh_from_db()
        self.assertEqual(stored_pages(self.document)[0]["text"], "Second")
        self.assertFalse(DocumentPage.objects.filter(document=version.pk).exists())

    def test_empty_or_duplicate_selection_is_rejected_without_changes(self):
        for operations in (
            [],
            [{"page": 1}, {"page": 1}],
            [{"page": 9}],
            [{"page": 1}, {"page": 2}, {"page": 3}],
        ):
            with self.assertRaises(ValueError):
                self.split(move=True, operations=operations)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(self.document.source_path.read_bytes(), self.old_bytes)

    def test_split_failure_rolls_back_documents_files_and_page_records(self):
        save_pages(self.document, rich_pages(["First", "Second", "Third"]))
        old_files = set(self.dirs.originals_dir.rglob("*.pdf"))
        with mock.patch(
            "documents.page_splitting._copy_metadata",
            side_effect=RuntimeError("failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.split(move=True, keep_only_current_file=True)
        self.document.refresh_from_db()
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(self.document.source_path.read_bytes(), self.old_bytes)
        self.assertEqual(set(self.dirs.originals_dir.rglob("*.pdf")), old_files)
        self.assertEqual(len(stored_pages(self.document)), 3)

    def test_reorder_delete_and_rotation_keep_rich_data_without_reparse(self):
        pages = rich_pages(["First", "Second", "Third"])
        save_pages(self.document, pages)
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                self.document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 3, "rotate": 180}, {"page": 1}],
            )
        self.document.refresh_from_db()
        result = stored_pages(self.document)
        self.assertEqual(result[0]["markdown"], pages[2]["markdown"])
        self.assertEqual(result[0]["rotation"], 180)
        self.assertNotIn("Second", self.document.content)
        self.ocr.assert_not_called()

    def test_append_preserves_rich_results_and_parses_only_upload(self):
        pages = rich_pages(["First", "Second", "Third"])
        save_pages(self.document, pages)
        upload = searchable_pdf(self.dirs.scratch_dir / "upload.pdf", ["Fourth"])
        self.ocr.return_value.get_parser_for_file.return_value = FakeParser
        with self.captureOnCommitCallbacks(execute=True):
            update_file(
                self.document.pk,
                upload,
                append=True,
                keep_only_current_file=True,
            )
        self.document.refresh_from_db()
        result = stored_pages(self.document)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[2]["layout"], pages[2]["layout"])
        self.assertIsNone(result[3]["layout"])
        self.assertEqual(
            self.ocr.return_value.get_parser_for_file.call_args.args[2],
            upload,
        )

    def test_parser_results_strip_expiring_image_urls(self):
        class Parser:
            def get_review_pages(self):
                return [
                    {
                        "page_number": i,
                        "text": "Text",
                        "markdown": "# Text",
                        "items": [
                            {
                                "type": "image",
                                "url": "https://invalid/?token=secret",
                                "caption": "Figure",
                            },
                        ],
                    }
                    for i in range(1, 4)
                ]

        pages = parser_pages(Parser(), self.document.source_path)
        self.assertNotIn("token=secret", json.dumps(pages, default=str))
        self.assertEqual(pages[0]["layout"]["items"][0]["caption"], "Figure")


class PageApiTests(PageFixture, APITestCase):
    def setUp(self):
        self.make_document()
        self.owner = UserFactory()
        grant_global(
            self.owner,
            "view_document",
            "add_document",
            "change_document",
            "delete_document",
        )
        Document.objects.filter(pk=self.document.pk).update(owner=self.owner)
        self.document.refresh_from_db()
        self.client.force_authenticate(self.owner)

    def test_page_api_returns_rich_data_and_requires_document_access(self):
        save_pages(self.document, rich_pages(["First", "Second", "Third"]))
        url = f"/api/documents/{self.document.pk}/page_data/"
        result = self.client.get(url)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["pages"][0]["layout"]["items"][0]["type"], "table")
        outsider = UserFactory()
        grant_global(outsider, "view_document")
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_page_api_selects_root_revision_explicitly_when_a_newer_version_exists(
        self,
    ):
        save_pages(
            self.document,
            rich_pages(["Root first", "Root second", "Root third"]),
        )
        version = DocumentFactory(
            root_document=self.document,
            version_index=1,
            page_count=1,
        )
        save_pages(version, rich_pages(["Latest version"]))
        url = f"/api/documents/{self.document.pk}/page_data/"
        latest = self.client.get(url)
        self.assertEqual(latest.data["source_document_id"], version.pk)
        original = self.client.get(url, {"version": self.document.pk})
        self.assertEqual(original.data["source_document_id"], self.document.pk)
        self.assertEqual(original.data["pages"][0]["text"], "Root first")

    def test_split_api_requires_stale_guard_and_returns_new_document(self):
        url = f"/api/documents/{self.document.pk}/split_pages/"
        payload = {"operations": [{"page": 2}], "source_checksum": "stale"}
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 400)
        payload["source_checksum"] = self.document.checksum
        with self.captureOnCommitCallbacks(execute=True):
            result = self.client.post(url, payload, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(
            Document.objects.get(pk=result.data["new_document_id"]).content,
            "Second notice",
        )
        self.ocr.assert_not_called()

    def test_asn_collision_and_no_permission_are_rejected(self):
        url = f"/api/documents/{self.document.pk}/split_pages/"
        payload = {
            "operations": [{"page": 1}],
            "source_checksum": self.fingerprint,
            "archive_serial_number": 1000001,
        }
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 400)
        self.client.force_authenticate(UserFactory())
        del payload["archive_serial_number"]
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 403)
