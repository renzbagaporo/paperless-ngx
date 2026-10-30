"""Initial image uploads honor Never without losing durable OCR evidence."""

import copy
from itertools import product
from unittest import mock

from django.test import override_settings
from PIL import Image

from documents.data_models import DocumentSource
from documents.ocr_queue import pending_pages
from documents.ocr_recovery import reconcile
from documents.page_data import stored_pages
from documents.tests.test_page_data import response
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from paperless.parsers.llamaparse import LlamaParseDocumentParser
from paperless.parsers.remote import RemoteDocumentParser


class InitialImageArchivePolicyTests(RemoteOCRTestCase):
    def assert_initial_image(self, index, extension, current_only, quota):
        count = 2 if extension == "tiff" else 1
        upload = self.dirs.consumption_dir / f"initial_{index}.{extension}"
        first = Image.new(
            "RGB",
            (100 + index, 160 + index),
            (30 + index * 3, 90, 210 - index * 2),
        )
        options = (
            {
                "save_all": True,
                "append_images": [
                    Image.new(
                        "RGB",
                        (110 + index, 170 + index),
                        (90, 40 + index * 2, 110),
                    ),
                ],
            }
            if count == 2
            else {}
        )
        first.save(upload, format=extension, dpi=(150, 150), **options)
        self.failure = 402 if quota else None
        self.failure_stage = "/api/v2/parse"
        labels = [
            f"Initial image {index} page {number}" for number in range(1, count + 1)
        ]

        def result(_text):
            parsed = response("template")
            for section, field in (
                ("text", "text"),
                ("markdown", "markdown"),
                ("items", None),
            ):
                template = parsed[section]["pages"][0]
                parsed[section]["pages"] = []
                for number, label in enumerate(labels, 1):
                    item = dict(copy.deepcopy(template), page_number=number)
                    if field:
                        item[field] = label
                    parsed[section]["pages"].append(item)
            return parsed

        with (
            override_settings(
                ARCHIVE_FILE_GENERATION="never",
                STORE_ONLY_CURRENT_FILE=current_only,
            ),
            mock.patch.object(
                LlamaParseDocumentParser,
                "get_thumbnail",
                new=RemoteDocumentParser.get_thumbnail,
            ),
            mock.patch(
                "documents.tests.test_remote_ocr_resilience.response",
                side_effect=result,
            ),
        ):
            document, original = self.consume(
                source=DocumentSource.ConsumeFolder
                if quota
                else DocumentSource.ApiUpload,
                upload=upload,
                asn=3100 + index,
            )
            self.assertEqual(document.source_path.read_bytes(), original)
            self.assertEqual(document.mime_type, f"image/{extension}")
            self.assertIsNone(document.archive_path)
            self.assertIsNone(document.archive_checksum)
            self.assertTrue(document.thumbnail_path.is_file())
            self.assertEqual(document.archive_serial_number, 3100 + index)
            self.assertTrue(document.tags.filter(pk=self.user_tag.pk).exists())
            pages = stored_pages(document)
            self.assertIsNotNone(pages)
            self.assertEqual(len(pages), count)
            if document.page_count is not None:
                self.assertEqual(document.page_count, count)
            if quota:
                self.assertEqual(document.content, "")
                self.assertEqual(len(pending_pages(document)), count)
                self.assertTrue(document.tags.filter(name="ocr-pending").exists())
                self.assertTrue(document.tags.filter(name="ocr-fail").exists())
            else:
                self.assertEqual(document.content, "\n\n".join(labels))
                self.assertFalse(pending_pages(document))
                self.assertTrue(document.tags.filter(name="ocr-ok").exists())
                self.assertEqual([page["text"] for page in pages], labels)
                self.assertTrue(
                    all(
                        page["markdown"] is not None and page["layout"] is not None
                        for page in pages
                    ),
                )
            self.assertEqual(self.create_requests, 1)
            requests = len(self.requests)
            with mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as dispatch:
                reconcile()
                reconcile()
                dispatch.assert_not_called()
            self.assertEqual(self.create_requests, 1)
            self.assertEqual(len(self.requests), requests)
            self.assertEqual(document.source_path.read_bytes(), original)
            self.assertIsNone(document.archive_path)


for case_index, (image_extension, keep_current, quota_failure) in enumerate(
    product(("png", "tiff"), (False, True), (False, True)),
    1,
):

    def case(
        self,
        index=case_index,
        extension=image_extension,
        current_only=keep_current,
        quota=quota_failure,
    ):
        self.assert_initial_image(index, extension, current_only, quota)

    test_name = f"test_initial_{image_extension}_never_{'current' if keep_current else 'original'}_{'quota' if quota_failure else 'healthy'}"
    setattr(InitialImageArchivePolicyTests, test_name, case)
