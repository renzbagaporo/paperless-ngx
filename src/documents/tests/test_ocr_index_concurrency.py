"""Index writers serialize snapshots and acknowledgment for each root."""

import threading
from contextlib import contextmanager
from unittest import mock

import pytest
from django.db import connection
from django.db import connections
from django.test import TransactionTestCase
from django.test import override_settings

from documents.models import Document
from documents.ocr_index import PENDING
from documents.page_data import stored_pages
from documents.tasks import bulk_update_documents
from documents.tasks import update_document_in_llm_index
from documents.tests.test_remote_ocr_resilience import RemoteOCRFixture


@override_settings(
    REMOTE_OCR_ENGINE="llamaparse",
    REMOTE_OCR_API_KEY="isolated-provider-key",
    REMOTE_OCR_MODE="always",
    STORE_ONLY_CURRENT_FILE=True,
    OCR_ROTATE_PAGES=False,
    AI_ENABLED=True,
    LLM_EMBEDDING_BACKEND="huggingface",
)
class IndexWriterConcurrencyTests(RemoteOCRFixture, TransactionTestCase):
    @contextmanager
    def captureOnCommitCallbacks(self, *, execute=False):
        assert connection.get_autocommit()
        yield []

    def writer_race(self, *, bulk=False):
        if connection.vendor != "postgresql":
            pytest.skip("Requires independent committed PostgreSQL connections")
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queued:
            document, _ = self.consume()
        delivery = queued.call_args.kwargs["kwargs"]
        captured = threading.Event()
        release_a = threading.Event()
        b_done = threading.Event()
        errors = []
        writes = []
        pids = {}
        index = {}
        state_after_b = []

        def snapshot(root):
            return (root.title, root.get_effective_content())

        def indexer(root):
            value = snapshot(root)
            if threading.current_thread().name == "index-a":
                captured.set()
                if not release_a.wait(20):
                    raise AssertionError("A release barrier timed out")
            index["document"] = value
            writes.append((threading.current_thread().name, value))
            return True

        def batch_indexer(*args, **kwargs):
            for root in Document.objects.filter(pk__in=kwargs["document_ids"]):
                indexer(root)
            return "LLM index updated successfully."

        def worker_a():
            try:
                connections.close_all()
                with connections["default"].cursor() as cursor:
                    cursor.execute("select pg_backend_pid()")
                    pids["a"] = cursor.fetchone()[0]
                update_document_in_llm_index.run(**delivery)
            except BaseException as error:
                errors.append(("a", repr(error)))
            finally:
                connections.close_all()

        def worker_b():
            try:
                connections.close_all()
                with connections["default"].cursor() as cursor:
                    cursor.execute("select pg_backend_pid()")
                    pids["b"] = cursor.fetchone()[0]
                if bulk:
                    bulk_update_documents.run([document.pk])
                else:
                    update_document_in_llm_index.run(
                        document=Document.objects.get(pk=document.pk),
                    )
                state_after_b.append(
                    stored_pages(Document.objects.get(pk=document.pk))[0][
                        "provenance"
                    ].get(PENDING),
                )
            except BaseException as error:
                errors.append(("b", repr(error)))
            finally:
                b_done.set()
                connections.close_all()

        with (
            mock.patch(
                "documents.tasks.llm_index_add_or_update_document",
                side_effect=indexer,
            ),
            mock.patch("documents.tasks.update_llm_index", side_effect=batch_indexer),
        ):
            a = threading.Thread(target=worker_a, name="index-a")
            b = threading.Thread(target=worker_b, name="index-b")
            a.start()
            try:
                assert captured.wait(20), (
                    "queued worker did not capture its initial snapshot"
                )
                current = Document.objects.get(pk=document.pk)
                current.title = "New deliberate title"
                current.content = "New deliberate searchable body"
                current.save(update_fields=["title", "content", "modified"])
                b.start()
                b_finished_before_a = b_done.wait(5)
            finally:
                release_a.set()
            a.join(25)
            b.join(25)
            assert not a.is_alive() and (not b.is_alive()), "worker did not terminate"
        assert not errors, errors
        assert pids["a"] != pids["b"], pids
        final = stored_pages(Document.objects.get(pk=document.pk))[0]["provenance"]
        assert index["document"] == (
            "New deliberate title",
            "New deliberate searchable body",
        )
        assert not b_finished_before_a
        assert state_after_b == [None]
        assert final.get(PENDING) is None

    def test_legacy_manual_writer_cannot_erase_pending_then_be_overwritten(self):
        self.writer_race(bulk=False)

    def test_bulk_writer_cannot_erase_pending_then_be_overwritten(self):
        self.writer_race(bulk=True)

    def test_relation_name_edit_during_indexing_keeps_fresh_intent(self):
        from paperless_testing.factories import CorrespondentFactory

        correspondent = CorrespondentFactory(
            name="Old correspondent",
            match="",
            matching_algorithm=0,
        )
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queued:
            document, _ = self.consume()
        document.correspondent = correspondent
        document.save(update_fields=["correspondent", "modified"])
        from documents.ocr_index import queue_complete_index

        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queued:
            queue_complete_index(document.pk)
        delivery = queued.call_args.kwargs["kwargs"]
        seen = []

        def edit_name_after_snapshot(root):
            seen.append(root.correspondent.name)
            type(correspondent).objects.filter(pk=correspondent.pk).update(
                name="New correspondent",
            )
            return True

        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            side_effect=edit_name_after_snapshot,
        ):
            update_document_in_llm_index.run(**delivery)
        pending = stored_pages(Document.objects.get(pk=document.pk))[0][
            "provenance"
        ].get(PENDING)
        assert seen == ["Old correspondent"]
        assert pending
