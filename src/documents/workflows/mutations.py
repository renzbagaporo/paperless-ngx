import copy
import logging

from django.conf import settings
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.db.models.signals import post_save
from django.utils import timezone
from guardian.shortcuts import remove_perm

from documents.data_models import DocumentMetadataOverrides
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import WorkflowAction
from documents.permissions import set_permissions_for_object
from documents.templating.workflows import parse_w_workflow_placeholders

logger = logging.getLogger("paperless.workflows.mutations")


def _guarded_custom_field_query(document, field_id):
    guard = document._deferred_metadata_guard
    root_ids = Document.objects.filter(pk=document.pk, **guard["fields"]).values("pk")
    # Keep value predicates on the target table. A root join would move them
    # inside a PK subquery, weakening rechecks after a concurrent row update.
    previous = guard["relations"]["custom_fields"][field_id]
    query = CustomFieldInstance.objects.filter(
        pk=guard["custom_field_ids"].get(field_id),
        field_id=field_id,
        document_id=document.pk,
        document_id__in=root_ids,
        **{
            key: value for key, value in previous.items() if key != "value_document_ids"
        },
    )
    if previous["value_document_ids"] is None:
        # Django's JSONField exact=None means JSON null, not SQL NULL. Both
        # represent an unset document-link value in the model snapshot.
        return query.filter(
            Q(value_document_ids__isnull=True) | Q(value_document_ids=None),
        )
    return query.filter(value_document_ids=previous["value_document_ids"])


def _custom_field_audit(instance, *, changes):
    if settings.AUDIT_LOG_ENABLED:
        from auditlog.models import LogEntry

        LogEntry.objects.log_create(
            instance=instance,
            action=LogEntry.Action.UPDATE,
            changes=changes,
            additional_data={"reason": "Apply deferred workflow custom field mutation"},
        )


def _assign_deferred_custom_field(document, field, value):
    from documents.deferred_ingestion import MetadataChanged
    from documents.deferred_ingestion import advance_metadata_guard

    guard = document._deferred_metadata_guard
    previous = guard["relations"]["custom_fields"].get(field.pk)
    if previous is not None and value in (None, ""):
        return
    value_field = CustomFieldInstance.get_value_field_name(data_type=field.data_type)
    value = CustomFieldInstance._meta.get_field(value_field).to_python(value)
    if previous is not None and previous[value_field] == value:
        return
    expected = copy.deepcopy(guard["relations"])
    if previous is None:
        expected["custom_fields"][field.pk] = dict.fromkeys(
            set(CustomFieldInstance.TYPE_TO_DATA_STORE_NAME_MAP.values()),
        )
    expected["custom_fields"][field.pk][value_field] = value
    if previous is None:
        instance = CustomFieldInstance(
            document_id=document.pk,
            field=field,
            **{value_field: value},
        )
        try:
            with transaction.atomic():
                if not Document.objects.filter(
                    pk=document.pk,
                    **guard["fields"],
                ).exists():
                    raise MetadataChanged("Deferred custom field metadata changed")
                CustomFieldInstance.objects.bulk_create([instance])
                if not Document.objects.filter(
                    pk=document.pk,
                    **guard["fields"],
                ).exists():
                    raise MetadataChanged("Deferred custom field metadata changed")
        except IntegrityError as error:
            raise MetadataChanged("A concurrent custom field was preserved") from error
        instance.refresh_from_db()
    else:
        instance = CustomFieldInstance.objects.filter(
            pk=guard["custom_field_ids"].get(field.pk),
        ).first()
        if instance is None or not _guarded_custom_field_query(
            document,
            field.pk,
        ).update(**{value_field: value}):
            raise MetadataChanged("A concurrent custom field edit was preserved")
        instance.refresh_from_db()
        _custom_field_audit(
            instance,
            changes={value_field: [str(previous[value_field]), str(value)]},
        )
    # QuerySet writes avoid unconditional save(). Publish the usual post-save
    # effects after SQL, with a separately loaded document so filename handlers
    # cannot discard the parent workflow's staged direct-field assignments.
    post_save.send(
        sender=CustomFieldInstance,
        instance=instance,
        created=previous is None,
        raw=False,
        using=instance._state.db,
        update_fields=None if previous is None else frozenset((value_field,)),
    )
    advance_metadata_guard(document, expected_relations=expected)


