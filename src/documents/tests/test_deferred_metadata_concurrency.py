"""Deferred classification and workflows cannot overwrite edits made while waiting."""

from datetime import date
from unittest import mock

from django.db.models.query import QuerySet
from django.test import override_settings

from documents import matching
from documents.deferred_ingestion import CONTEXT
from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import MatchingModel
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.page_data import stored_pages
from documents.tests.test_remote_ocr_resilience import RemoteOCRTestCase
from paperless_testing.factories import CorrespondentFactory
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import DocumentTypeFactory
from paperless_testing.factories import StoragePathFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory


class DeferredMetadataConcurrencyTests(RemoteOCRTestCase):
    def initial_custom_fields(self, values):
        self._initial_custom_fields = {field.pk: value for field, value in values}

    def consume(self, *args, **kwargs):
        from documents.data_models import DocumentMetadataOverrides

        if not getattr(self, "_initial_custom_fields", None):
            return super().consume(*args, **kwargs)

        def overrides(*args, **kwargs):
            return DocumentMetadataOverrides(
                *args,
                **{**kwargs, "custom_fields": self._initial_custom_fields},
            )

        with mock.patch(
            "documents.tests.test_remote_ocr_resilience.DocumentMetadataOverrides",
            side_effect=overrides,
        ):
            return super().consume(*args, **kwargs)

    def workflow(self, *actions):
        workflow = Workflow.objects.create(name="Deferred metadata concurrency")
        workflow.triggers.add(
            WorkflowTrigger.objects.create(
                type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
                matching_algorithm=MatchingModel.MATCH_NONE,
            ),
        )
        workflow.actions.add(*actions)
        return workflow

    def edit(self, document, field, value):
        fresh = Document.objects.get(pk=document.pk)
        setattr(fresh, field, value)
        fresh.save(update_fields=[field, "modified"])

    def test_classifier_loading_cannot_override_a_concurrent_user_choice(self):
        CorrespondentFactory(
            match="Water Supply",
            matching_algorithm=MatchingModel.MATCH_LITERAL,
        )
        deliberate = CorrespondentFactory(
            match="",
            matching_algorithm=MatchingModel.MATCH_NONE,
        )
        document, _ = self.consume()
        self.failure = None

        def load():
            self.edit(document, "correspondent", deliberate)
            return None

        with mock.patch("documents.classifier.load_classifier", side_effect=load):
            self.retry(document)
        self.assertEqual(document.correspondent_id, deliberate.pk)
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "review",
        )

    def test_all_classifier_saves_reject_user_edits_during_matching(self):
        for index, (field, factory, matcher) in enumerate(
            (
                ("correspondent", CorrespondentFactory, "match_correspondents"),
                ("document_type", DocumentTypeFactory, "match_document_types"),
                ("storage_path", StoragePathFactory, "match_storage_paths"),
            ),
        ):
            with self.subTest(field=field):
                factory(
                    match="Water Supply",
                    matching_algorithm=MatchingModel.MATCH_LITERAL,
                )
                deliberate = factory(
                    match="",
                    matching_algorithm=MatchingModel.MATCH_NONE,
                )
                self.failure = 402
                document, _ = self.consume(asn=1201 + index)
                self.failure = None
                actual = getattr(matching, matcher)

                def choose(root, classifier):
                    result = actual(root, classifier)
                    self.edit(document, field, deliberate)
                    return result

                with mock.patch(f"documents.matching.{matcher}", side_effect=choose):
                    self.retry(document)
                self.assertEqual(getattr(document, f"{field}_id"), deliberate.pk)

    def test_webhook_only_workflow_does_not_save_stale_title(self):
        workflow = self.workflow(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.WEBHOOK,
            ),
        )
        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.signals.handlers.execute_webhook_action",
            side_effect=lambda *args: self.edit(
                document,
                "title",
                "My title while webhook waits",
            ),
        ):
            self.retry(document)
        self.assertEqual(document.title, "My title while webhook waits")
        self.assertFalse(WorkflowRun.objects.filter(workflow=workflow).exists())
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "review",
        )

    def test_edit_during_webhook_prevents_later_relation_and_direct_mutations(self):
        tag = TagFactory(name="Automatic workflow tag")
        webhook = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.WEBHOOK,
            order=0,
        )
        assignment = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
            assign_title="Automatic title",
            order=1,
        )
        assignment.assign_tags.add(tag)
        self.workflow(webhook, assignment)
        document, _ = self.consume()
        self.failure = None
        with mock.patch(
            "documents.signals.handlers.execute_webhook_action",
            side_effect=lambda *args: self.edit(document, "title", "User wins"),
        ):
            self.retry(document)
        self.assertEqual(document.title, "User wins")
        self.assertFalse(document.tags.filter(pk=tag.pk).exists())

    @override_settings(OCR_VALIDATE_PRINTED_DATES=True)
    def test_own_date_and_classification_changes_allow_workflow_assignment(self):
        correspondent = CorrespondentFactory(
            match="Water Supply",
            matching_algorithm=MatchingModel.MATCH_LITERAL,
        )
        tag = TagFactory(name="Classified after date")
        assignment = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
            assign_title="Classified notices",
        )
        assignment.assign_tags.add(tag)
        workflow = self.workflow(assignment)
        document, _ = self.consume()
        self.failure = None
        self.response_text = "Water Supply Notice Issued: 2024-06-28"
        self.retry(document)
        self.assertEqual(document.created, date(2024, 6, 28))
        self.assertEqual(document.correspondent_id, correspondent.pk)
        self.assertEqual(document.title, "Classified notices")
        self.assertTrue(document.tags.filter(pk=tag.pk).exists())
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "completed",
        )

    def test_relation_edit_during_assignment_stops_later_removal(self):
        from guardian.shortcuts import assign_perm

        from documents.signals.handlers import apply_assignment_to_document

        for index, relation in enumerate(("custom_field", "tag", "permission")):
            with self.subTest(relation=relation):
                custom_field = CustomField.objects.create(
                    name=f"User annotation {index}",
                    data_type=CustomField.FieldDataType.STRING,
                )
                automatic_tag = TagFactory(name=f"Automatic {index}")
                user_tag = TagFactory(name=f"Deliberate {index}")
                user = UserFactory()
                assignment = WorkflowAction.objects.create(
                    type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                    order=0,
                )
                assignment.assign_tags.add(automatic_tag)
                removal = WorkflowAction.objects.create(
                    type=WorkflowAction.WorkflowActionType.REMOVAL,
                    remove_all_custom_fields=True,
                    remove_all_tags=True,
                    remove_all_permissions=True,
                    order=1,
                )
                workflow = self.workflow(assignment, removal)
                self.failure = 402
                document, _ = self.consume(asn=1301 + index)
                self.failure = None

                def edit_during_assignment(action, root, logging_group):
                    apply_assignment_to_document(action, root, logging_group)
                    if relation == "custom_field":
                        CustomFieldInstance.objects.create(
                            document=root,
                            field=custom_field,
                            value_text="User supplied annotation",
                        )
                    elif relation == "tag":
                        Document.objects.get(pk=root.pk).tags.add(user_tag)
                    else:
                        assign_perm("view_document", user, root)
                    editor = Document.objects.get(pk=root.pk)
                    editor.save(update_fields=["modified"])

                with mock.patch(
                    "documents.signals.handlers.apply_assignment_to_document",
                    side_effect=edit_during_assignment,
                ):
                    self.retry(document)
                if relation == "custom_field":
                    self.assertEqual(
                        CustomFieldInstance.objects.get(
                            document=document,
                            field=custom_field,
                        ).value_text,
                        "User supplied annotation",
                    )
                elif relation == "tag":
                    self.assertTrue(document.tags.filter(pk=user_tag.pk).exists())
                else:
                    from guardian.shortcuts import get_perms

                    self.assertIn("view_document", get_perms(user, document))
                self.assertFalse(WorkflowRun.objects.filter(workflow=workflow).exists())
                self.assertEqual(
                    stored_pages(document)[0]["provenance"][CONTEXT]["state"],
                    "review",
                )
                Workflow.objects.all().delete()

    def test_own_custom_field_and_permission_changes_are_accepted(self):
        from guardian.shortcuts import get_perms

        field = CustomField.objects.create(
            name="Workflow field",
            data_type=CustomField.FieldDataType.STRING,
        )
        user = UserFactory()
        assignment = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
            assign_custom_fields_values={str(field.pk): "Automatic annotation"},
        )
        assignment.assign_custom_fields.add(field)
        assignment.assign_view_users.add(user)
        workflow = self.workflow(assignment)
        document, _ = self.consume()
        self.failure = None
        self.retry(document)
        self.assertEqual(
            CustomFieldInstance.objects.get(document=document, field=field).value_text,
            "Automatic annotation",
        )
        self.assertIn("view_document", get_perms(user, document))
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "completed",
        )

    def test_same_target_custom_field_edit_at_sql_write_is_preserved(self):
        field = CustomField.objects.create(
            name="Annotation",
            data_type=CustomField.FieldDataType.STRING,
        )
        self.initial_custom_fields([(field, "At upload")])
        action = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
            assign_custom_fields_values={str(field.pk): "Automatic"},
        )
        action.assign_custom_fields.add(field)
        self.workflow(action)
        document, _ = self.consume()
        instance = CustomFieldInstance.objects.get(document=document, field=field)
        self.failure = None
        actual = QuerySet.update
        edits = []

        def edit_before_sql(query, **updates):
            if (
                query.model is CustomFieldInstance
                and updates.get("value_text") == "Automatic"
            ):
                edits.append(True)
                actual(
                    CustomFieldInstance.objects.filter(pk=instance.pk),
                    value_text="Deliberate at SQL boundary",
                )
                Document.objects.get(pk=document.pk).save(update_fields=["modified"])
            return actual(query, **updates)

        with mock.patch.object(
            QuerySet,
            "update",
            autospec=True,
            side_effect=edit_before_sql,
        ):
            self.retry(document)
        self.assertEqual(edits, [True])
        self.assertEqual(
            CustomFieldInstance.objects.get(pk=instance.pk).value_text,
            "Deliberate at SQL boundary",
        )
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "review",
        )

    def test_conditional_custom_field_removal_preserves_new_or_edited_rows(self):
        for index, new_field in enumerate((False, True)):
            with self.subTest(new_field=new_field):
                field = CustomField.objects.create(
                    name=f"Existing field {index}",
                    data_type=CustomField.FieldDataType.STRING,
                )
                added = CustomField.objects.create(
                    name=f"User field {index}",
                    data_type=CustomField.FieldDataType.STRING,
                )
                self.initial_custom_fields([(field, "At upload")])
                self.workflow(
                    WorkflowAction.objects.create(
                        type=WorkflowAction.WorkflowActionType.REMOVAL,
                        remove_all_custom_fields=True,
                    ),
                )
                self.failure = 402
                document, _ = self.consume(asn=1401 + index)
                instance = CustomFieldInstance.objects.get(
                    document=document,
                    field=field,
                )
                self.failure = None
                actual = QuerySet._raw_delete
                edits = []

                def edit_before_sql(query, using):
                    if query.model is CustomFieldInstance and not edits:
                        edits.append(True)
                        if new_field:
                            CustomFieldInstance.objects.create(
                                document_id=document.pk,
                                field=added,
                                value_text="New user annotation",
                            )
                        else:
                            CustomFieldInstance.objects.filter(pk=instance.pk).update(
                                value_text="Edited user annotation",
                            )
                    return actual(query, using)

                with mock.patch.object(
                    QuerySet,
                    "_raw_delete",
                    autospec=True,
                    side_effect=edit_before_sql,
                ):
                    self.retry(document)
                self.assertEqual(edits, [True])
                preserved = CustomFieldInstance.objects.get(
                    document=document,
                    field=added if new_field else field,
                )
                self.assertIn("user annotation", preserved.value_text)
                self.assertEqual(
                    stored_pages(document)[0]["provenance"][CONTEXT]["state"],
                    "review",
                )
                Workflow.objects.all().delete()

    def test_all_custom_field_types_allow_deferred_creation_and_update(self):
        linked = DocumentFactory()
        values = {
            "string": ("before", "after"),
            "url": ("https://before.test", "https://after.test"),
            "date": ("2020-01-02", "2024-01-02"),
            "boolean": (False, True),
            "integer": (1, 2),
            "float": (1.5, 2.5),
            "monetary": ("JPY10", "JPY20"),
            "documentlink": ([], [linked.pk]),
            "select": ("old", "option"),
            "longtext": ("old text", "long\ntext"),
        }
        for index, existing in enumerate((False, True)):
            with self.subTest(existing=existing):
                fields = [
                    CustomField.objects.create(
                        name=f"Field {index} {kind}",
                        data_type=kind,
                    )
                    for kind, label in CustomField.FieldDataType.choices
                ]
                if existing:
                    self.initial_custom_fields(
                        [(field, values[field.data_type][0]) for field in fields],
                    )
                action = WorkflowAction.objects.create(
                    type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
                    assign_custom_fields_values={
                        str(field.pk): values[field.data_type][1] for field in fields
                    },
                )
                action.assign_custom_fields.add(*fields)
                workflow = self.workflow(action)
                self.failure = 402
                document, _ = self.consume(asn=1501 + index)
                self.failure = None
                self.retry(document)
                self.assertEqual(
                    stored_pages(document)[0]["provenance"][CONTEXT]["state"],
                    "completed",
                )
                self.assertEqual(
                    WorkflowRun.objects.filter(workflow=workflow).count(),
                    1,
                )
                for field in fields:
                    value_field = CustomFieldInstance.get_value_field_name(
                        data_type=field.data_type,
                    )
                    expected = CustomFieldInstance._meta.get_field(
                        value_field,
                    ).to_python(values[field.data_type][1])
                    self.assertEqual(
                        getattr(
                            CustomFieldInstance.objects.get(
                                document=document,
                                field=field,
                            ),
                            value_field,
                        ),
                        expected,
                    )
                Workflow.objects.all().delete()

    @override_settings(
        FILENAME_FORMAT="{{title}}_{{custom_fields|get_cf_value('Annotation')}}",
    )
    def test_conditional_custom_field_update_keeps_filename_and_audit_effects(self):
        from auditlog.models import LogEntry
        from django.contrib.contenttypes.models import ContentType

        field = CustomField.objects.create(
            name="Annotation",
            data_type=CustomField.FieldDataType.STRING,
        )
        self.initial_custom_fields([(field, "At upload")])
        action = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
            assign_title="Staged title",
            assign_custom_fields_values={str(field.pk): "Automatic"},
        )
        action.assign_custom_fields.add(field)
        self.workflow(action)
        document, _ = self.consume()
        instance = CustomFieldInstance.objects.get(document=document, field=field)
        self.failure = None
        self.retry(document)
        self.assertEqual(document.title, "Staged title")
        self.assertIn("Staged title_Automatic", document.filename)
        self.assertTrue(document.source_path.is_file())
        entries = LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(CustomFieldInstance),
            object_pk=str(instance.pk),
            action=LogEntry.Action.UPDATE,
        )
        self.assertTrue(
            any(
                entry.changes.get("value_text") == ["At upload", "Automatic"]
                for entry in entries
            ),
        )

    @override_settings(
        FILENAME_FORMAT="{{title}}_{{custom_fields|get_cf_value('Annotation', 'blank')}}",
    )
    def test_conditional_custom_field_removal_keeps_filename_and_audit_effects(self):
        from auditlog.models import LogEntry
        from django.contrib.contenttypes.models import ContentType

        field = CustomField.objects.create(
            name="Annotation",
            data_type=CustomField.FieldDataType.STRING,
        )
        self.initial_custom_fields([(field, "At upload")])
        self.workflow(
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.REMOVAL,
                remove_all_custom_fields=True,
            ),
        )
        document, _ = self.consume()
        instance_id = CustomFieldInstance.objects.get(document=document, field=field).pk
        self.failure = None
        self.retry(document)
        self.assertEqual(
            stored_pages(document)[0]["provenance"][CONTEXT]["state"],
            "completed",
        )
        self.assertFalse(document.custom_fields.exists())
        self.assertIn("Water Supply Notices_blank", document.filename)
        self.assertTrue(document.source_path.is_file())
        self.assertEqual(
            LogEntry.objects.filter(
                content_type=ContentType.objects.get_for_model(CustomFieldInstance),
                object_pk=str(instance_id),
                action=LogEntry.Action.DELETE,
            ).count(),
            1,
        )
