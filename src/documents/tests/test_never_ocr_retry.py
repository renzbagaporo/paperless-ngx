"""Pending OCR honors Never without modifying any persisted rendition."""

import copy
import io
import json
from contextlib import contextmanager
from itertools import product
from unittest import mock

import httpx
import pikepdf
from django.test import override_settings
from PIL import Image

from documents.file_updates import update_file
from documents.models import MatchingModel
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.ocr_queue import pending_pages
from documents.ocr_recovery import reconcile
from documents.page_data import public_provenance
from documents.page_data import stored_pages
from documents.tasks import update_document_in_llm_index
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_page_data import response
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase


class NeverOCRRetryTests(RemoteOCRTestCase):
    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_cleared_partial_aggregate_does_not_resurrect_completed_page_text(self):
        upload, _ = self.upload("pdf", 80)
        with self.cloud() as state:
            document, _ = self.consume(upload=upload)
            state["failure"] = None
            self.assertEqual(self.retry(document, max_pages=1), 1)
            self.assertEqual(document.content, state["labels"][0])
            document.content = ""
            document.save(update_fields=["content"])
            before = self.file_state(document)
            self.assertEqual(self.retry(document), 1)
            self.assertEqual(document.content, state["labels"][1])
            self.assertEqual(stored_pages(document)[0]["text"], state["labels"][0])
            self.assertEqual(self.file_state(document), before)

    def upload(self, extension, index=1):
        path = self.dirs.consumption_dir / f"never_retry_{index}.{extension}"
        if extension == "pdf":
            return write_pdf(path, [180 + index, 240 + index]), 2
        count = 2 if extension == "tiff" else 1
        first = Image.new("RGB", (100 + index, 160 + index), (50 + index, 70, 140))
        options = (
            {
                "save_all": True,
                "append_images": [
                    Image.new(
                        "RGB",
                        (120 + index, 180 + index),
                        (80, 100 + index, 200),
                    ),
                ],
            }
            if count == 2
            else {}
        )
        first.save(path, format=extension, dpi=(150, 150), **options)
        return path, count

    @contextmanager
    def cloud(self, failure="quota"):
        state = {
            "failure": failure,
            "files": {},
            "jobs": {},
            "creates": [],
            "labels": None,
            "polls": [],
        }

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.url.path == "/api/v1/beta/files":
                body = request.content
                content = body[body.index(b"%PDF-") : body.rindex(b"%%EOF") + 5]
                with pikepdf.open(io.BytesIO(content)) as pdf:
                    labels = [
                        f"Synthetic width {float(page.mediabox[2]):.2f}"
                        for page in pdf.pages
                    ]
                file_id = f"file-{len(state['files']) + 1}"
                state["files"][file_id] = labels
                if state["labels"] is None:
                    state["labels"] = labels
                return httpx.Response(200, json={"id": file_id})
            if request.url.path == "/api/v2/parse":
                state["creates"].append(request)
                if state["failure"] == "quota":
                    return httpx.Response(402)
                job_id = f"paid-{len(state['creates'])}"
                state["jobs"][job_id] = state["files"][
                    json.loads(request.content)["file_id"]
                ]
                return httpx.Response(200, json={"id": job_id})
            state["polls"].append(request)
            if state["failure"] == "poll":
                return httpx.Response(503)
            job_id = request.url.path.rsplit("/", 1)[-1]
            labels = state["jobs"][job_id]
            result = response("template")
            result["job"]["id"] = job_id
            for section, field in (
                ("text", "text"),
                ("markdown", "markdown"),
                ("items", None),
            ):
                template = result[section]["pages"][0]
                result[section]["pages"] = []
                for number, label in enumerate(labels, 1):
                    page = dict(copy.deepcopy(template), page_number=number)
                    if field:
                        page[field] = label
                    result[section]["pages"].append(page)
            return httpx.Response(200, json=result)

        def client(endpoint, **kwargs):
            kwargs.pop("allow_internal", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch(
            "paperless.parsers.llamaparse.create_pinned_httpx_client",
            side_effect=client,
        ):
            yield state

    def file_state(self, document):
        document.refresh_from_db()
        return {
            "filename": document.filename,
            "checksum": document.checksum,
            "mime_type": document.mime_type,
            "archive_filename": document.archive_filename,
            "archive_checksum": document.archive_checksum,
            "original_filename": document.original_filename,
            "source": document.source_path.read_bytes(),
            "archive": document.archive_path.read_bytes()
            if document.archive_path
            else None,
            "thumbnail": document.thumbnail_path.read_bytes(),
        }

    def run_case(self, index, extension, current_only, failure):
        upload, count = self.upload(extension, index)
        with (
            override_settings(
                ARCHIVE_FILE_GENERATION="never",
                STORE_ONLY_CURRENT_FILE=current_only,
            ),
            self.cloud(failure) as state,
        ):
            document, _ = self.consume(upload=upload, asn=4200 + index)
            before = self.file_state(document)
            self.assertEqual(len(pending_pages(document)), count)
            creates = len(state["creates"])
            state["failure"] = None
            self.assertEqual(self.retry(document), count)
            self.assertEqual(self.file_state(document), before)
            self.assertEqual(document.archive_serial_number, 4200 + index)
            self.assertEqual(document.content, "\n\n".join(state["labels"]))
            self.assertFalse(pending_pages(document))
            self.assertEqual(len(stored_pages(document)), count)
            self.assertFalse(
                any(
                    page["provenance"].get("ocr_job") for page in stored_pages(document)
                ),
            )
            self.assertEqual(
                len(state["creates"]),
                creates if failure == "poll" else creates + count,
            )
            creates = len(state["creates"])
            polls = len(state["polls"])
            reconcile()
            reconcile()
            self.assertEqual(len(state["creates"]), creates)
            self.assertEqual(len(state["polls"]), polls)

    def test_never_keeps_an_existing_archive_in_both_storage_modes(self):
        for current_only in (False, True):
            with self.subTest(current_only=current_only), self.cloud() as state:
                upload, count = self.upload("tiff", 40 + current_only)
                with override_settings(
                    ARCHIVE_FILE_GENERATION="always",
                    STORE_ONLY_CURRENT_FILE=False,
                ):
                    document, _ = self.consume(upload=upload, asn=4400 + current_only)
                self.assertIsNotNone(document.archive_path)
                before = self.file_state(document)
                state["failure"] = None
                with override_settings(
                    ARCHIVE_FILE_GENERATION="never",
                    STORE_ONLY_CURRENT_FILE=current_only,
                ):
                    self.assertEqual(self.retry(document), count)
                self.assertEqual(self.file_state(document), before)
                self.assertEqual(document.content, "\n\n".join(state["labels"]))

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_partial_retry_preserves_manual_content_rotation_and_completed_page(self):
        upload, _ = self.upload("pdf", 50)
        with self.cloud() as state:
            document, _ = self.consume(upload=upload)
            state["failure"] = None
            self.assertEqual(self.retry(document, max_pages=1), 1)
            document.content = "Human correction"
            document.save(update_fields=["content"])
            thumbnail = self.dirs.scratch_dir / "rotated.webp"
            thumbnail.write_bytes(b"rotated thumbnail")
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
            before = self.file_state(document)
            completed = copy.deepcopy(stored_pages(document)[0])
            creates = len(state["creates"])
            self.assertEqual(self.retry(document), 1)
            self.assertEqual(len(state["creates"]), creates + 1)
            self.assertEqual(self.file_state(document), before)
            self.assertEqual(
                document.content,
                "Human correction\n\n" + state["labels"][1],
            )
            after = stored_pages(document)
            for field in (
                "origin",
                "source_page",
                "rotation",
                "text",
                "markdown",
                "layout",
            ):
                self.assertEqual(after[0][field], completed[field])
            self.assertEqual(
                public_provenance(after[0]["provenance"]),
                public_provenance(completed["provenance"]),
            )
            self.assertEqual(after[1]["rotation"], 90)

    @override_settings(ARCHIVE_FILE_GENERATION="never")
    def test_text_checkpoint_rollback_preserves_files_and_completed_paid_job(self):
        upload, _ = self.upload("png", 60)
        with self.cloud() as state:
            document, _ = self.consume(upload=upload)
            before = self.file_state(document)
            state["failure"] = None
            with (
                mock.patch(
                    "documents.ocr_queue.save_pages",
                    side_effect=RuntimeError("checkpoint failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "checkpoint failed"),
            ):
                self.retry(document)
            document.refresh_from_db()
            self.assertEqual(document.content, "")
            self.assertEqual(self.file_state(document), before)
            self.assertTrue(pending_pages(document))
            self.assertTrue(stored_pages(document)[0]["provenance"]["ocr_job"]["id"])
            creates = len(state["creates"])
            self.assertEqual(self.retry(document), 1)
            self.assertEqual(len(state["creates"]), creates)
            self.assertEqual(self.file_state(document), before)

    @override_settings(
        ARCHIVE_FILE_GENERATION="never",
        AI_ENABLED=True,
        LLM_EMBEDDING_BACKEND="huggingface",
    )
    def test_text_only_completion_runs_added_workflow_and_indexes_once(self):
        workflow = Workflow.objects.create(name="After complete OCR")
        workflow.triggers.add(
            WorkflowTrigger.objects.create(
                type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
                matching_algorithm=MatchingModel.MATCH_NONE,
            ),
        )
        workflow.actions.add(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                assign_title="Complete collection",
            ),
        )
        upload, _ = self.upload("tiff", 70)
        with (
            self.cloud() as state,
            mock.patch(
                "documents.tasks.update_document_in_llm_index.apply_async",
            ) as enqueue,
        ):
            document, original = self.consume(upload=upload)
            state["failure"] = None
            self.assertEqual(self.retry(document, max_pages=1), 1)
            self.assertFalse(
                WorkflowRun.objects.filter(
                    workflow=workflow,
                    document=document,
                ).exists(),
            )
            enqueue.assert_not_called()
            self.assertEqual(self.retry(document), 1)
            self.assertEqual(
                WorkflowRun.objects.filter(
                    workflow=workflow,
                    document=document,
                ).count(),
                1,
            )
            self.assertEqual(document.title, "Complete collection")
            self.assertEqual(document.source_path.read_bytes(), original)
            self.assertIsNone(document.archive_path)
            enqueue.assert_called_once()
            with mock.patch(
                "documents.tasks.llm_index_add_or_update_document",
                return_value=True,
            ):
                update_document_in_llm_index.run(**enqueue.call_args.kwargs["kwargs"])
            creates, polls = len(state["creates"]), len(state["polls"])
            reconcile()
            reconcile()
            enqueue.assert_called_once()
            self.assertEqual(
                (len(state["creates"]), len(state["polls"])),
                (creates, polls),
            )


for case_index, (image_extension, keep_current, first_failure) in enumerate(
    product(("png", "tiff", "pdf"), (False, True), ("quota", "poll")),
    1,
):

    def case(
        self,
        index=case_index,
        extension=image_extension,
        current_only=keep_current,
        failure=first_failure,
    ):
        self.run_case(index, extension, current_only, failure)

    setattr(
        NeverOCRRetryTests,
        f"test_never_retry_{image_extension}_{keep_current}_{first_failure}",
        case,
    )