def _remove_deferred_custom_fields(document, field_ids):
    from documents.deferred_ingestion import MetadataChanged
    from documents.deferred_ingestion import advance_metadata_guard
    from documents.signals.handlers import update_filename_and_move_files

    for field_id in field_ids:
        guard = document._deferred_metadata_guard
        instance = CustomFieldInstance.objects.filter(
            pk=guard["custom_field_ids"].get(field_id),
        ).first()
        query = _guarded_custom_field_query(document, field_id)
        if instance is None or not query._raw_delete(using=query.db):
            raise MetadataChanged("A concurrent custom field edit was preserved")
        post_delete.send(
            sender=CustomFieldInstance,
            instance=instance,
            using=query.db,
            origin=query,
        )
        update_filename_and_move_files(
            sender=Document,
            instance=Document.objects.get(pk=document.pk),
        )
        expected = copy.deepcopy(guard["relations"])
        expected["custom_fields"].pop(field_id)
        advance_metadata_guard(document, expected_relations=expected)


def _prepare_deferred_custom_fields(document, action):
    from documents.deferred_ingestion import advance_metadata_guard
    from documents.deferred_ingestion import expected_action_relations

    expected = expected_action_relations(document, action)
    expected["custom_fields"] = copy.deepcopy(
        document._deferred_metadata_guard["relations"]["custom_fields"],
    )
    # Tags and grants earlier in this same action have known allowed effects;
    # custom fields must still match their prior values before their SQL writes.
    advance_metadata_guard(document, expected_relations=expected)


def apply_assignment_to_document(
    action: WorkflowAction,
    document: Document,
    logging_group,
):
    """
    Apply assignment actions to a Document instance.

    action: WorkflowAction, annotated with 'has_assign_*' boolean fields
    """
    if action.has_assign_tags:
        # Apply to a freshly-fetched instance rather than the shared `document`.
        # Document.tags.add() fires an m2m_changed signal that ultimately calls
        # instance.refresh_from_db(), which would discard any other unsaved
        # assignment fields (e.g. storage_path) already staged on `document`.
        Document.objects.get(pk=document.pk).add_nested_tags(action.assign_tags.all())

    if action.assign_correspondent:
        document.correspondent = action.assign_correspondent

    if action.assign_document_type:
        document.document_type = action.assign_document_type

    if action.assign_storage_path:
        document.storage_path = action.assign_storage_path

    if action.assign_owner:
        document.owner = action.assign_owner

    if action.assign_title:
        try:
            title = parse_w_workflow_placeholders(
                action.assign_title,
                document.correspondent.name if document.correspondent else "",
                document.document_type.name if document.document_type else "",
                document.owner.username if document.owner else "",
                timezone.localtime(document.added),
                document.original_filename or "",
                document.filename or "",
                document.created,
                "",  # dont pass the title to avoid recursion
                "",  # no urls in titles
                document.pk,
            )
            if title:
                document.title = title
        except Exception:  # pragma: no cover
            logger.exception(
                f"Error occurred parsing title assignment '{action.assign_title}', falling back to original",
                extra={"group": logging_group},
            )

    if any(
        [
            action.has_assign_view_users,
            action.has_assign_view_groups,
            action.has_assign_change_users,
            action.has_assign_change_groups,
        ],
    ):
        permissions = {
            "view": {
                "users": action.assign_view_users.values_list("id", flat=True),
                "groups": action.assign_view_groups.values_list("id", flat=True),
            },
            "change": {
                "users": action.assign_change_users.values_list("id", flat=True),
                "groups": action.assign_change_groups.values_list("id", flat=True),
            },
        }
        set_permissions_for_object(
            permissions=permissions,
            object=document,
            merge=True,
        )

    if action.has_assign_custom_fields:
        if getattr(document, "_deferred_metadata_guard", None) is not None:
            _prepare_deferred_custom_fields(document, action)
            for field in action.assign_custom_fields.all():
                _assign_deferred_custom_field(
                    document,
                    field,
                    action.assign_custom_fields_values.get(str(field.pk), None),
                )
            return
        for field in action.assign_custom_fields.all():
            value_field_name = CustomFieldInstance.get_value_field_name(
                data_type=field.data_type,
            )
            args = {
                value_field_name: action.assign_custom_fields_values.get(
                    str(field.pk),
                    None,
                ),
            }
            # for some reason update_or_create doesn't work here
            instance = CustomFieldInstance.objects.filter(
                field=field,
                document=document,
            ).first()
            # empty string is indistinguishable from no value in the UI
            if instance and args[value_field_name] not in (None, ""):
                setattr(instance, value_field_name, args[value_field_name])
                instance.save()
            elif not instance:
                CustomFieldInstance.objects.create(
                    **args,
                    field=field,
                    document=document,
                )


