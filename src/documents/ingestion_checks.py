"""Optional, local OCR checks shared by consumption and document-file updates."""

import logging
import re
from dataclasses import dataclass
from datetime import date
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from documents.printed_dates import normalise
from documents.printed_dates import printed_dates

logger = logging.getLogger("paperless.ingestion_checks")


@dataclass(frozen=True)
class IngestionChecks:
    printed_date: date | None = None
    date_review: str | None = None
    suspicious_pages: tuple[int, ...] = ()


def _numbers(text: str) -> set[str]:
    text = normalise(text)
    text = re.sub(r"(?<=\d)[,\s](?=\d{3}\b)", "", text)
    compact = re.sub(r"\s", "", text)
    return set(re.findall(r"\d{1,4}年\d{1,2}月\d{1,2}日", compact)) | set(
        re.findall(r"\d{3,}", text),
    )


def suspicious_number_pages(pages: list[dict]) -> tuple[int, ...]:
    """Flag Markdown-only numbers supported by a different page's plain OCR."""
    found = []
    plain = [_numbers(page.get("plain_text", "")) for page in pages]
    for index, page in enumerate(pages):
        if not page.get("plain_text") or not page.get("markdown"):
            continue
        digits = re.sub(r"\D", "", normalise(page["plain_text"]))
        unexplained = {
            value
            for value in _numbers(page["markdown"]) - plain[index]
            if re.sub(r"\D", "", value) not in digits
        }
        siblings = set().union(
            *(values for other, values in enumerate(plain) if other != index),
        )
        if unexplained & siblings:
            found.append(index + 1)
    return tuple(found)


def collect_ingestion_checks(parser, text: str) -> IngestionChecks:
    """Never let an advisory check fail document ingestion or make a cloud request."""
    try:
        selected = None
        review = None
        if settings.OCR_VALIDATE_PRINTED_DATES:
            candidates = printed_dates(text)
            maximum = timezone.localdate() + timedelta(days=1)
            issue_dates = {
                item.value
                for item in candidates
                if item.kind == "issue"
                and item.value is not None
                and date(1900, 1, 1) <= item.value <= maximum
            }
            invalid_issue = any(
                item.kind == "issue"
                and (
                    item.value is None or not date(1900, 1, 1) <= item.value <= maximum
                )
                for item in candidates
            )
            unlabelled_conflict = any(
                item.kind is None and item.value not in issue_dates
                for item in candidates
            )
            if len(issue_dates) == 1 and not invalid_issue and not unlabelled_conflict:
                selected = issue_dates.pop()
            elif candidates:
                review = "Printed-date review: no single valid, explicitly issue-labeled date was established from OCR text. The existing date selection was retained; check the printed date if needed."
        pages = []
        if settings.OCR_REVIEW_CROSS_PAGE_NUMBERS:
            getter = getattr(parser, "get_review_pages", None)
            pages = getter() if callable(getter) else []
        return IngestionChecks(selected, review, suspicious_number_pages(pages))
    except Exception:
        # Do not expose private OCR text or provider errors in logs.
        logger.warning("Optional ingestion checks could not be completed")
        return IngestionChecks()


def apply_ingestion_checks(
    document,
    checks: IngestionChecks,
    *,
    page_offset: int = 0,
    preserve_metadata: bool = True,
) -> None:
    """Attach private document notes; collection metadata is always preserved."""
    from documents.models import Note

    try:
        notes = []
        if checks.date_review:
            notes.append(checks.date_review)
        if checks.suspicious_pages:
            pages = ", ".join(
                str(page + page_offset) for page in checks.suspicious_pages
            )
            notes.append(
                f"OCR review: check numbers on page(s) {pages}. Formatted OCR includes a number found only in another page's plain OCR. This is an advisory; the scan and OCR text were retained.",
            )
        with transaction.atomic():
            for note in notes:
                Note.objects.get_or_create(document=document, note=note)
    except Exception:
        logger.warning("Optional ingestion review notes could not be saved")
