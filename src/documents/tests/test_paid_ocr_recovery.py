"""Known paid jobs survive transient failures and never auto-create replacements."""

import copy
import json
import subprocess
from datetime import timedelta
from unittest import mock

import httpx
import pikepdf
from django.utils import timezone

from documents.file_updates import update_file
from documents.models import Document
from documents.ocr_recovery import RETRY
from documents.ocr_recovery import reconcile
from documents.ocr_recovery import store_requests
from documents.page_data import public_provenance
from documents.page_data import stored_pages
from documents.page_splitting import split_pages
from documents.tasks import update_document_content_maybe_archive_file
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_page_data import response
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from paperless.parsers.llamaparse import LlamaParseDocumentParser


class WorkerLost(BaseException):
    pass


class PaidOCRRecoveryTests(RemoteOCRTestCase):
    def test_invalid_creation_identifiers_keep_upload_pending_without_poisoning_json(
        self,
    ):
        for index, identifier in enumerate(
            (
                "job\0invalid",
                "",
                None,
                {"id": "nested"},
                "x" * 1025,
                "job\ud800invalid",
            ),
        ):
            with self.subTest(identifier_type=type(identifier).__name__):
                polls = []

                def handler(request):
                    if request.method == "DELETE":
                        return httpx.Response(204)
                    if request.url.path == "/api/v1/beta/files":
                        return httpx.Response(200, json={"id": "source"})
                    if request.url.path == "/api/v2/parse":
                        return httpx.Response(
                            200,
                            content=json.dumps({"id": identifier}).encode("ascii"),
                            headers={"Content-Type": "application/json"},
                        )
                    polls.append(request)
                    return httpx.Response(500)

                def client(endpoint, **kwargs):
                    kwargs.pop("allow_internal", None)
                    return httpx.Client(
                        transport=httpx.MockTransport(handler),
                        **kwargs,
                    )

                with mock.patch(
                    "paperless.parsers.llamaparse.create_pinned_httpx_client",
                    side_effect=client,
                ):
                    document, original = self.consume(asn=1700 + index)
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertFalse(polls)
                job = stored_pages(document)[0]["provenance"]["ocr_job"]
                self.assertEqual(job["state"], "submitting")
                self.assertNotIn("id", job)

    def test_invalid_unicode_in_provider_text_markdown_or_keys_is_deferred(self):
        for index, location in enumerate(("text", "markdown", "key")):
            with self.subTest(location=location):
                result = response("safe text")
                if location == "key":
                    result["items"]["pages"][0]["invalid\ud800key"] = "safe value"
                else:
                    result[location]["pages"][0][location] = (
                        "Invalid\ud800provider text"
                    )

                def handler(request):
                    if request.method == "DELETE":
                        return httpx.Response(204)
                    if request.url.path == "/api/v1/beta/files":
                        return httpx.Response(200, json={"id": "source"})
                    if request.url.path == "/api/v2/parse":
                        return httpx.Response(200, json={"id": "valid-job"})
                    return httpx.Response(
                        200,
                        content=json.dumps(result).encode("ascii"),
                        headers={"Content-Type": "application/json"},
                    )

                def client(endpoint, **kwargs):
                    kwargs.pop("allow_internal", None)
                    return httpx.Client(
                        transport=httpx.MockTransport(handler),
                        **kwargs,
                    )

                with mock.patch(
                    "paperless.parsers.llamaparse.create_pinned_httpx_client",
                    side_effect=client,
                ):
                    document, original = self.consume(asn=1800 + index)
                self.assertEqual(document.source_path.read_bytes(), original)
                self.assertEqual(document.content, "")
                job = stored_pages(document)[0]["provenance"]["ocr_job"]
                self.assertEqual(job["id"], "valid-job")
                self.assertEqual(job["state"], "terminal")

    def expire(self, document):
        document.refresh_from_db()
        pages = stored_pages(document)
        for page in pages:
            state = page["provenance"].get(RETRY)
            if state:
                state["not_before"] = (
                    timezone.now() - timedelta(seconds=1)
                ).isoformat()
                state["sent_at"] = None
        store_requests(document, pages)

    def test_initial_poll_failure_retains_job_and_explicit_retry_only_polls(self):
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        document, original = self.consume()
        job = stored_pages(document)[0]["provenance"]["ocr_job"]
        self.assertEqual(job["id"], "isolated-job")
        self.assertNotIn(
            "ocr_job",
            public_provenance(stored_pages(document)[0]["provenance"]),
        )
        self.assertEqual(document.source_path.read_bytes(), original)
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, 1)
        self.assertNotIn("ocr_job", stored_pages(document)[0]["provenance"])

    def test_worker_loss_after_job_creation_resumes_without_another_charge(self):
        document, _ = self.consume()
        original_send = LlamaParseDocumentParser._send

        def lose_poll(client, method, path, **kwargs):
            if method == "GET":
                self.assertEqual(
                    stored_pages(document)[0]["provenance"]["ocr_job"]["id"],
                    "isolated-job",
                )
                raise WorkerLost
            return original_send(client, method, path, **kwargs)

        self.failure = None
        with (
            mock.patch.object(LlamaParseDocumentParser, "_send", side_effect=lose_poll),
            mock.patch("documents.ocr_recovery.finish"),
            self.assertRaises(WorkerLost),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as dispatch:
            reconcile()
            self.expire(document)
            reconcile()
            with self.captureOnCommitCallbacks(execute=True):
                update_document_content_maybe_archive_file.run(
                    **dispatch.call_args.kwargs,
                )
        document.refresh_from_db()
        self.assertEqual(document.content, self.response_text)
        self.assertEqual(
            self.create_requests,
            2,
        )  # Initial quota rejection and one paid retry.

    def test_ambiguous_job_creation_crash_requires_a_new_explicit_request(self):
        document, _ = self.consume()
        original_send = LlamaParseDocumentParser._send

        def lose_creation(client, method, path, **kwargs):
            if path == "/api/v2/parse":
                raise WorkerLost
            return original_send(client, method, path, **kwargs)

        self.failure = None
        with (
            mock.patch.object(
                LlamaParseDocumentParser,
                "_send",
                side_effect=lose_creation,
            ),
            mock.patch("documents.ocr_recovery.finish"),
            self.assertRaises(WorkerLost),
        ):
            update_document_content_maybe_archive_file(document.pk, remote_ocr=True)
        with mock.patch(
            "documents.tasks.update_document_content_maybe_archive_file.delay",
        ) as dispatch:
            reconcile()
            self.expire(document)
            reconcile()
            dispatch.assert_not_called()
        state = stored_pages(document)[0]["provenance"][RETRY]
        self.assertFalse(state["requested"])
        self.assertTrue(
            document.notes.filter(note__contains="paid job may already exist").exists(),
        )
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, 2)

    def test_expired_known_job_is_not_automatically_replaced(self):
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        document, _ = self.consume()
        self.failure = 404
        self.assertEqual(self.retry(document), 0)
        self.assertEqual(self.create_requests, 1)
        self.assertEqual(
            stored_pages(document)[0]["provenance"]["ocr_job"]["state"],
            "terminal",
        )
        self.failure = None
        self.assertEqual(self.retry(document), 1)
        self.assertEqual(self.create_requests, 2)

    def test_changed_page_identity_is_not_sent_to_the_saved_job(self):
        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        document, _ = self.consume()
        record = document.parsed_pages.get()
        record.provenance["ocr_job"]["identity"] = "a different physical page"
        record.save(update_fields=["provenance"])
        self.failure = None
        requests = len(self.requests)
        self.assertEqual(self.retry(document), 0)
        self.assertEqual(len(self.requests), requests)
        self.assertEqual(self.create_requests, 1)
        self.assertEqual(
            stored_pages(document)[0]["provenance"]["ocr_job"]["state"],
            "terminal",
        )

    def test_resumed_auto_oriented_job_preserves_a_later_manual_rotation(self):
        upload = self.dirs.consumption_dir / "oriented.pdf"
        with pikepdf.new() as pdf:
            page = pdf.add_blank_page(page_size=(180, 500))
            page.Contents = pikepdf.Stream(pdf, b"1 0 0 rg 10 20 40 70 re f")
            pdf.save(upload)
        expected = self.dirs.scratch_dir / "expected.pdf"
        with pikepdf.open(upload) as pdf:
            pdf.pages[0].rotate(180, relative=True)
            pdf.save(expected)

        def orient(parser, source):
            target = parser._tempdir / "synthetic_orientation.pdf"
            with pikepdf.open(source) as pdf:
                pdf.pages[0].rotate(90, relative=True)
                pdf.save(target)
            return target

        self.failure = 503
        self.failure_stage = "/api/v2/parse/isolated-job"
        with mock.patch.object(
            LlamaParseDocumentParser,
            "_orient",
            autospec=True,
            side_effect=orient,
        ):
            document, _ = self.consume(upload=upload)
        thumbnail = self.dirs.scratch_dir / "thumbnail.webp"
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
                operations=[{"page": 1, "rotate": 180}],
            )
        self.failure = None
        document.refresh_from_db()
        self.assertEqual(self.retry(document), 1)

        def rendered(path, name):
            output = self.dirs.scratch_dir / name
            subprocess.run(
                ["pdftoppm", "-singlefile", "-r", "72", "-png", str(path), str(output)],
                check=True,
                capture_output=True,
            )
            from PIL import Image

            with Image.open(output.with_suffix(".png")) as image:
                return image.size, image.tobytes()

        self.assertEqual(
            rendered(document.source_path, "actual"),
            rendered(expected, "expected"),
        )
        self.assertEqual(self.create_requests, 1)

    def test_multipage_job_survives_reorder_split_and_manual_rotation(self):
        creates = []
        poll_failure = [True]
        result = response("placeholder")
        for key, field in (("text", "text"), ("markdown", "markdown"), ("items", None)):
            template = result[key]["pages"][0]
            result[key]["pages"] = []
            for number in range(1, 4):
                item = copy.deepcopy(template)
                item["page_number"] = number
                if field:
                    item[field] = f"Cloud page {number}"
                result[key]["pages"].append(item)

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.url.path == "/api/v1/beta/files":
                return httpx.Response(200, json={"id": "source"})
            if request.url.path == "/api/v2/parse":
                creates.append(request)
                return httpx.Response(200, json={"id": "multipage"})
            return httpx.Response(503 if poll_failure[0] else 200, json=result)

        def client(endpoint, **kwargs):
            kwargs.pop("allow_internal", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch(
            "paperless.parsers.llamaparse.create_pinned_httpx_client",
            side_effect=client,
        ):
            document, _ = self.consume(
                upload=write_pdf(
                    self.dirs.consumption_dir / "multi.pdf",
                    [180, 240, 300],
                ),
            )
            thumbnail = self.dirs.scratch_dir / "thumb.webp"
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
                    operations=[{"page": 3, "rotate": 90}, {"page": 1}, {"page": 3}],
                )
                extracted = split_pages(document.pk, [{"page": 1}])
            target = Document.objects.get(pk=extracted["new_document_id"])
            poll_failure[0] = False
            self.assertEqual(self.retry(target), 1)
            self.assertEqual(target.content, "Cloud page 3")
            with pikepdf.open(target.source_path) as pdf:
                self.assertEqual(pdf.pages[0].get("/Rotate"), 90)
            document.refresh_from_db()
            self.assertEqual(self.retry(document), 3)
            self.assertEqual(
                document.content,
                "Cloud page 3\n\nCloud page 1\n\nCloud page 3",
            )
        self.assertEqual(len(creates), 1)
