"""Publish a consumer's durable index work after its current file is ready."""

from contextlib import contextmanager
from unittest import mock

from django.db import transaction
from django.test import TransactionTestCase
from django.test import override_settings
from PIL import Image

from documents.data_models import DocumentMetadataOverrides
from documents.file_updates import compact_imported_document
from documents.models import Document
from documents.ocr_index import INGESTION
from documents.ocr_index import PENDING
from documents.ocr_recovery import reconcile
from documents.page_data import public_provenance
from documents.page_data import stored_pages
from documents.page_splitting import split_pages
from documents.tasks import update_document_in_llm_index
from documents.tests.test_file_updates import write_pdf
from documents.tests.test_remote_ocr_resilience import RemoteOCRFixture


class OwnerStopped(BaseException):
    """A consumer stops after its document commit and before final publication."""


@override_settings(
    REMOTE_OCR_ENGINE="llamaparse",
    REMOTE_OCR_API_KEY="isolated-provider-key",
    REMOTE_OCR_MODE="always",
    STORE_ONLY_CURRENT_FILE=True,
    OCR_ROTATE_PAGES=False,
    AI_ENABLED=True,
    LLM_EMBEDDING_BACKEND="huggingface",
)
class IngestionIndexPublicationTests(RemoteOCRFixture, TransactionTestCase):
    @contextmanager
    def captureOnCommitCallbacks(self, *, execute=False):
        # TransactionTestCase performs the actual commits, including when a
        # test supplies one surrounding ingestion transaction.
        yield []

    def image(self, name="initial_scan.png"):
        path = self.dirs.consumption_dir / name
        Image.new("RGB", (180, 180), "white").save(path, dpi=(72, 72))
        return path

    def test_initial_image_publishes_one_final_file_delivery(self):
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queue:
            document, _ = self.consume(upload=self.image())
        queue.assert_called_once()
        self.assertTrue(document.filename.endswith(".pdf"))
        self.assertIsNone(document.archive_path)
        self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])
        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            return_value=True,
        ) as indexer:
            update_document_in_llm_index.run(**queue.call_args.kwargs["kwargs"])
        indexer.assert_called_once()
        self.assertEqual(self.create_requests, 1)

    def test_immediate_worker_and_cron_cannot_index_during_compaction(self):
        self.failure = None
        indexed_names = []

        def index(root):
            self.assertTrue(root.filename.endswith(".pdf"))
            self.assertIsNone(root.archive_path)
            indexed_names.append(root.filename)
            return True

        def worker(*args, **kwargs):
            update_document_in_llm_index.run(**kwargs["kwargs"])

        def compact(document):
            self.assertEqual(indexed_names, [])
            self.assertIn(INGESTION, stored_pages(document)[0]["provenance"])
            reconcile()
            self.assertEqual(indexed_names, [])
            compact_imported_document(document)
            self.assertEqual(indexed_names, [])

        with (
            mock.patch(
                "documents.tasks.llm_index_add_or_update_document",
                side_effect=index,
            ),
            mock.patch(
                "documents.tasks.update_document_in_llm_index.apply_async",
                side_effect=worker,
            ) as queue,
            mock.patch(
                "documents.file_updates.compact_imported_document",
                side_effect=compact,
            ),
        ):
            self.consume(upload=self.image())
            reconcile()
        queue.assert_called_once()
        self.assertEqual(len(indexed_names), 1)

    def test_active_ingestion_marker_is_private_and_cron_waits_for_owner(self):
        self.failure = None

        def post_consume(document):
            provenance = stored_pages(document)[0]["provenance"]
            self.assertIn(INGESTION, provenance)
            self.assertNotIn(INGESTION, public_provenance(provenance))
            reconcile()
            queue.assert_not_called()

        with (
            mock.patch(
                "documents.tasks.update_document_in_llm_index.apply_async",
            ) as queue,
            mock.patch(
                "documents.consumer.ConsumerPlugin.run_post_consume_script",
                side_effect=post_consume,
            ),
        ):
            document, _ = self.consume(upload=self.image())
        queue.assert_called_once()
        self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])

    def test_abandoned_owner_leaves_committed_work_for_reconciliation(self):
        self.failure = None
        with (
            mock.patch(
                "documents.tasks.update_document_in_llm_index.apply_async",
            ) as queue,
            mock.patch(
                "documents.consumer.ConsumerPlugin.run_post_consume_script",
                side_effect=OwnerStopped,
            ),
            self.assertRaises(OwnerStopped),
        ):
            self.consume(upload=self.image())
        queue.assert_not_called()
        document = Document.objects.get(archive_serial_number=1201)
        self.assertTrue(document.source_path.exists())
        self.assertTrue(document.archive_path.exists())
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as recovery:
            reconcile()
        recovery.assert_called_once()
        self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])
        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            return_value=True,
        ) as indexer:
            update_document_in_llm_index.run(**recovery.call_args.kwargs["kwargs"])
        indexer.assert_called_once()
        self.assertNotIn(PENDING, stored_pages(document)[0]["provenance"])

    def test_broker_failure_after_finalization_retains_durable_work(self):
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
            side_effect=ConnectionError,
        ) as queue:
            document, _ = self.consume(upload=self.image())
        queue.assert_called_once()
        self.assertTrue(document.filename.endswith(".pdf"))
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as recovery:
            reconcile()
        recovery.assert_called_once()
        with mock.patch(
            "documents.tasks.llm_index_add_or_update_document",
            return_value=True,
        ):
            update_document_in_llm_index.run(**recovery.call_args.kwargs["kwargs"])
        self.assertNotIn(PENDING, stored_pages(document)[0]["provenance"])

    def test_outer_transaction_publishes_only_after_its_real_commit(self):
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queue:
            with transaction.atomic():
                document, _ = self.consume(upload=self.image())
                queue.assert_not_called()
            queue.assert_called_once()
        document.refresh_from_db()
        self.assertTrue(document.filename.endswith(".pdf"))
        self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])

    def test_pdf_and_born_digital_paths_publish_once(self):
        self.failure = None
        for index, text in enumerate(
            (None, "Born digital content without remote OCR " * 20),
        ):
            with self.subTest(digital=text is not None):
                before = self.create_requests
                with mock.patch(
                    "documents.tasks.update_document_in_llm_index.apply_async",
                ) as queue:
                    document, _ = self.consume(asn=1301 + index, text=text)
                queue.assert_called_once()
                self.assertIsNone(document.archive_path)
                self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])
                self.assertEqual(self.create_requests - before, int(text is None))

    def test_quota_failure_remains_pending_until_explicit_ocr_completion(self):
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queue:
            document, _ = self.consume(upload=self.image())
        queue.assert_not_called()
        self.assertTrue(stored_pages(document)[0]["provenance"][PENDING])
        self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])
        self.assertTrue(document.tags.filter(name="ocr-fail").exists())
        self.failure = None
        with mock.patch(
            "documents.tasks.update_document_in_llm_index.apply_async",
        ) as queue:
            self.assertEqual(self.retry(document), 1)
        queue.assert_called_once()

    def test_split_copy_does_not_wait_for_an_unrelated_root_owner(self):
        self.failure = None
        copied = []
        thumbnail = self.dirs.scratch_dir / "copied.webp"
        thumbnail.write_bytes(b"copy thumbnail")

        def post_consume(document):
            result = split_pages(document.pk, [{"page": 1}], title="Independent copy")
            copied.append(result["new_document_id"])
            queue.assert_called_once()
            self.assertEqual(queue.call_args.kwargs["kwargs"]["document"].pk, copied[0])

        with (
            mock.patch(
                "documents.tasks.update_document_in_llm_index.apply_async",
            ) as queue,
            mock.patch(
                "documents.consumer.ConsumerPlugin.run_post_consume_script",
                side_effect=post_consume,
            ),
            mock.patch(
                "documents.page_splitting.make_thumbnail_from_pdf",
                return_value=thumbnail,
            ),
        ):
            document, _ = self.consume(upload=self.image())
        self.assertEqual(queue.call_count, 2)
        target = Document.objects.get(pk=copied[0])
        self.assertNotIn(INGESTION, stored_pages(target)[0]["provenance"])
        self.assertNotIn(INGESTION, stored_pages(document)[0]["provenance"])
        self.assertEqual(self.create_requests, 1)

    def test_finishing_owner_publishes_a_newer_version_that_waited_for_it(self):
        self.failure = None
        versions = []

        def post_consume(document):
            if document.root_document_id is not None:
                return
            self.response_text = "Newer notice from the concurrent version"
            upload = write_pdf(self.dirs.consumption_dir / "new_version.pdf", [240])
            with self.get_consumer(
                upload,
                overrides=DocumentMetadataOverrides(),
            ) as consumer:
                consumer.input_doc.root_document_id = document.pk
                result = consumer.run()
            versions.append(result["document_id"])
            queue.assert_not_called()

        with (
            mock.patch(
                "documents.tasks.update_document_in_llm_index.apply_async",
            ) as queue,
            mock.patch(
                "documents.consumer.ConsumerPlugin.run_post_consume_script",
                side_effect=post_consume,
            ),
            # This test isolates publication after a newer native version has
            # become current, independently of maintenance's history policy.
            mock.patch("documents.file_updates.compact_imported_document"),
        ):
            document, _ = self.consume(upload=self.image())
        queue.assert_called_once()
        self.assertEqual(document.versions.get().pk, versions[0])
        self.assertIn(
            "Newer notice",
            queue.call_args.kwargs["kwargs"]["document"].get_effective_content(),
        )
