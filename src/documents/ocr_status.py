"""Identify service status tags without claiming unrelated users' private tags."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q

from documents.models import Tag

STATUS_NAMES = (
    "ocr-pending",
    "ocr-fail",
    "ocr-processing",
    "ocr-ok",
    "ocr-skip",
    "ocr-review",
)


def configured_tags() -> dict[str, Tag]:
    mapping = getattr(settings, "OCR_STATUS_TAG_IDS", {})
    if not isinstance(mapping, dict) or set(mapping) - set(STATUS_NAMES):
        raise ImproperlyConfigured(
            "OCR status tag IDs must map known logical status names.",
        )
    if any(type(pk) is not int or pk < 1 for pk in mapping.values()) or len(
        set(mapping.values()),
    ) != len(mapping):
        raise ImproperlyConfigured(
            "OCR status tag IDs must be distinct positive integers.",
        )
    found = Tag.objects.in_bulk(mapping.values())
    for name, pk in mapping.items():
        if pk not in found or found[pk].name.casefold() != name:
            raise ImproperlyConfigured(
                f"Configured OCR status tag for {name} is missing or has a different name.",
            )
    return {name: found[pk] for name, pk in mapping.items()}


def status_q(names=STATUS_NAMES) -> Q:
    configured = configured_tags()
    result = Q(pk__in=[])
    for name in names:
        result |= Q(name__iexact=name, owner__isnull=True)
        if name in configured:
            result |= Q(pk=configured[name].pk)
    return result


def status_tag(name: str) -> Tag:
    if name not in STATUS_NAMES:
        raise ValueError("Unknown OCR status name.")
    configured = configured_tags()
    if name in configured:
        return configured[name]
    existing = (
        Tag.objects.filter(name__iexact=name, owner__isnull=True).order_by("pk").first()
    )
    if existing is not None:
        return existing
    return Tag.objects.get_or_create(name=name, owner=None)[0]


def attach_completed_status(document, status: str) -> None:
    if status not in ("ok", "skip"):
        return
    wanted = status_tag(f"ocr-{status}")
    document.tags.add(wanted)
    document.tags.remove(
        *document.tags.filter(
            status_q(name for name in STATUS_NAMES if name != "ocr-review"),
        ).exclude(pk=wanted.pk),
    )
