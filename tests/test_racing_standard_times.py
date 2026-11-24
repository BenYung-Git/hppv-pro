"""Standard times PDF parse / upsert / list — no network in unit path except optional live."""

from __future__ import annotations

from pathlib import Path

import pytest

from hppv.racing_standard_times import (
    STANDARD_TIMES_PDF_URLS,
    StandardTimesError,
    StandardTimesRow,
    ensure_standard_times_table,
    fetch_standard_times_filtered,
    parse_standard_times_pdf_bytes,
    resolve_download_years,
    upsert_standard_times_rows,
)
from hppv.soccer_db import init_soccer_db
from hppv.translations import load_active_text_map, translate


def test_resolve_years_defaults_to_registered() -> None:
    years = resolve_download_years(None)
    assert years == sorted(STANDARD_TIMES_PDF_URLS.keys())
    assert resolve_download_years([]) == years


def test_resolve_years_rejects_unknown() -> None:
    with pytest.raises(StandardTimesError) as ei:
        resolve_download_years([2099])
    assert ei.value.code == "year_url_unknown"


def test_upsert_and_list(tmp_path: Path) -> None:
    rows = [
        StandardTimesRow(
            published_on="2025-08-26",
            source_year=2025,
            venue_code="ST",
            track_type="TURF",
            distance_m=1000,
            class_band="Group",
            standard_time="0.55.90",
            sectionals=("", "", "", "13.05", "20.60", "22.25"),
        ),
        StandardTimesRow(
            published_on="2025-08-26",
            source_year=2025,
            venue_code="ST",
            track_type="TURF",
            distance_m=1000,
            class_band="Class 1",
            standard_time="",
            sectionals=("", "", "", "", "", ""),
        ),
    ]
    db = tmp_path / "std.sqlite3"
    ensure_standard_times_table(db)
    counts = upsert_standard_times_rows(db, rows, overwrite=False)
    assert counts["inserted"] == 2
    counts2 = upsert_standard_times_rows(db, rows, overwrite=False)
    assert counts2["skipped"] == 2
    counts3 = upsert_standard_times_rows(
        db,
        [
            StandardTimesRow(
                published_on="2025-08-26",
                source_year=2025,
                venue_code="ST",
                track_type="TURF",
                distance_m=1000,
                class_band="Group",
                standard_time="0.55.91",
                sectionals=("1", "2", "3", "4", "5", "6"),
            )
        ],
        overwrite=True,
    )
    assert counts3["updated"] == 1
    result = fetch_standard_times_filtered(db, all_matches=True, n=1)
    assert result.total_count == 2
    group = next(r for r in result.rows if r["class_band"] == "Group")
    assert group["standard_time"] == "0.55.91"
    assert group["sec_1"] == "1"


def test_standard_times_translation_keys(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    assert (
        translate(load_active_text_map(db, "zh-Hant"), "racing.info.tab.standard_times")
        == "標準時間"
    )
    assert (
        translate(load_active_text_map(db, "en"), "racing.download.type.standard_times")
        == "Standard times"
    )
    assert (
        translate(load_active_text_map(db, "en"), "soccer.daily.task.unit.year")
        == "year"
    )


@pytest.mark.parametrize("year", [2024, 2025])
def test_live_pdf_parse_smoke(year: int) -> None:
    """Optional live smoke: skip if network/PDF unavailable."""
    import requests

    from hppv.racing_standard_times import fetch_standard_times_pdf

    try:
        pdf = fetch_standard_times_pdf(year, timeout=30)
    except (StandardTimesError, requests.RequestException) as e:
        pytest.skip(f"live PDF unavailable: {e}")
    published_on, rows = parse_standard_times_pdf_bytes(pdf, source_year=year)
    assert published_on.startswith(str(year)[:2]) or published_on.startswith("20")
    assert len(rows) >= 50
    assert any(r.venue_code == "ST" for r in rows)
    assert any(r.sectionals for r in rows)
