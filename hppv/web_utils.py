from __future__ import annotations

import re

from hppv.display_format import format_date_display


def format_race_date_display(date_text: str) -> str:
    """Display race date as YYYY-MM-DD (shared list date rule)."""
    return format_date_display(date_text)


def race_date_sort_key(date_text: str) -> int:
    """Return sortable yyyymmdd key for race date text."""
    t = (date_text or "").strip()
    m4 = re.match(r"^(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})$", t)
    if m4:
        dd, mm, yyyy = m4.groups()
        return int(f"{int(yyyy):04d}{int(mm):02d}{int(dd):02d}")
    m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2})$", t)
    if m2:
        dd, mm, yy = m2.groups()
        yy_num = int(yy)
        yyyy = 2000 + yy_num if yy_num <= 69 else 1900 + yy_num
        return int(f"{yyyy:04d}{int(mm):02d}{int(dd):02d}")
    m_iso = re.match(r"^((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})$", t)
    if m_iso:
        yyyy, mm, dd = m_iso.groups()
        return int(f"{int(yyyy):04d}{int(mm):02d}{int(dd):02d}")
    return 0
