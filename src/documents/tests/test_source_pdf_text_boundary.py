"""Source PDF text remains safe for page evidence and deferred OCR storage."""

from pathlib import Path
from unittest import mock

import pikepdf
from django.test import override_settings

from documents.models import Document
from documents.ocr_queue import pending_pages
from documents.page_data import pdf_pages
from documents.page_data import stored_pages
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from documents.utils import compute_checksum
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless.parsers.remote import RemoteDocumentParser


def source_pdf(
    path: Path,
    pages: list[str | tuple[str, str] | None],
) -> Path:
    """Write visible text with optional PDF accessibility replacement text."""
    with pikepdf.new() as pdf:
        font = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica,
                Encoding=pikepdf.Name.WinAnsiEncoding,
            ),
        )
        for value in pages:
            page = pdf.add_blank_page(page_size=(180, 300))
            if value is None:
                continue
            visible, actual = value if isinstance(value, tuple) else (value, None)
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            content = b"BT /F1 12 Tf 20 200 Td " + pikepdf.String(visible).unparse()
            content += b" Tj ET"
            if actual is not None:
                replacement = (b"\xfe\xff" + actual.encode("utf-16-be")).hex()
                content = (
                    b"/Span << /ActualText <"
                    + replacement.encode("ascii")
                    + b"> >> BDC "
                    + content
                    + b" EMC"
                )
            page.Contents = pikepdf.Stream(pdf, content)
        pdf.save(path)
    return path


class SourcePDFTextBoundaryTests(RemoteOCRTestCase):
    def test_actualtext_unicode_nul_is_removed_from_page_evidence(self):
        source = source_pdf(
            self.dirs.scratch_dir / "actualtext_nul.pdf",
            [("AA", "\0AA")],
        )
        pages = pdf_pages(source)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["text"], "AA")
        self.assertNotIn("\0", pages[0]["text"])

    def test_blank_pages_and_page_boundaries_are_preserved(self):
        source = source_pdf(
            self.dirs.scratch_dir / "blank_boundaries.pdf",
            [None, "First", None, "Last", None],
        )
        pages = pdf_pages(source)
        self.assertEqual(
            [page["text"] for page in pages],
            ["", "First", "", "Last", ""],
        )
        self.assertEqual([page["source_page"] for page in pages], [1, 2, 3, 4, 5])
        self.assertEqual(len({page["origin"] for page in pages}), 5)

    def test_nul_cleanup_preserves_multipage_text_order(self):
        source = source_pdf(
            self.dirs.scratch_dir / "multipage_nul.pdf",
            ["First", ("Middle", "\0Middle"), "Last"],
        )
        pages = pdf_pages(source)
        self.assertEqual([page["text"] for page in pages], ["First", "Middle", "Last"])
        self.assertEqual([page["source_page"] for page in pages], [1, 2, 3])

    def test_normal_pdf_text_and_unicode_actualtext_are_preserved(self):
        source = source_pdf(
            self.dirs.scratch_dir / "normal_text.pdf",
            [
                "Ordinary PDF text",
                ("Accessible", "Accessible text"),
                ("Japanese", "東京"),
            ],
        )
        pages = pdf_pages(source)
        self.assertEqual(
            [page["text"] for page in pages],
            ["Ordinary PDF text", "Accessible text", "東京"],
        )

    @override_settings(ARCHIVE_FILE_GENERATION="always")
    def test_quota_upload_with_actualtext_nul_retains_identity_and_original(self):
        upload = source_pdf(
            self.dirs.consumption_dir / "actualtext_quota.pdf",
            [("AA", "\0AA")],
        )
        with mock.patch.object(
            LlamaParseDocumentParser,
            "get_thumbnail",
            new=RemoteDocumentParser.get_thumbnail,
        ):
            document, original = self.consume(upload=upload, asn=4701)

        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(Document.deleted_objects.count(), 0)
        self.assertEqual(
            Document.objects.get(archive_serial_number=4701).pk,
            document.pk,
        )
        self.assertEqual(document.title, "Water Supply Notices")
        self.assertEqual(document.content, "AA")
        self.assertEqual(document.page_count, 1)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(compute_checksum(document.source_path), document.checksum)
        self.assertTrue(document.thumbnail_path.is_file())
        self.assertGreater(document.thumbnail_path.stat().st_size, 0)
        self.assertIsNone(document.archive_path)
        self.assertIsNone(document.archive_checksum)
        self.assertFalse(upload.exists())
        self.assertEqual(
            set(document.tags.values_list("name", flat=True)),
            {self.user_tag.name, "ocr-pending", "ocr-fail"},
        )
        pages = stored_pages(document)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["text"], "AA")
        self.assertEqual(pages[0]["provenance"]["ocr_status"], "pending")
        self.assertEqual(pages[0]["provenance"]["reason"], "quota")
        self.assertEqual(len(pending_pages(document)), 1)
        self.assertEqual(self.create_requests, 1)
