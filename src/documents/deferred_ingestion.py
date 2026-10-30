"""Resume initial ingestion metadata after saved OCR work has finished."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import shutil
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout
from guardian.models import GroupObjectPermission
from guardian.models import UserObjectPermission

from documents.models import Document
from documents.models import DocumentPage
from documents.models import Note
from documents.models import Workflow
from documents.models import WorkflowTrigger
from documents.ocr_status import status_q
from documents.page_data import stored_pages
from documents.versioning import get_latest_version_for_root

logger = logging.getLogger("paperless.deferred_ingestion")
CONTEXT = "deferred_ingestion"
UPDATE_CONTEXT = "deferred_file_update"
METADATA_FIELDS = (
    "title",
    "created",
    "owner_id",
    "archive_serial_number",
    "correspondent_id",
    "document_type_id",
    "storage_path_id",
    "modified",
)


class MetadataChanged(RuntimeError):
    """A user edit superseded an in-progress deferred metadata operation."""


def begin_metadata_guard(document: Document, *, expected=None) -> None:
    document._deferred_metadata_guard = {
        "fingerprint": metadata_fingerprint(document) if expected is None else expected,
        "fields": {field: getattr(document, field) for field in METADATA_FIELDS},
        "relations": metadata_relations(document),
        "custom_field_ids": dict(document.custom_fields.values_list("field_id", "pk")),
    }


def check_metadata_guard(document: Document) -> None:
    guard = getattr(document, "_deferred_metadata_guard", None)
    if guard is None:
        return
    fresh = Document.objects.get(pk=document.pk)
    if (
        metadata_fingerprint(fresh) != guard["fingerprint"]
        or metadata_relations(fresh) != guard["relations"]
        or any(
            getattr(fresh, field) != value for field, value in guard["fields"].items()
        )
    ):
        raise MetadataChanged("Deferred metadata was superseded by a user edit")


def advance_metadata_guard(
    document: Document,
    *,
    changed_fields=None,
    expected_relations=None,
) -> None:
    guard = getattr(document, "_deferred_metadata_guard", None)
    if guard is None:
        return
    fresh = Document.objects.get(pk=document.pk)
    expected_fields = {**guard["fields"], **(changed_fields or {})}
    for field, value in expected_fields.items():
        if field != "modified" and getattr(fresh, field) != value:
            raise MetadataChanged("Deferred metadata was superseded by a user edit")
    relations = metadata_relations(fresh)
    if relations != (
        guard["relations"] if expected_relations is None else expected_relations
    ):
        raise MetadataChanged(
            "Deferred metadata was superseded by a user relation edit",
        )
    guard["fields"] = {field: getattr(fresh, field) for field in METADATA_FIELDS}
    guard["fingerprint"] = metadata_fingerprint(fresh)
    guard["relations"] = relations
    guard["custom_field_ids"] = dict(fresh.custom_fields.values_list("field_id", "pk"))


def metadata_relations(document: Document) -> dict:
    from documents.models import CustomFieldInstance

    value_fields = sorted(set(CustomFieldInstance.TYPE_TO_DATA_STORE_NAME_MAP.values()))
    custom_fields = {
        row["field_id"]: {field: row[field] for field in value_fields}
        for row in document.custom_fields.values("field_id", *value_fields)
    }
    content_type = ContentType.objects.get_for_model(Document)
    permissions = set()
    for kind, manager, principal in (
        ("user", UserObjectPermission.objects, "user_id"),
        ("group", GroupObjectPermission.objects, "group_id"),
    ):
        permissions.update(
            (kind, principal_id, codename)
            for principal_id, codename in manager.filter(
                object_pk=str(document.pk),
                content_type=content_type,
            ).values_list(principal, "permission__codename")
        )
    return {
        "tags": set(document.tags.exclude(status_q()).values_list("pk", flat=True)),
        "custom_fields": custom_fields,
        "permissions": permissions,
    }


def expected_tag_relations(document: Document, *, added=(), removed=(), clear=False):
    from documents.models import Tag

    guard = getattr(document, "_deferred_metadata_guard", None)
    if guard is None:
        return None
    expected = copy.deepcopy(guard["relations"])
    tags = set() if clear else expected["tags"] - set(removed)
    for tag in added:
        tags.add(tag.pk)
        tags.update(tag.get_ancestors_pks())
    expected["tags"] = set(
        Tag.objects.filter(pk__in=tags)
        .exclude(status_q())
        .values_list("pk", flat=True),
    )
    return expected


def expected_action_relations(document: Document, action):
    """Compute the mutation's allowed result before executing its writes."""
    from documents.models import CustomFieldInstance
    from documents.models import WorkflowAction

    guard = getattr(document, "_deferred_metadata_guard", None)
    if guard is None:
        return None
    expected = copy.deepcopy(guard["relations"])
    assignment = action.type == WorkflowAction.WorkflowActionType.ASSIGNMENT
    if assignment:
        expected = expected_tag_relations(document, added=action.assign_tags.all())
        for field in action.assign_custom_fields.all():
            value_field = CustomFieldInstance.get_value_field_name(
                data_type=field.data_type,
            )
            value = action.assign_custom_fields_values.get(str(field.pk), None)
            previous = expected["custom_fields"].get(field.pk)
            if previous is None:
                previous = dict.fromkeys(
                    set(CustomFieldInstance.TYPE_TO_DATA_STORE_NAME_MAP.values()),
                )
                expected["custom_fields"][field.pk] = previous
            elif value in (None, ""):
                continue
            previous[value_field] = CustomFieldInstance._meta.get_field(
                value_field,
            ).to_python(value)
    else:
        removed = set()
        for tag in action.remove_tags.all():
            removed.add(tag.pk)
            removed.update(tag.get_descendants_pks())
        expected = expected_tag_relations(
            document,
            removed=removed,
            clear=action.remove_all_tags,
        )
        if action.remove_all_custom_fields:
            expected["custom_fields"] = {}
        else:
            for field_id in action.remove_custom_fields.values_list("pk", flat=True):
                expected["custom_fields"].pop(field_id, None)
        if action.remove_all_permissions:
            expected["permissions"] = {
                grant
                for grant in expected["permissions"]
                if grant[2] not in ("view_document", "change_document")
            }
    for verb in ("view", "change"):
        for kind, plural in (("user", "users"), ("group", "groups")):
            manager = getattr(
                action,
                f"{'assign' if assignment else 'remove'}_{verb}_{plural}",
            )
            grants = {
                (kind, pk, f"{verb}_document")
                for pk in manager.values_list("pk", flat=True)
            }
            if assignment:
                expected["permissions"].update(grants)
            else:
                expected["permissions"].difference_update(grants)
    return expected


