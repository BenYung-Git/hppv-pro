"""Shared list field display rules."""

from __future__ import annotations

from hppv.display_format import (
    EMPTY_DISPLAY,
    format_date_display,
    format_field_display,
)
from hppv.web_utils import format_race_date_display, race_date_sort_key


def test_format_date_display_iso_and_slash_variants() -> None:
    assert format_date_display("2026-11-08") == "2026-11-08"
    assert format_date_display("2026/11/08") == "2026-11-08"
    assert format_date_display("01/02/2026") == "2026-02-01"
    assert format_date_display("1/2/26") == "2026-02-01"
    assert format_date_display("") == EMPTY_DISPLAY
    assert format_date_display(None) == EMPTY_DISPLAY
    assert format_date_display("—") == EMPTY_DISPLAY


def test_format_field_display_routes_date_keys() -> None:
    assert format_field_display("race_date", "2026/11/08") == "2026-11-08"
    assert format_field_display("match_date", "01/02/2026") == "2026-02-01"
    assert format_field_display("horse_name", "測試馬") == "測試馬"
    assert format_field_display("horse_name", "") == EMPTY_DISPLAY


def test_format_race_date_display_uses_iso_rule() -> None:
    assert format_race_date_display("08/03/26") == "2026-03-08"
    assert format_race_date_display("2026/03/08") == "2026-03-08"
    assert race_date_sort_key("2026-03-08") == 20260308
    assert race_date_sort_key("08/03/2026") == 20260308
