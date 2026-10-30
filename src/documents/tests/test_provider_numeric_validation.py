"""Unusable provider numbers are rejected before rich JSON reaches PostgreSQL."""

import json
from decimal import Decimal
from unittest import mock

import httpx
from django.test import override_settings
from PIL import Image

from documents.ocr_queue import pending_pages
from documents.page_data import stored_pages
from documents.tests.test_page_data import response
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase


@override_settings(ARCHIVE_FILE_GENERATION="never", STORE_ONLY_CURRENT_FILE=False)
class ProviderNumericValidationTests(RemoteOCRTestCase):
    def consume_response(self, result, index, *, token=None):
        upload = self.dirs.consumption_dir / f"provider_numeric_{index}.png"
        Image.new("RGB", (130 + index, 190 + index), (30 + index, 90, 180)).save(upload)
        wire = json.dumps(result)
        if token is not None:
            wire = wire.replace('"__NUMBER__"', token)

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.url.path == "/api/v1/beta/files":
                return httpx.Response(200, json={"id": "source"})
            if request.url.path == "/api/v2/parse":
                return httpx.Response(200, json={"id": "known-paid-job"})
            return httpx.Response(
                200,
                content=wire.encode("ascii"),
                headers={"Content-Type": "application/json"},
            )

        def client(endpoint, **kwargs):
            kwargs.pop("allow_internal", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch(
            "paperless.parsers.llamaparse.create_pinned_httpx_client",
            side_effect=client,
        ):
            return self.consume(upload=upload, asn=4500 + index)

    def assert_pending_original(self, document, original):
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(document.mime_type, "image/png")
        self.assertIsNone(document.archive_path)
        self.assertEqual(document.content, "")
        self.assertEqual(len(pending_pages(document)), 1)
        page = stored_pages(document)[0]
        self.assertIsNone(page["layout"])
        self.assertIsNone(page["markdown"])
        self.assertEqual(page["provenance"]["ocr_job"]["id"], "known-paid-job")
        self.assertEqual(page["provenance"]["ocr_job"]["state"], "terminal")

    def test_nonfinite_nested_bbox_table_and_dimensions_preserve_valid_upload(self):
        index = 0
        for location in ("bbox", "table", "dimension"):
            for token in ("NaN", "1e999", "-1e999"):
                with self.subTest(location=location, wire_number=token):
                    index += 1
                    result = response("Valid cloud text")
                    page = result["items"]["pages"][0]
                    if location == "bbox":
                        page["items"] = [
                            {
                                "type": "text",
                                "value": "Valid cloud text",
                                "bbox": [{"x": "__NUMBER__", "y": 1, "w": 20, "h": 10}],
                            },
                        ]
                    elif location == "table":
                        page["items"] = [
                            {"type": "table", "rows": [["Valid cell", "__NUMBER__"]]},
                        ]
                    else:
                        page["page_width"] = "__NUMBER__"
                    document, original = self.consume_response(
                        result,
                        index,
                        token=token,
                    )
                    self.assert_pending_original(document, original)

    def test_malformed_page_dimensions_preserve_valid_upload(self):
        for index, value in enumerate(
            (True, None, "not a width", 0, -1, {}, 10**400),
            20,
        ):
            with self.subTest(width=value):
                result = response("Valid cloud text")
                result["items"]["pages"][0]["page_width"] = value
                document, original = self.consume_response(result, index)
                self.assert_pending_original(document, original)

    def test_finite_nested_numbers_and_nan_text_remain_valid_evidence(self):
        result = response("Valid finite evidence")
        result["items"]["pages"][0]["items"] = [
            {
                "type": "table",
                "rows": [[-2.5, "NaN", 1e200]],
                "bbox": [{"x": -1.0, "y": 1.0, "w": 20.0, "h": 10.0}],
            },
        ]
        document, original = self.consume_response(result, 30)
        self.assertEqual(document.source_path.read_bytes(), original)
        self.assertEqual(document.content, "Valid finite evidence")
        self.assertFalse(pending_pages(document))
        values = stored_pages(document)[0]["layout"]["items"][0]["rows"][0]
        self.assertEqual(values[:2], [-2.5, "NaN"])
        self.assertEqual(Decimal(str(values[2])), Decimal("1e200"))