def apply_assignment_to_overrides(
    action: WorkflowAction,
    overrides: DocumentMetadataOverrides,
):
    """
    Apply assignment actions to DocumentMetadataOverrides.

    action: WorkflowAction, annotated with 'has_assign_*' boolean fields
    """
    if action.has_assign_tags:
        if overrides.tag_ids is None:
            overrides.tag_ids = []
        tag_ids_to_add: set[int] = set()
        for tag in action.assign_tags.all():
            tag_ids_to_add.add(tag.pk)
            tag_ids_to_add.update(int(pk) for pk in tag.get_ancestors_pks())

        overrides.tag_ids = list(set(overrides.tag_ids) | tag_ids_to_add)

    if action.assign_correspondent:
        overrides.correspondent_id = action.assign_correspondent.pk

    if action.assign_document_type:
        overrides.document_type_id = action.assign_document_type.pk

    if action.assign_storage_path:
        overrides.storage_path_id = action.assign_storage_path.pk

    if action.assign_owner:
        overrides.owner_id = action.assign_owner.pk

    if action.assign_title:
        overrides.title = action.assign_title

    if any(
        [
            action.has_assign_view_users,
            action.has_assign_view_groups,
            action.has_assign_change_users,
            action.has_assign_change_groups,
        ],
    ):
        overrides.view_users = list(
            set(
                (overrides.view_users or [])
                + list(action.assign_view_users.values_list("id", flat=True)),
            ),
        )
        overrides.view_groups = list(
            set(
                (overrides.view_groups or [])
                + list(action.assign_view_groups.values_list("id", flat=True)),
            ),
        )
        overrides.change_users = list(
            set(
                (overrides.change_users or [])
                + list(action.assign_change_users.values_list("id", flat=True)),
            ),
        )
        overrides.change_groups = list(
            set(
                (overrides.change_groups or [])
                + list(action.assign_change_groups.values_list("id", flat=True)),
            ),
        )

    if action.has_assign_custom_fields:
        if overrides.custom_fields is None:
            overrides.custom_fields = {}
        overrides.custom_fields.update(
            {
                field.pk: action.assign_custom_fields_values.get(
                    str(field.pk),
                    None,
                )
                for field in action.assign_custom_fields.all()
            },
        )


