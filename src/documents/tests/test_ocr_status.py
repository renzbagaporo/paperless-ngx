import json
from io import StringIO
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import override_settings

from documents.deferred_ingestion import metadata_fingerprint
from documents.models import Tag
from documents.models import WorkflowAction
from documents.ocr_status import configured_tags
from documents.ocr_status import status_tag
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory


class OCRStatusOwnershipTests(RemoteOCRTestCase):
    def test_same_named_private_tags_do_not_block_or_become_service_tags(self):
        alice, bob = UserFactory(), UserFactory()
        TagFactory(name="ocr-processing", owner=alice)
        TagFactory(name="ocr-processing", owner=bob)
        private_failure = TagFactory(name="ocr-fail", owner=alice)
        document, _ = self.consume()
        document.tags.add(private_failure)
        self.failure = None
        self.retry(document)
        self.assertTrue(document.tags.filter(pk=private_failure.pk).exists())
        self.assertTrue(
            document.tags.filter(name="ocr-ok", owner__isnull=True).exists(),
        )
        self.assertFalse(
            document.tags.filter(name="ocr-fail", owner__isnull=True).exists(),
        )
        self.assertIsNone(status_tag("ocr-processing").owner_id)

    def test_private_failure_tag_removal_cannot_request_paid_ocr(self):
        document, _ = self.consume()
        private = TagFactory(name="ocr-fail", owner=UserFactory())
        document.tags.add(private)
        with (
            mock.patch(
                "documents.tasks.update_document_content_maybe_archive_file.delay",
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            document.tags.remove(private)
        enqueue.assert_not_called()

    def test_explicit_legacy_tag_ids_keep_ownership_references_and_retry_behavior(self):
        owner = UserFactory()
        tags = {
            name: TagFactory(name=name, owner=owner)
            for name in (
                "ocr-fail",
                "ocr-ok",
                "ocr-processing",
                "ocr-review",
            )
        }
        action = WorkflowAction.objects.create()
        action.assign_tags.add(tags["ocr-fail"])
        mapping = {name: tag.pk for name, tag in tags.items()}
        with override_settings(OCR_STATUS_TAG_IDS=mapping):
            document, _ = self.consume()
            self.assertTrue(document.tags.filter(pk=tags["ocr-fail"].pk).exists())
            before = metadata_fingerprint(document)
            with (
                mock.patch(
                    "documents.tasks.update_document_content_maybe_archive_file.delay",
                ) as enqueue,
                self.captureOnCommitCallbacks(execute=True),
            ):
                document.tags.remove(tags["ocr-fail"])
            enqueue.assert_called_once_with(
                document_id=document.pk,
                remote_ocr=True,
                pending_only=True,
            )
            self.assertEqual(metadata_fingerprint(document), before)
            self.failure = None
            self.retry(document)
            self.assertTrue(document.tags.filter(pk=tags["ocr-ok"].pk).exists())
            self.assertFalse(document.tags.filter(pk=tags["ocr-fail"].pk).exists())
        self.assertEqual(Tag.objects.get(pk=tags["ocr-fail"].pk).owner_id, owner.pk)
        self.assertTrue(action.assign_tags.filter(pk=tags["ocr-fail"].pk).exists())

    def test_configured_ids_are_validated_and_not_silently_recreated(self):
        tag = TagFactory(name="ocr-ok", owner=UserFactory())
        for mapping in (
            {"unknown": tag.pk},
            {"ocr-ok": -1},
            {"ocr-ok": True},
            {"ocr-ok": tag.pk, "ocr-fail": tag.pk},
            {"ocr-fail": tag.pk},
            {"ocr-ok": tag.pk + 1000},
        ):
            with self.subTest(mapping=mapping):
                with override_settings(OCR_STATUS_TAG_IDS=mapping):
                    with self.assertRaises(ImproperlyConfigured):
                        configured_tags()

    def test_legacy_owner_candidate_report_does_not_mutate_tags_or_use_provider(self):
        owner = UserFactory()
        tag = TagFactory(name="ocr-fail", owner=owner)
        TagFactory(name="ocr-fail", owner=UserFactory())
        output = StringIO()
        call_command("document_ocr", legacy_helper_owner=owner.pk, stdout=output)
        self.assertEqual(json.loads(output.getvalue()), {"ocr-fail": tag.pk})
        tag.refresh_from_db()
        self.assertEqual(tag.owner_id, owner.pk)
        self.assertEqual(self.requests, [])