def save_guarded_metadata(document: Document, fields) -> None:
    """CAS only staged fields, then run filename changes outside SQL rollback."""
    guard = getattr(document, "_deferred_metadata_guard", None)
    if guard is None:
        if fields:
            document.save(update_fields=fields)
        return
    check_metadata_guard(document)
    updates = {
        Document._meta.get_field(field).attname: getattr(
            document,
            Document._meta.get_field(field).attname,
        )
        for field in fields
        if field != "modified"
    }
    updates = {
        field: value
        for field, value in updates.items()
        if value != guard["fields"].get(field)
    }
    if not updates:
        document.refresh_from_db()
        return
    before = guard["fields"]
    if not Document.objects.filter(pk=document.pk, **before).update(
        **updates,
        modified=timezone.now(),
    ):
        raise MetadataChanged("Deferred metadata was superseded by a user edit")
    document.refresh_from_db()
    from documents.signals.handlers import update_filename_and_move_files

    update_filename_and_move_files(sender=Document, instance=document)
    document.refresh_from_db()
    if settings.AUDIT_LOG_ENABLED:
        from auditlog.models import LogEntry

        LogEntry.objects.log_create(
            instance=document,
            action=LogEntry.Action.UPDATE,
            changes={
                field: [str(before[field]), str(value)]
                for field, value in updates.items()
            },
            additional_data={"reason": "Apply deferred ingestion metadata"},
        )
    advance_metadata_guard(document, changed_fields=updates)


