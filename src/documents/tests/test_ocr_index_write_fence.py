"""The real indexer fences its local write after external embedding finishes."""

import json
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import connection
from django.db import connections
from django.test import TransactionTestCase
from django.test import override_settings
from django.utils import timezone
from llama_index.core.vector_stores.types import VectorStoreQuery

from documents.models import Document
from documents.ocr_index import PENDING
from documents.ocr_index import REQUEST
from documents.page_data import stored_pages
from documents.permissions import permitted_document_ids
from documents.tasks import remove_document_from_llm_index
from documents.tasks import update_document_in_llm_index
from documents.tests.test_remote_ocr_resilience import RemoteOCRFixture
from paperless_ai.indexing import MigrationCheckResult
from paperless_ai.indexing import document_id_filters
from paperless_ai.vector_store import PaperlessSqliteVecVectorStore
from paperless_testing.factories import UserFactory


@override_settings(
    REMOTE_OCR_ENGINE="llamaparse",
    REMOTE_OCR_API_KEY="isolated-provider-key",
    REMOTE_OCR_MODE="always",
    STORE_ONLY_CURRENT_FILE=True,
    OCR_ROTATE_PAGES=False,
    AI_ENABLED=True,
    LLM_EMBEDDING_BACKEND="huggingface",
)
class IndexWriteFenceTests(RemoteOCRFixture, TransactionTestCase):
    vector = [1.0, *([0.0] * 15)]

    def setUp(self):
        super().setUp()
        self.storage = self.dirs.data_dir / "write_fence_vectors"
        self.enterContext(
            override_settings(
                LLM_INDEX_DIR=self.storage,
                LLM_INDEX_LOCK=self.storage / "index.lock",
            ),
        )
        # Keep the production indexer, write_store, migrations boundary and
        # sqlite-vec upsert/query code. Only provider/model work is synthetic.
        self.enterContext(
            mock.patch(
                "paperless_ai.indexing.get_embedding_model",
                return_value=object(),
            ),
        )
        self.enterContext(
            mock.patch(
                "paperless_ai.indexing._check_and_run_migrations",
                return_value=MigrationCheckResult.CURRENT,
            ),
        )

    @contextmanager
    def captureOnCommitCallbacks(self, *, execute=False):
        self.assertTrue(connection.get_autocommit())
        yield []

    def prepare_document(self):
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queued:
            document, _ = self.consume()
        queued.assert_called_once()
        delivery = queued.call_args.kwargs["kwargs"]
        self.assertTrue(delivery["personal_index_token"])
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        return document, delivery

    def embed(self, nodes, model):
        self.assertTrue(nodes)
        for node in nodes:
            node.embedding = self.vector

    def read_nodes(self, *, filters=None):
        self.storage.mkdir(parents=True, exist_ok=True)
        with PaperlessSqliteVecVectorStore(uri=str(self.storage)) as store:
            return store.query(
                VectorStoreQuery(
                    query_embedding=self.vector,
                    similarity_top_k=5,
                    filters=filters,
                ),
            ).nodes

    def test_current_request_writes_real_vectors_and_acknowledges(self):
        document, delivery = self.prepare_document()
        with mock.patch(
            "paperless_ai.indexing._embed_nodes",
            side_effect=self.embed,
        ) as embed:
            update_document_in_llm_index.run(**delivery)
        embed.assert_called_once()
        nodes = self.read_nodes(filters=document_id_filters([str(document.pk)]))
        self.assertTrue(nodes)
        self.assertTrue(any(document.content in node.text for node in nodes))
        provenance = stored_pages(Document.objects.get(pk=document.pk))[0]["provenance"]
        self.assertNotIn(PENDING, provenance)
        self.assertNotIn(REQUEST, provenance)

    def test_metadata_change_during_embedding_prevents_stale_upsert(self):
        document, delivery = self.prepare_document()

        def edit_after_embedding(nodes, model):
            self.embed(nodes, model)
            current = Document.objects.get(pk=document.pk)
            current.content = "Deliberate content after embedding began"
            current.save(update_fields=["content", "modified"])

        with mock.patch(
            "paperless_ai.indexing._embed_nodes",
            side_effect=edit_after_embedding,
        ) as embed:
            update_document_in_llm_index.run(**delivery)
        embed.assert_called_once()
        self.assertFalse(self.read_nodes())
        self.assertTrue(
            stored_pages(Document.objects.get(pk=document.pk))[0]["provenance"][
                PENDING
            ],
        )

    def test_replaced_request_during_embedding_prevents_stale_upsert(self):
        document, delivery = self.prepare_document()
        replacement = str(uuid.uuid4())

        def replace_request(nodes, model):
            self.embed(nodes, model)
            for page in document.parsed_pages.all():
                page.provenance[REQUEST]["token"] = replacement
                page.save(update_fields=["provenance"])

        with mock.patch(
            "paperless_ai.indexing._embed_nodes",
            side_effect=replace_request,
        ) as embed:
            update_document_in_llm_index.run(**delivery)
        embed.assert_called_once()
        self.assertFalse(self.read_nodes())
        provenance = stored_pages(document)[0]["provenance"]
        self.assertEqual(provenance[REQUEST]["token"], replacement)
        self.assertTrue(provenance[PENDING])

    def replacement_export(self, document):
        target = self.dirs.scratch_dir / "replacement_export"
        target.mkdir()
        call_command(
            "document_exporter",
            target,
            data_only=True,
            skip_checks=True,
            stdout=StringIO(),
        )
        manifest = target / "manifest.json"
        records = json.loads(manifest.read_text())
        records = [
            record
            for record in records
            if not (
                record["model"] == "documents.documentpage"
                and record["fields"]["document"] == document.pk
            )
        ]
        record = next(
            record
            for record in records
            if record["model"] == "documents.document" and record["pk"] == document.pk
        )
        record["fields"].update(
            title="Replacement after native import",
            content="Different replacement content",
            added=timezone.now().isoformat(),
        )
        manifest.write_text(json.dumps(records))
        return target

    def deleted_while_embedding(self, *, trash=False, reuse=False):
        if connection.vendor != "postgresql":
            pytest.skip("Requires separate committed PostgreSQL connections")
        document, delivery = self.prepare_document()
        original_id = document.pk
        target = self.replacement_export(document) if reuse else None
        captured = threading.Event()
        release = threading.Event()
        errors = []
        pids = {}
        removals = []

        def wait_after_embedding(nodes, model):
            self.embed(nodes, model)
            self.assertTrue(any(document.content in node.text for node in nodes))
            captured.set()
            if not release.wait(30):
                raise AssertionError("Embedding barrier timed out")

        def worker():
            try:
                connections.close_all()
                with connections["default"].cursor() as cursor:
                    cursor.execute("select pg_backend_pid()")
                    pids["worker"] = cursor.fetchone()[0]
                update_document_in_llm_index.run(**delivery)
            except BaseException as error:
                errors.append(repr(error))
            finally:
                connections.close_all()

        def capture_removal(*args, **kwargs):
            # Celery serializes before Django clears the deleted instance PK.
            removals.append(deepcopy(kwargs["kwargs"]["document"]))

        with (
            mock.patch(
                "paperless_ai.indexing._embed_nodes",
                side_effect=wait_after_embedding,
            ) as embed,
            mock.patch(
                "documents.tasks.remove_document_from_llm_index.apply_async",
                side_effect=capture_removal,
            ),
        ):
            thread = threading.Thread(target=worker, name="index-before-deletion")
            thread.start()
            try:
                self.assertTrue(captured.wait(20))
                with connection.cursor() as cursor:
                    cursor.execute("select pg_backend_pid()")
                    pids["main"] = cursor.fetchone()[0]
                query = Document.objects.filter(pk=original_id)
                if trash:
                    query.delete()
                    self.assertTrue(
                        Document.deleted_objects.filter(pk=original_id).exists(),
                    )
                else:
                    query.hard_delete()
                    self.assertFalse(
                        Document.global_objects.filter(pk=original_id).exists(),
                    )
                    self.assertEqual(len(removals), 1)
                    for removed in removals:
                        remove_document_from_llm_index.run(document=removed)
                if reuse:
                    # Exercise the native data importer without changing global
                    # audit registration for other tests in this process.
                    with mock.patch(
                        "documents.management.commands.document_importer.auditlog.unregister",
                    ):
                        call_command(
                            "document_importer",
                            target,
                            data_only=True,
                            no_progress_bar=True,
                            skip_checks=True,
                            stdout=StringIO(),
                        )
                    replacement = Document.objects.get(pk=original_id)
                    self.assertEqual(
                        replacement.content,
                        "Different replacement content",
                    )
                    self.assertIn(original_id, permitted_document_ids(UserFactory()))
            finally:
                release.set()
                thread.join(25)
            self.assertFalse(thread.is_alive())
            embed.assert_called_once()
        self.assertFalse(errors, errors)
        self.assertNotEqual(pids["main"], pids["worker"])
        self.assertFalse(self.read_nodes())
        if reuse:
            self.assertFalse(
                self.read_nodes(filters=document_id_filters([str(original_id)])),
            )

    def test_hard_delete_before_vector_write_preserves_deletion(self):
        self.deleted_while_embedding()

    def test_trash_before_vector_write_prevents_stale_write(self):
        self.deleted_while_embedding(trash=True)

    def test_native_import_reusing_id_cannot_receive_old_vectors(self):
        self.deleted_while_embedding(reuse=True)