def apply_removal_to_document(
    action: WorkflowAction,
    document: Document,
):
    """
    Apply removal actions to a Document instance.

    action: WorkflowAction, annotated with 'has_remove_*' boolean fields
    """

    if action.remove_all_tags:
        Document.objects.get(pk=document.pk).tags.clear()
    else:
        tag_ids_to_remove: set[int] = set()
        for tag in action.remove_tags.all():
            tag_ids_to_remove.add(tag.pk)
            tag_ids_to_remove.update(int(pk) for pk in tag.get_descendants_pks())

        if tag_ids_to_remove:
            Document.objects.get(pk=document.pk).tags.remove(*tag_ids_to_remove)

    if action.remove_all_correspondents or (
        document.correspondent
        and action.remove_correspondents.filter(pk=document.correspondent.pk).exists()
    ):
        document.correspondent = None

    if action.remove_all_document_types or (
        document.document_type
        and action.remove_document_types.filter(pk=document.document_type.pk).exists()
    ):
        document.document_type = None

    if action.remove_all_storage_paths or (
        document.storage_path
        and action.remove_storage_paths.filter(pk=document.storage_path.pk).exists()
    ):
        document.storage_path = None

    if action.remove_all_owners or (
        document.owner and action.remove_owners.filter(pk=document.owner.pk).exists()
    ):
        document.owner = None

    if action.remove_all_permissions:
        permissions = {
            "view": {"users": [], "groups": []},
            "change": {"users": [], "groups": []},
        }
        set_permissions_for_object(
            permissions=permissions,
            object=document,
            merge=False,
        )

    if any(
        [
            action.has_remove_view_users,
            action.has_remove_view_groups,
            action.has_remove_change_users,
            action.has_remove_change_groups,
        ],
    ):
        for user in action.remove_view_users.all():
            remove_perm("view_document", user, document)
        for user in action.remove_change_users.all():
            remove_perm("change_document", user, document)
        for group in action.remove_view_groups.all():
            remove_perm("view_document", group, document)
        for group in action.remove_change_groups.all():
            remove_perm("change_document", group, document)

    if getattr(document, "_deferred_metadata_guard", None) is not None and (
        action.remove_all_custom_fields or action.has_remove_custom_fields
    ):
        _prepare_deferred_custom_fields(document, action)
        field_ids = set(document._deferred_metadata_guard["relations"]["custom_fields"])
        if not action.remove_all_custom_fields:
            field_ids &= set(action.remove_custom_fields.values_list("pk", flat=True))
        _remove_deferred_custom_fields(document, sorted(field_ids))
    elif action.remove_all_custom_fields:
        CustomFieldInstance.objects.filter(document=document).hard_delete()
    elif action.has_remove_custom_fields:
        CustomFieldInstance.objects.filter(
            field__in=action.remove_custom_fields.all(),
            document=document,
        ).hard_delete()


def apply_removal_to_overrides(
    action: WorkflowAction,
    overrides: DocumentMetadataOverrides,
):
    """
    Apply removal actions to DocumentMetadataOverrides.

    action: WorkflowAction, annotated with 'has_remove_*' boolean fields
    """
    if action.remove_all_tags:
        overrides.tag_ids = None
    elif overrides.tag_ids:
        tag_ids_to_remove: set[int] = set()
        for tag in action.remove_tags.all():
            tag_ids_to_remove.add(tag.pk)
            tag_ids_to_remove.update(int(pk) for pk in tag.get_descendants_pks())

        overrides.tag_ids = [t for t in overrides.tag_ids if t not in tag_ids_to_remove]

    if action.remove_all_correspondents or (
        overrides.correspondent_id
        and action.remove_correspondents.filter(pk=overrides.correspondent_id).exists()
    ):
        overrides.correspondent_id = None

    if action.remove_all_document_types or (
        overrides.document_type_id
        and action.remove_document_types.filter(pk=overrides.document_type_id).exists()
    ):
        overrides.document_type_id = None

    if action.remove_all_storage_paths or (
        overrides.storage_path_id
        and action.remove_storage_paths.filter(pk=overrides.storage_path_id).exists()
    ):
        overrides.storage_path_id = None

    if action.remove_all_owners or (
        overrides.owner_id
        and action.remove_owners.filter(pk=overrides.owner_id).exists()
    ):
        overrides.owner_id = None

    if action.remove_all_permissions:
        overrides.view_users = None
        overrides.view_groups = None
        overrides.change_users = None
        overrides.change_groups = None
    elif any(
        [
            action.has_remove_view_users,
            action.has_remove_view_groups,
            action.has_remove_change_users,
            action.has_remove_change_groups,
        ],
    ):
        if overrides.view_users:
            for user in action.remove_view_users.filter(pk__in=overrides.view_users):
                overrides.view_users.remove(user.pk)
        if overrides.change_users:
            for user in action.remove_change_users.filter(
                pk__in=overrides.change_users,
            ):
                overrides.change_users.remove(user.pk)
        if overrides.view_groups:
            for group in action.remove_view_groups.filter(pk__in=overrides.view_groups):
                overrides.view_groups.remove(group.pk)
        if overrides.change_groups:
            for group in action.remove_change_groups.filter(
                pk__in=overrides.change_groups,
            ):
                overrides.change_groups.remove(group.pk)

    if action.remove_all_custom_fields:
        overrides.custom_fields = None
    elif action.has_remove_custom_fields and overrides.custom_fields:
        for field in action.remove_custom_fields.filter(
            pk__in=overrides.custom_fields.keys(),
        ):
            overrides.custom_fields.pop(field.pk, None)
