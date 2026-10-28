"""Read dates exactly supported by printed OCR text, without guessing locale."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

DIGITS = {
    char: value
    for value, chars in enumerate(
        ("〇零", "一", "二", "三", "四", "五", "六", "七", "八", "九"),
    )
    for char in chars
}
ERAS = {
    "令和": (2019, date(2019, 5, 1), None, "令R"),
    "平成": (1989, date(1989, 1, 8), date(2019, 4, 30), "平H"),
    "昭和": (1926, date(1926, 12, 25), date(1989, 1, 7), "昭S"),
    "大正": (1912, date(1912, 7, 30), date(1926, 12, 24), "大T"),
    # Earlier Meiji dates use the Japanese lunisolar calendar, not this conversion.
    "明治": (1868, date(1873, 1, 1), date(1912, 7, 29), "明M"),
}
MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        1,
    )
    for name in names
}
NUM = r"(?:\d{1,4}|[〇零一二三四五六七八九十百千]+)"
ERA = r"(?:令和|平成|昭和|大正|明治|[令平昭大明]|(?<![A-Za-z])[RHSTM](?=\s*\d))"
PATTERNS = (
    ("era", rf"({ERA})\s*(元|{NUM})\s*[年.\-/]\s*({NUM})\s*[月.\-/]\s*({NUM})\s*日?"),
    ("ymd", rf"({NUM})\s*年\s*({NUM})\s*月\s*({NUM})\s*日"),
    ("ymd", r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)"),
    ("dmy", r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)"),
    ("slash", r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)"),
    (
        "d_month_y",
        r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)",  # codespell:ignore nd
    ),
    (
        "month_d_y",
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)",  # codespell:ignore nd
    ),
)
LABEL = re.compile(
    r"(?P<issue>\b(?:issue date|date of issue|date issued|issued|invoice date|document date|created on)\b|発行年月日|発行日|作成日|通知日|交付年月日|交付日|請求日)"
    r"|(?P<other>\b(?:due(?: date)?|payment date|pay(?:ment)? by|birth(?: date)?|date of birth|dob|expiry|expiration|valid until|period)\b|生年月日|誕生日|支払期限|納期限|納付期限|有効期限|対象期間)",  # codespell:ignore ment
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PrintedDate:
    value: date | None
    start: int
    end: int
    kind: str | None


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _number(text: str) -> int:
    if text.isdecimal():
        return int(text)
    if text == "元":
        return 1
    if not any(char in text for char in "十百千"):
        return int("".join(str(DIGITS[char]) for char in text))
    if not re.fullmatch(
        r"(?:[一二三四五六七八九]?千)?(?:[一二三四五六七八九]?百)?(?:[一二三四五六七八九]?十)?[〇零一二三四五六七八九]?",
        text,
    ):
        raise ValueError("Invalid kanji number")
    total = current = 0
    for char in text:
        if char in DIGITS:
            current = DIGITS[char]
        else:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
    return total + current


def _date(kind: str, groups: tuple[str, ...]) -> date | None:
    if kind == "era":
        era, year, month, day = groups
        base, start, end, _ = next(
            value for name, value in ERAS.items() if era == name or era in value[3]
        )
        number = _number(year)
        if number < 1:
            return None
        value = date(base + number - 1, _number(month), _number(day))
        return value if value >= start and (end is None or value <= end) else None
    if kind == "ymd":
        return date(*(_number(group) for group in groups))
    if kind == "dmy":
        day, month, year = map(int, groups)
        return date(year, month, day)
    if kind == "slash":
        first, second, year = map(int, groups)
        if first > 12 >= second:
            return date(year, second, first)
        if second > 12 >= first:
            return date(year, first, second)
        return None
    if kind == "d_month_y":
        day, month, year = groups
    else:
        month, day, year = groups
    return date(int(year), MONTHS[month.lower()], int(day))


def printed_dates(text: str) -> list[PrintedDate]:
    """Include invalid/ambiguous spans so a review does not mistake them for absent dates."""
    text = normalise(text)
    found = []
    occupied = []
    for kind, pattern in PATTERNS:
        for match in re.finditer(pattern, text):
            if any(
                start < match.end() and match.start() < end for start, end in occupied
            ):
                continue
            occupied.append(match.span())
            try:
                value = _date(kind, match.groups())
            except (ValueError, KeyError, StopIteration):
                value = None
            found.append(PrintedDate(value, match.start(), match.end(), None))
    result = []
    previous = 0
    # Resolve labels after ordering all date formats, including overlapping era dates.
    for item in sorted(found, key=lambda item: item.start):
        line_start = text.rfind("\n", 0, item.start) + 1
        labels = list(
            LABEL.finditer(
                text[max(line_start, previous, item.start - 64) : item.start],
            ),
        )
        label = labels[-1].lastgroup if labels else None
        result.append(PrintedDate(item.value, item.start, item.end, label))
        previous = item.end
    return result