@contextmanager
def current_attachment(document: Document):
    """Keep one immutable current-file snapshot for the lifetime of an event."""
    with tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR) as temporary:
        with FileLock(settings.MEDIA_LOCK):
            current = get_latest_version_for_root(document)
            source = current.archive_path or current.source_path
            name = Path(current.original_filename or "document.pdf").name
            if current.archive_path is not None:
                name = str(Path(name).with_suffix(".pdf"))
            attachment = Path(temporary) / name
            if source.is_file():
                shutil.copyfile(source, attachment)
            else:
                # Metadata-only workflows can run without reading file bytes.
                # An attachment action must fail on this current path rather
                # than fall back to a historical root file.
                attachment = source
        yield attachment


def file_key(document: Document) -> str:
    return f"{document.pk}:{document.archive_checksum or document.checksum}"


def metadata_lock(function):
    @wraps(function)
    def run(document_id):
        document = Document.objects.filter(pk=document_id).first()
        if document is None:
            return
        root_id = document.root_document_id or document.pk
        try:
            with FileLock(
                settings.DATA_DIR / f"document-ingestion-{root_id}.lock",
                timeout=0,
            ):
                return function(document_id)
        except Timeout:
            return

    return run


def configured_workflows(trigger_type) -> dict:
    return {
        str(workflow.pk): {
            str(action.pk): action.type for action in workflow.actions.all()
        }
        for workflow in Workflow.objects.filter(
            enabled=True,
            triggers__type=trigger_type,
        )
        .distinct()
        .prefetch_related("actions")
    }


