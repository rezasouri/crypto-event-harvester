"""Parse explicit coarse calendar labels without inventing an exact day.

The canonical period keys are YYYY-MM and YYYY-QN. This is metadata, *not*
a timestamp or the first date of the named month / quarter.
"""
from __future__ import annotations

import re

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_LABEL = re.compile(
    r"^(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+(?P<year>\d{4})(?:\s+UTC)?$",
    re.I,
)
QUARTER_LABEL = re.compile(
    r"^(?:from\s+)?Q(?P<quarter>[1-4])(?:,\s*|\s+)(?P<year>\d{4})"
    r"(?:\s+UTC)?$",
    re.I,
)


def coarse_period(label: str) -> tuple[str, str | None]:
    """Return ('month'|'quarter'|'unknown', period key or None).

    Only the entire explicit calendar label can establish precision.
    Never infer a day from a sentence containing a month name.
    """
    clean = " ".join(label.split())
    month = MONTH_LABEL.fullmatch(clean)
    if month:
        year = int(month["year"])
        if 1 <= year <= 9999:
            number = MONTHS[month["month"][:3].lower()]
            return "month", f"{year:04d}-{number:02d}"
    quarter = QUARTER_LABEL.fullmatch(clean)
    if quarter:
        year = int(quarter["year"])
        if 1 <= year <= 9999:
            return "quarter", f"{year:04d}-Q{quarter['quarter']}"
    return "unknown", None
