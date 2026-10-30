# ruff: noqa: RUF001 - fullwidth OCR characters are deliberate test inputs.
from datetime import date
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from django.db import DatabaseError

from documents.ingestion_checks import IngestionChecks
from documents.ingestion_checks import apply_ingestion_checks
from documents.ingestion_checks import collect_ingestion_checks
from documents.ingestion_checks import suspicious_number_pages
from documents.models import Note
from documents.printed_dates import printed_dates
from paperless_testing.factories import DocumentFactory


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("発行日: 令和六年六月二十八日", date(2024, 6, 28)),
        ("発行日: 令和元年5月1日", date(2019, 5, 1)),
        ("発行日: 令和元年4月30日", None),
        ("発行日: 平成31年4月30日", date(2019, 4, 30)),
        ("発行日: 平成31年5月1日", None),
        ("発行日: 平成元年1月7日", None),
        ("発行日: 昭和64年1月7日", date(1989, 1, 7)),
        ("発行日: 昭和64年1月8日", None),
        ("発行日: 大正元年7月30日", date(1912, 7, 30)),
        ("発行日: 大正元年7月29日", None),
        ("発行日: 明治45年7月29日", date(1912, 7, 29)),
        ("発行日: 明治45年7月30日", None),
        ("発行日: 明治元年1月1日", None),
        ("発行日: 令和十十年6月28日", None),
        ("発行日: R0.6.28", None),
        ("発行日: Ｒ６．６．２８", date(2024, 6, 28)),
        ("発行日: 二〇二四年六月二十八日", date(2024, 6, 28)),
        ("Issued: ２０２４／６／２８", date(2024, 6, 28)),
        ("Issued: 2024-02-30", None),
        ("Issued: 09/12/2024", None),
        ("Issued: 28/6/2024", date(2024, 6, 28)),
        ("Issued: 6/28/2024", date(2024, 6, 28)),
        ("Issued: 28.06.2024", date(2024, 6, 28)),
        ("Issued: June 28, 2024", date(2024, 6, 28)),
        ("Issued: 28 Jun 2024", date(2024, 6, 28)),
    ],
)
def test_printed_dates(text, expected):
    candidates = printed_dates(text)
    assert len(candidates) == 1
    assert candidates[0].value == expected
    assert candidates[0].kind == "issue"


@pytest.fixture()
def enabled(settings):
    settings.OCR_VALIDATE_PRINTED_DATES = True
    settings.OCR_REVIEW_CROSS_PAGE_NUMBERS = True


def test_only_printed_issue_date_selected(enabled):
    checks = collect_ingestion_checks(
        None,
        "生年月日: 平成5年1月2日\n発行日: 令和六年六月二十八日\n納付期限: 2024-07-31",
    )
    assert checks.printed_date == date(2024, 6, 28)
    assert checks.date_review is None


@pytest.mark.parametrize(
    "text",
    [
        "2024-06-28",
        "Due date: 2024-06-28",
        "生年月日: 1993年1月2日",
        "Issued: 2024-06-28\nIssued: 2024-06-29",
        "Issued: 2099-06-28",
        "Issued: 1899-06-28",
        "Issued: 2024-02-30",
        "Issued: 2024-06-28 / 2024-06-29",
    ],
)
def test_uncertain_dates_are_advisory(enabled, text):
    checks = collect_ingestion_checks(None, text)
    assert checks.printed_date is None
    assert checks.date_review


def test_same_line_labels_and_duplicate_dates(enabled):
    checks = collect_ingestion_checks(
        None,
        "Issued: 2024-06-28 Due: 2024-07-31\n発行日: 令和6年6月28日",
    )
    assert checks.printed_date == date(2024, 6, 28)


def test_label_does_not_jump_over_another_date_format():
    dates = printed_dates("Issued: 2024-06-28 令和6年6月29日")
    assert [item.kind for item in dates] == ["issue", None]


def test_checks_opt_in(settings):
    settings.OCR_VALIDATE_PRINTED_DATES = False
    settings.OCR_REVIEW_CROSS_PAGE_NUMBERS = False
    parser = Mock()
    assert collect_ingestion_checks(parser, "Issued: 2024-06-28") == IngestionChecks()
    parser.get_review_pages.assert_not_called()


def test_cross_page_numbers():
    pages = [
        {
            "plain_text": "Total １２，３４５",
            "markdown": "Total 12,345 Reference 777888",
        },
        {"plain_text": "Reference 777888", "markdown": "Reference 777888"},
    ]
    assert suspicious_number_pages(pages) == (1,)
    pages[0]["markdown"] = "Total 12,345"
    assert suspicious_number_pages(pages) == ()


def test_no_review_without_plain_evidence_or_other_page_support():
    assert (
        suspicious_number_pages([{"markdown": "777888"}, {"plain_text": "777888"}])
        == ()
    )
    assert (
        suspicious_number_pages([{"plain_text": "123456", "markdown": "999999"}]) == ()
    )
    assert suspicious_number_pages([]) == ()


def test_checks_fail_soft_without_logging_text(enabled, caplog):
    parser = Mock()
    parser.get_review_pages.side_effect = RuntimeError("private OCR content")
    assert collect_ingestion_checks(parser, "Issued: 2024-06-28") == IngestionChecks()
    assert "private OCR" not in caplog.text


@pytest.mark.django_db
def test_notes_preserve_metadata_and_use_page_offsets(
    django_capture_on_commit_callbacks,
):
    document = DocumentFactory(title="Water Supply Notices", created=date(2020, 1, 2))
    checks = IngestionChecks(
        date(2024, 6, 28),
        "Printed-date review: check date",
        (1, 3),
    )
    with django_capture_on_commit_callbacks(execute=True):
        apply_ingestion_checks(document, checks, page_offset=4, preserve_metadata=True)
        apply_ingestion_checks(document, checks, page_offset=4, preserve_metadata=True)
    document.refresh_from_db()
    assert document.title == "Water Supply Notices"
    assert document.created == date(2020, 1, 2)
    assert document.notes.count() == 2
    assert list(document.tags.values_list("name", flat=True)) == ["ocr-review"]
    assert "5, 7" in document.notes.last().note


@pytest.mark.django_db
def test_note_failure_does_not_break_outer_transaction():
    document = DocumentFactory()
    with patch.object(
        Note.objects,
        "get_or_create",
        side_effect=DatabaseError("private detail"),
    ):
        apply_ingestion_checks(document, IngestionChecks(date_review="review"))
    assert document.notes.count() == 0
    document.title = "Still usable"
    document.save()