def metadata_fingerprint(document: Document) -> str:
    """Keep a digest, without exposing permission or custom-field snapshots."""
    fields = (
        "title",
        "created",
        "owner_id",
        "archive_serial_number",
        "correspondent_id",
        "document_type_id",
        "storage_path_id",
    )
    data = {field: getattr(document, field) for field in fields}
    data["tags"] = sorted(
        document.tags.exclude(status_q()).values_list(
            "pk",
            flat=True,
        ),
    )
    data["custom_fields"] = list(document.custom_fields.order_by("pk").values())
    data["permissions"] = [
        list(
            manager.filter(
                object_pk=str(document.pk),
                content_type=ContentType.objects.get_for_model(Document),
            )
            .order_by("pk")
            .values(),
        )
        for manager in (UserObjectPermission.objects, GroupObjectPermission.objects)
    ]
    encoded = json.dumps(data, cls=DjangoJSONEncoder, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def capture(document: Document, pages: list[dict], *, allow_date: bool) -> None:
    workflows = (
        Workflow.objects.filter(
            enabled=True,
            triggers__type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
        )
        .distinct()
        .prefetch_related("actions")
    )
    context = {
        "document_id": document.pk,
        "state": "pending",
        "metadata": metadata_fingerprint(document),
        "allow_date": allow_date,
        "workflows": {
            str(workflow.pk): {
                str(action.pk): action.type for action in workflow.actions.all()
            }
            for workflow in workflows
        },
    }
    for page in pages:
        page["provenance"][CONTEXT] = copy.deepcopy(context)


def carry_context(document: Document, pages: list[dict]) -> None:
    """Keep collection intent when its first pages are removed or replaced."""
    root_id = document.root_document_id or document.pk
    candidates = [page["provenance"].get(CONTEXT) for page in pages]
    candidates.extend(
        provenance.get(CONTEXT)
        for provenance in DocumentPage.global_objects.filter(
            Q(document_id=root_id) | Q(document__root_document_id=root_id),
            provenance__has_key=CONTEXT,
        ).values_list("provenance", flat=True)
    )
    candidates = [
        context
        for context in candidates
        if context and context.get("document_id") == root_id
    ]
    if not candidates:
        return
    context = next(
        (item for item in candidates if item["state"] != "pending"),
        candidates[0],
    )
    for page in pages:
        page["provenance"][CONTEXT] = copy.deepcopy(context)


def _set_state(
    root_id: int,
    context: dict,
    state: str,
    *,
    key=CONTEXT,
    attach=False,
) -> None:
    records = list(
        DocumentPage.global_objects.filter(
            Q(document_id=root_id) | Q(document__root_document_id=root_id),
        ),
    )
    changed = []
    for record in records:
        previous = record.provenance.get(key, {})
        if attach or previous.get("document_id") == root_id:
            record.provenance[key] = {**context, "state": state}
            changed.append(record)
    if changed:
        DocumentPage.global_objects.bulk_update(changed, ["provenance"])


def prepare_file_event(
    document: Document,
    pages: list[dict],
    *,
    file_event: bool,
) -> None:
    root_id = document.root_document_id or document.pk
    initial = next(
        (
            page["provenance"].get(CONTEXT)
            for page in pages
            if (page["provenance"].get(CONTEXT, {}).get("document_id") == root_id)
        ),
        None,
    )
    previous = next(
        (
            provenance.get(UPDATE_CONTEXT)
            for provenance in DocumentPage.global_objects.filter(
                Q(document_id=root_id) | Q(document__root_document_id=root_id),
                provenance__has_key=UPDATE_CONTEXT,
            ).values_list("provenance", flat=True)
            if provenance[UPDATE_CONTEXT].get("document_id") == root_id
        ),
        None,
    )
    if (
        file_event
        and any(page["provenance"].get("ocr_status") == "pending" for page in pages)
        and not (initial and initial["state"] == "pending")
        and (previous is None or previous["state"] != "pending")
    ):
        root = Document.objects.get(pk=root_id)
        previous = {
            "document_id": root_id,
            "state": "pending",
            "metadata": metadata_fingerprint(root),
            "workflows": configured_workflows(
                WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
            ),
        }
        _set_state(root_id, previous, "pending", key=UPDATE_CONTEXT, attach=True)
    if previous:
        for page in pages:
            page["provenance"][UPDATE_CONTEXT] = copy.deepcopy(previous)


def suppress_updated_event(document: Document) -> bool:
    current = get_latest_version_for_root(document)
    pages = stored_pages(current) or []
    if any(page["provenance"].get("ocr_status") == "pending" for page in pages):
        return True
    return any(
        page["provenance"].get(key, {}).get("document_id") == document.pk
        and page["provenance"][key].get("file") == file_key(current)
        for page in pages
        for key in (CONTEXT, UPDATE_CONTEXT)
    )


@metadata_lock
def complete_file_event(document_id: int) -> None:
    from documents.ocr_queue import pending_pages
    from documents.signals import document_updated

    document = Document.objects.filter(pk=document_id).first()
    if document is None:
        return
    root_id = document.root_document_id or document.pk
    with transaction.atomic():
        root = Document.objects.select_for_update().get(pk=root_id)
        current = get_latest_version_for_root(root)
        if current.pk != document.pk or pending_pages(current):
            return
        context = next(
            (
                page["provenance"].get(UPDATE_CONTEXT)
                for page in stored_pages(current) or []
                if (
                    page["provenance"].get(UPDATE_CONTEXT, {}).get("document_id")
                    == root_id
                )
            ),
            None,
        )
        if context is None or context["state"] != "pending":
            return
        context = {**context, "file": file_key(current)}
        if metadata_fingerprint(root) != context["metadata"]:
            _set_state(root_id, context, "skipped", key=UPDATE_CONTEXT)
            Note.objects.get_or_create(
                document=root,
                note=(
                    "OCR is complete. Deferred file-update workflows were skipped "
                    "because metadata was edited while OCR was pending."
                ),
            )
            return
        _set_state(root_id, context, "running", key=UPDATE_CONTEXT)
    try:
        root._deferred_workflow_actions = context["workflows"]
        begin_metadata_guard(root, expected=context["metadata"])
        with current_attachment(root) as attachment:
            document_updated.send(
                sender=complete_file_event,
                document=root,
                original_file=attachment,
                skip_ai_index=True,
            )
    except Exception:
        _set_state(root_id, context, "review", key=UPDATE_CONTEXT)
        Note.objects.get_or_create(
            document=root,
            note=(
                "Deferred file-update workflows need review. Some actions may have "
                "completed and will not be replayed automatically."
            ),
        )
    else:
        _set_state(root_id, context, "completed", key=UPDATE_CONTEXT)


@metadata_lock
def complete(document_id: int) -> None:
    """Called after file commit, while the caller owns its document file lock.

    Claim the attempt durably before executing configured workflow side effects.
    Interrupted attempts require review; they are never automatically replayed.
    Metadata saves run in autocommit, so a later workflow error cannot roll back
    a filename database update while leaving its physical rename in place.
    """
    from documents.classifier import load_classifier
    from documents.ingestion_checks import collect_ingestion_checks
    from documents.ocr_queue import pending_pages
    from documents.plugins.date_parsing import get_date_parser
    from documents.signals import document_consumption_finished

    document = Document.objects.filter(pk=document_id).first()
    if document is None:
        return
    root_id = document.root_document_id or document.pk
    with transaction.atomic():
        root = Document.objects.select_for_update().get(pk=root_id)
        current = get_latest_version_for_root(root)
        if current.pk != document.pk or pending_pages(current):
            return
        pages = stored_pages(current) or []
        context = next(
            (
                page["provenance"][CONTEXT]
                for page in pages
                if page["provenance"].get(CONTEXT, {}).get("document_id") == root_id
            ),
            None,
        )
        if context is None or context["state"] != "pending":
            return
        context = {**context, "file": file_key(current)}
        if metadata_fingerprint(root) != context["metadata"]:
            _set_state(root_id, context, "skipped")
            Note.objects.get_or_create(
                document=root,
                note=(
                    "OCR is complete. Automatic ingestion metadata was skipped because "
                    "metadata was edited while OCR was pending. Review suggestions or run "
                    "the desired workflow explicitly."
                ),
            )
            return
        _set_state(root_id, context, "running")

    try:
        begin_metadata_guard(root, expected=context["metadata"])
        if context["allow_date"]:
            text = current.get_effective_content()
            checks = collect_ingestion_checks(None, text)
            with get_date_parser() as parser:
                selected = checks.printed_date or next(
                    parser.parse(current.original_filename or "", text),
                    None,
                )
            try:
                check_metadata_guard(root)
            except MetadataChanged:
                _set_state(root_id, context, "skipped")
                Note.objects.get_or_create(
                    document=root,
                    note="Automatic ingestion metadata was skipped because metadata changed during date selection.",
                )
                return
            if selected is not None:
                selected = Document._meta.get_field("created").to_python(selected)
                root.created = selected
                try:
                    save_guarded_metadata(root, ["created"])
                except MetadataChanged:
                    _set_state(root_id, context, "skipped")
                    Note.objects.get_or_create(
                        document=root,
                        note="Automatic ingestion metadata was skipped because metadata changed during date selection.",
                    )
                    return
        root._deferred_workflow_actions = context["workflows"]
        root._deferred_created_override = not context["allow_date"]
        classifier = load_classifier()
        check_metadata_guard(root)
        # Classifiers and workflows may rename the stored source. Attachments
        # need the same stable working copy ordinary ingestion supplies.
        with tempfile.TemporaryDirectory(dir=settings.SCRATCH_DIR) as temporary:
            attachment = Path(temporary) / (current.original_filename or "document.pdf")
            attachment = Path(temporary) / attachment.name
            with FileLock(settings.MEDIA_LOCK):
                current.refresh_from_db()
                shutil.copyfile(current.source_path, attachment)
            document_consumption_finished.send(
                sender=complete,
                document=root,
                classifier=classifier,
                original_file=attachment,
                skip_ai_index=True,
            )
    except Exception:
        logger.warning("Deferred ingestion metadata requires review")
        _set_state(root_id, context, "review")
        Note.objects.get_or_create(
            document=root,
            note=(
                "OCR is complete, but deferred ingestion metadata needs review. "
                "Some workflow actions may already have completed and will not be "
                "replayed automatically. Review metadata and run only the desired "
                "remaining actions explicitly."
            ),
        )
    else:
        _set_state(root_id, context, "completed")


def recover_interrupted(document: Document) -> None:
    """The recovery caller must own the root's nonblocking file lock."""
    pages = stored_pages(get_latest_version_for_root(document)) or []
    for key in (CONTEXT, UPDATE_CONTEXT):
        context = next(
            (
                page["provenance"].get(key)
                for page in pages
                if (
                    page["provenance"].get(key, {}).get("document_id") == document.pk
                    and page["provenance"].get(key, {}).get("state") == "running"
                )
            ),
            None,
        )
        if not context:
            continue
        _set_state(document.pk, context, "review", key=key)
        Note.objects.get_or_create(
            document=document,
            note=(
                "Deferred ingestion was interrupted. Some workflow actions may have "
                "completed. Review the document before explicitly running any "
                "remaining actions; they will not be replayed automatically."
            ),
        )
