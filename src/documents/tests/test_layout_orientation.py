"""Provider boxes map to rendered content through the documented rotation basis."""

import subprocess
from unittest import mock

import pikepdf
from django.test import override_settings
from PIL import Image

from documents.file_updates import update_file
from documents.page_data import stored_pages
from documents.tests.test_page_data import response
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase as Base
from paperless.parsers.llamaparse import LlamaParseDocumentParser


class LayoutOrientationTests(Base):
    def printed_rectangle(self):
        path = self.dirs.consumption_dir / "printed_rectangle.pdf"
        with pikepdf.new() as pdf:
            page = pdf.add_blank_page(page_size=(180, 500))
            page.Contents = pikepdf.Stream(pdf, b"1 0 0 rg 10 20 40 70 re f")
            pdf.save(path)
        return path

    def provider_result(self, width, height, box):
        result = response("MARK")
        result["items"]["pages"][0].update(
            page_width=width,
            page_height=height,
            items=[
                {
                    "type": "text",
                    "value": "MARK",
                    "bbox": [dict(zip(("x", "y", "w", "h"), box))],
                },
            ],
        )
        return result

    def visible_geometry(self, path, name, *, page_number=1):
        target = self.dirs.scratch_dir / name
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
                "-r",
                "72",
                "-png",
                str(path),
                str(target),
            ],
            check=True,
            capture_output=True,
        )
        with Image.open(target.with_suffix(".png")).convert("RGB") as image:
            mask = image.point(lambda x: x)
            pixels = mask.load()
            xs = []
            ys = []
            for y in range(image.height):
                for x in range(image.width):
                    r, g, b = pixels[x, y]
                    if r > 220 and g < 30 and (b < 30):
                        xs.append(x)
                        ys.append(y)
            return (
                image.size,
                (min(xs), min(ys), max(xs) + 1 - min(xs), max(ys) + 1 - min(ys)),
            )

    def assert_documented_mapping(self, document, *, page_number=1):
        page = stored_pages(document)[page_number - 1]
        layout = page["layout"]
        box = layout["items"][0]["bbox"][0]
        w, h = (layout["page_width"], layout["page_height"])
        x, y, bw, bh = (box[k] for k in ("x", "y", "w", "h"))
        basis = page["provenance"].get("layout_rotation", 0)
        delta = (page["rotation"] - basis) % 360
        if delta == 90:
            x, y, bw, bh, w, h = (h - y - bh, x, bh, bw, h, w)
        elif delta == 180:
            x, y = (w - x - bw, h - y - bh)
        elif delta == 270:
            x, y, bw, bh, w, h = (y, w - x - bw, bh, bw, h, w)
        actual_size, actual_box = self.visible_geometry(
            document.archive_path or document.source_path,
            f"actual_{page_number}",
            page_number=page_number,
        )
        self.assertEqual(
            (w, h),
            actual_size,
            "Documented orientation delta cannot map layout dimensions to retained PDF",
        )
        self.assertEqual(
            (x, y, bw, bh),
            actual_box,
            "Documented orientation delta places evidence away from printed content",
        )

    def test_known_job_before_manual_rotation_preserves_layout_basis(self):
        upload = self.printed_rectangle()
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            return_value=self.provider_result(180, 500, (10, 410, 40, 70)),
        ):
            document, _ = self.consume(upload=upload)
            thumb = self.dirs.scratch_dir / "thumb.webp"
            thumb.write_bytes(b"fixture")
            with (
                mock.patch(
                    "documents.file_updates.make_thumbnail_from_pdf",
                    return_value=thumb,
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                update_file(
                    document.pk,
                    None,
                    append=False,
                    keep_only_current_file=True,
                    operations=[{"page": 1, "rotate": 90}],
                )
            self.failure = None
            self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, 1)
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="never", OCR_ROTATE_PAGES=True)
    def test_never_fresh_auto_oriented_evidence_maps_to_unchanged_pdf(self):

        def orient(parser, source):
            target = parser._tempdir / "oriented.pdf"
            with pikepdf.open(source) as pdf:
                pdf.pages[0].rotate(90, relative=True)
                pdf.save(target)
            return target

        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_orient",
                autospec=True,
                side_effect=orient,
            ),
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                return_value=self.provider_result(500, 180, (20, 10, 70, 40)),
            ),
        ):
            document, original = self.consume(upload=self.printed_rectangle())
            self.failure = None
            self.assertEqual(self.retry(document), 1)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertIsNone(document.archive_path)
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="never", OCR_ROTATE_PAGES=True)
    def test_never_initial_success_layout_maps_to_unchanged_pdf(self):

        def orient(parser, source):
            target = parser._tempdir / "oriented.pdf"
            with pikepdf.open(source) as pdf:
                pdf.pages[0].rotate(90, relative=True)
                pdf.save(target)
            return target

        self.failure = None
        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_orient",
                autospec=True,
                side_effect=orient,
            ),
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                return_value=self.provider_result(500, 180, (20, 10, 70, 40)),
            ),
        ):
            document, original = self.consume(upload=self.printed_rectangle())
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertIsNone(document.archive_path)
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="always", OCR_ROTATE_PAGES=True)
    def test_archive_initial_success_geometry_control(self):

        def orient(parser, source):
            target = parser._tempdir / "oriented.pdf"
            with pikepdf.open(source) as pdf:
                pdf.pages[0].rotate(90, relative=True)
                pdf.save(target)
            return target

        self.failure = None
        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_orient",
                autospec=True,
                side_effect=orient,
            ),
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                return_value=self.provider_result(500, 180, (20, 10, 70, 40)),
            ),
        ):
            document, _ = self.consume(upload=self.printed_rectangle())
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_never_full_reprocess_retains_rotated_page_identity_and_policy(self):
        from documents.tasks import update_document_content_maybe_archive_file

        self.failure = None
        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            return_value=self.provider_result(180, 500, (10, 410, 40, 70)),
        ):
            document, _ = self.consume(upload=self.printed_rectangle())
        thumb = self.dirs.scratch_dir / "rotate.webp"
        thumb.write_bytes(b"fixture")
        with (
            mock.patch(
                "documents.file_updates.make_thumbnail_from_pdf",
                return_value=thumb,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1, "rotate": 90}],
            )
        document.refresh_from_db()
        before = stored_pages(document)[0]
        source = document.source_path.read_bytes()
        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                return_value=self.provider_result(500, 180, (20, 10, 70, 40)),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()
        after = stored_pages(document)[0]
        self.assertEqual(document.source_path.read_bytes(), source)
        self.assertEqual(after["origin"], before["origin"])
        self.assertEqual(after["rotation"], before["rotation"])
        self.assertEqual(
            after["provenance"].get("store_only_current_file"),
            before["provenance"].get("store_only_current_file"),
        )
        self.assert_documented_mapping(document)

    def orient_input(self, parser, source):
        target = parser._tempdir / "oriented_input.pdf"
        with pikepdf.open(source) as pdf:
            for page in pdf.pages:
                page.rotate(90, relative=True)
            pdf.save(target)
        return target

    def reprocess(self, document):
        from documents.tasks import update_document_content_maybe_archive_file

        with (
            mock.patch(
                "documents.tasks.get_parser_registry",
                return_value=self.registry.return_value,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        document.refresh_from_db()

    @override_settings(ARCHIVE_FILE_GENERATION="never", OCR_ROTATE_PAGES=True)
    def test_never_known_auto_oriented_job_maps_to_original_after_resume(self):
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_orient",
                autospec=True,
                side_effect=self.orient_input,
            ),
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                return_value=self.provider_result(500, 180, (20, 10, 70, 40)),
            ),
        ):
            document, original = self.consume(upload=self.printed_rectangle())
            self.failure = None
            self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, 1)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_never_known_job_before_manual_rotation_maps_to_current_original(self):
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            return_value=self.provider_result(180, 500, (10, 410, 40, 70)),
        ):
            document, _ = self.consume(upload=self.printed_rectangle())
            thumbnail = self.dirs.scratch_dir / "manual_rotation.webp"
            thumbnail.write_bytes(b"thumbnail")
            with (
                mock.patch(
                    "documents.file_updates.make_thumbnail_from_pdf",
                    return_value=thumbnail,
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                update_file(
                    document.pk,
                    None,
                    append=False,
                    keep_only_current_file=True,
                    operations=[{"page": 1, "rotate": 90}],
                )
            document.refresh_from_db()
            source = document.source_path.read_bytes()
            self.failure = None
            self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, 1)
        self.assertEqual(document.source_path.read_bytes(), source)
        self.assertIsNone(document.archive_path)
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="never", OCR_ROTATE_PAGES=True)
    def test_partial_never_resume_keeps_old_and_rotated_page_layouts_aligned(self):
        import copy

        source = self.dirs.consumption_dir / "two_rectangles.pdf"
        with pikepdf.new() as pdf:
            for rectangle in (b"10 20 40 70", b"40 200 30 50"):
                page = pdf.add_blank_page(page_size=(180, 500))
                page.Contents = pikepdf.Stream(pdf, b"1 0 0 rg " + rectangle + b" re f")
            pdf.save(source)
        result = self.provider_result(500, 180, (20, 10, 70, 40))
        for section in ("text", "markdown", "items"):
            second = copy.deepcopy(result[section]["pages"][0])
            second["page_number"] = 2
            if section == "items":
                second["items"][0]["bbox"] = [{"x": 200, "y": 40, "w": 50, "h": 30}]
            result[section]["pages"].append(second)
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_orient",
                autospec=True,
                side_effect=self.orient_input,
            ),
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                return_value=result,
            ),
        ):
            document, original = self.consume(upload=source)
            self.failure = None
            self.assertEqual(self.retry(document, max_pages=1), 1)
            self.assertEqual(document.source_path.read_bytes(), original)
            self.assert_documented_mapping(document)
            completed = copy.deepcopy(stored_pages(document)[0])
            thumbnail = self.dirs.scratch_dir / "second_rotated.webp"
            thumbnail.write_bytes(b"thumbnail")
            with (
                mock.patch(
                    "documents.file_updates.make_thumbnail_from_pdf",
                    return_value=thumbnail,
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                update_file(
                    document.pk,
                    None,
                    append=False,
                    keep_only_current_file=True,
                    operations=[{"page": 1}, {"page": 2, "rotate": 90}],
                )
            document.refresh_from_db()
            after_edit = document.source_path.read_bytes()
            self.assertEqual(self.retry(document), 1)
            self.assertEqual(document.source_path.read_bytes(), after_edit)
        self.assertEqual(self.create_requests, 1)
        self.assertEqual(stored_pages(document)[0]["layout"], completed["layout"])
        self.assert_documented_mapping(document, page_number=1)
        self.assert_documented_mapping(document, page_number=2)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_successful_full_reprocess_retires_prior_manual_flag_and_retry_state(self):
        from documents.page_data import has_manual_content

        self.failure = None
        result = self.provider_result(180, 500, (10, 410, 40, 70))
        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            return_value=result,
        ):
            document, _ = self.consume(upload=self.printed_rectangle())
            document.content = "Prior manual correction"
            document.save(update_fields=["content"])
            record = document.parsed_pages.get()
            record.provenance["manual_content"] = True
            record.save(update_fields=["provenance"])
            origin = record.origin
            self.reprocess(document)
        pages = stored_pages(document)
        self.assertEqual(document.content, "MARK")
        self.assertEqual(pages[0]["origin"], origin)
        self.assertFalse(pages[0]["provenance"].get("manual_content"))
        self.assertFalse(pages[0]["provenance"].get("ocr_retry", {}).get("running"))
        self.assertFalse(has_manual_content(document, pages))
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_full_reprocess_preserves_concurrent_manual_body_and_marks_it_manual(self):
        from documents.models import Document

        self.failure = None
        result = self.provider_result(180, 500, (10, 410, 40, 70))
        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.response",
            return_value=result,
        ):
            document, original = self.consume(upload=self.printed_rectangle())
            send = LlamaParseDocumentParser._send

            def edit(client, method, path, **kwargs):
                if method == "GET":
                    Document.objects.filter(pk=document.pk).update(
                        content="Edited during cloud OCR",
                    )
                return send(client, method, path, **kwargs)

            with mock.patch.object(LlamaParseDocumentParser, "_send", side_effect=edit):
                self.reprocess(document)
        self.assertEqual(document.content, "Edited during cloud OCR")
        self.assertTrue(stored_pages(document)[0]["provenance"]["manual_content"])
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assert_documented_mapping(document)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_digital_skip_reuses_absolute_layout_basis_without_adding_rotation(self):
        from django.db import transaction

        from documents.page_data import save_pages

        source = self.printed_rectangle()
        with pikepdf.open(source) as pdf:
            font = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Font,
                    Subtype=pikepdf.Name.Type1,
                    BaseFont=pikepdf.Name.Helvetica,
                ),
            )
            page = pdf.pages[0]
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            page.Contents = pikepdf.Stream(
                pdf,
                b"1 0 0 rg 10 20 40 70 re f BT /F1 4 Tf 100 100 Td (MARK) Tj ET",
            )
            visible = self.dirs.consumption_dir / "digital_rectangle.pdf"
            pdf.save(visible)
        document, _ = self.consume(upload=visible)
        page = stored_pages(document)[0]
        page["text"] = "MARK"
        page["markdown"] = "# MARK"
        page["layout"] = LlamaParseDocumentParser._validated_pages(
            self.provider_result(180, 500, (10, 410, 40, 70)),
            1,
        )[0]
        page["provenance"].update(engine="llamaparse", layout_rotation=0)
        with transaction.atomic(), self.captureOnCommitCallbacks(execute=True):
            save_pages(
                document,
                [page],
                automatic_content=document.content,
                manual_content=False,
            )
        thumbnail = self.dirs.scratch_dir / "digital_rotated.webp"
        thumbnail.write_bytes(b"thumbnail")
        with (
            mock.patch(
                "documents.file_updates.make_thumbnail_from_pdf",
                return_value=thumbnail,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            update_file(
                document.pk,
                None,
                append=False,
                keep_only_current_file=True,
                operations=[{"page": 1, "rotate": 90}],
            )
        document.refresh_from_db()
        before = copy_page = stored_pages(document)[0]
        source_bytes = document.source_path.read_bytes()
        self.reprocess(document)
        after = stored_pages(document)[0]
        self.assertEqual(after["origin"], before["origin"])
        self.assertEqual(after["layout"], copy_page["layout"])
        self.assertEqual(after["rotation"], 90)
        self.assertEqual(after["provenance"]["layout_rotation"], 0)
        self.assertEqual(document.source_path.read_bytes(), source_bytes)
        self.assertEqual(self.create_requests, 0)
        self.assert_documented_mapping(document)
