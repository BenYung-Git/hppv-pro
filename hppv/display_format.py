"""Shared list-field display rules (four records lists + other date UIs).

Storage formats stay domain-specific; only presentation is normalized here.
Date columns always display as YYYY-MM-DD.
"""

from __future__ import annotations

import re
from typing import Any

EMPTY_DISPLAY = "—"

_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_YMD_SLASH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_DATE_DMY4 = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DATE_DMY2 = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")

# Layout / API keys that use the date display rule.
DATE_DISPLAY_KEYS = frozenset({"match_date", "race_date"})


def format_text_field(value: Any) -> str:
    if value is None:
        return EMPTY_DISPLAY
    text = str(value).strip()
    return text if text else EMPTY_DISPLAY


def format_date_display(value: Any) -> str:
    """Normalize date strings to YYYY-MM-DD. Empty → —; unknown shape kept as-is."""
    if value is None:
        return EMPTY_DISPLAY
    text = str(value).strip()
    if not text or text == EMPTY_DISPLAY:
        return EMPTY_DISPLAY
    if _DATE_ISO.fullmatch(text):
        return text
    m = _DATE_YMD_SLASH.fullmatch(text)
    if m:
        yyyy, mm, dd = m.groups()
        return f"{yyyy}-{mm}-{dd}"
    m = _DATE_DMY4.fullmatch(text)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
    m = _DATE_DMY2.fullmatch(text)
    if m:
        dd, mm, yy = m.groups()
        yy_num = int(yy)
        yyyy = 2000 + yy_num if yy_num <= 69 else 1900 + yy_num
        return f"{yyyy:04d}-{int(mm):02d}-{int(dd):02d}"
    return text


def format_field_display(key: str, value: Any) -> str:
    """Apply per-key display rule; unknown keys use plain text/empty rule."""
    if key in DATE_DISPLAY_KEYS:
        return format_date_display(value)
    return format_text_field(value)
