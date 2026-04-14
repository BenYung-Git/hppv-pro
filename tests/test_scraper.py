from __future__ import annotations

from hppv.scraper import parse_past_performance_from_html


def test_parse_empty_returns_no_records() -> None:
    html = "<html><body><h1>No tables</h1></body></html>"
    res = parse_past_performance_from_html("H1", html, "https://example.invalid/")
    assert res.horse_id == "H1"
    assert res.records == []

