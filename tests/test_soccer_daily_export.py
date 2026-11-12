"""Daily-matches export: soccer_daily only, shared format builders."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hppv.soccer_daily_export import (
    build_daily_export,
    default_daily_export_basename,
    fetch_daily_export_rows,
    parse_daily_export_request,
)
from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_layout import COLUMN_CATALOG, default_layout_settings
from hppv.soccer_matches import fetch_matches_filtered


def _insert_match(
    db_path: Path,
    *,
    match_id: str,
    match_date: str,
    kickoff_at: str,
    home_team: str = "主隊甲",
    away_team: str = "客隊乙",
    competition_name_zh: str = "亞冠聯賽",
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO soccer_daily (
                match_id, match_code, match_date, kickoff_at,
                competition_code, competition_name_zh,
                home_team, away_team,
                home_odds, draw_odds, away_odds,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                match_id[-3:],
                match_date,
                kickoff_at,
                "ACL",
                competition_name_zh,
                home_team,
                away_team,
                1.9,
                3.2,
                3.5,
                "2026-11-10T00:00:00+00:00",
                "2026-11-10T00:00:00+00:00",
            ),
        )


def _init_db(tmp_path: Path) -> Path:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    for i in range(5):
        _insert_match(
            db,
            match_id=f"d{i}",
            match_date="2026-11-10",
            kickoff_at=f"2026-11-10T1{i}:00:00+08:00",
            home_team=f"主隊{i}",
            away_team=f"客隊{i}",
        )
    _insert_match(
        db,
        match_id="other",
        match_date="2026-11-11",
        kickoff_at="2026-11-11T12:00:00+08:00",
        home_team="別日主隊",
        away_team="別日客隊",
    )
    return db


def test_default_daily_basename() -> None:
    assert default_daily_export_basename(q="", match_date=date(2026, 11, 10)) == (
        "DailyMatches-20261110"
    )
    assert default_daily_export_basename(q="阿仙奴", match_date=date(2026, 11, 10)) == (
        "阿仙奴-DailyMatches-20261110"
    )


def test_daily_export_uses_soccer_daily_filters(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    catalog_keys = [c["key"] for c in COLUMN_CATALOG]
    req = parse_daily_export_request(
        {
            "file_type": "csv",
            "q": "主隊1",
            "status": "prematch",
            "all_matches": False,
            "date": "2026-11-10",
            "sort": "kickoff_hhmm:asc",
            "columns": [
                {"key": "home_team", "width": 120},
                {"key": "away_team", "width": 120},
            ],
            "lang": "zh-Hant",
        },
        catalog_keys=catalog_keys,
        db_path=db,
    )
    listed = fetch_matches_filtered(
        db,
        date(2026, 11, 10),
        q="主隊1",
        status="prematch",
        sort_specs=[("kickoff_hhmm", "asc")],
        search_keys=["home_team", "away_team", "competition_name_zh"],
    )
    rows = fetch_daily_export_rows(db, req)
    assert len(rows) == listed.total_count == 1
    assert rows[0]["home_team"] == "主隊1"

    result = build_daily_export(db, req)
    assert result.file_type == "csv"
    assert result.row_count == 1
    text = result.content.decode("utf-8-sig")
    assert "主隊1" in text
    assert "別日主隊" not in text


def test_daily_export_all_matching_rows_not_cursor(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    catalog_keys = [c["key"] for c in COLUMN_CATALOG]
    layout = default_layout_settings("daily_matches")
    req = parse_daily_export_request(
        {
            "file_type": "xlsx",
            "q": "",
            "status": "prematch",
            "all_matches": False,
            "date": "2026-11-10",
            "sort": "kickoff_hhmm:desc",
            "columns": [{"key": "home_team", "width": 100}],
            "lang": "en",
        },
        catalog_keys=catalog_keys,
        db_path=db,
    )
    listed = fetch_matches_filtered(
        db,
        date(2026, 11, 10),
        status="prematch",
        sort_specs=[("kickoff_hhmm", "desc")],
        search_keys=["home_team"],
    )
    result = build_daily_export(db, req, layout_settings=layout)
    assert result.row_count == listed.total_count == 5
    assert result.mime.startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_daily_export_jpeg_pdf_smoke(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    catalog_keys = [c["key"] for c in COLUMN_CATALOG]
    cols = [
        {"key": "match_date", "width": 100},
        {"key": "home_team", "width": 120},
        {"key": "away_team", "width": 120},
    ]
    for ftype in ("jpeg", "pdf"):
        req = parse_daily_export_request(
            {
                "file_type": ftype,
                "q": "",
                "status": "prematch",
                "all_matches": False,
                "date": "2026-11-10",
                "sort": "",
                "columns": cols,
                "lang": "zh-Hans",
            },
            catalog_keys=catalog_keys,
            db_path=db,
        )
        result = build_daily_export(db, req)
        assert result.file_type == ftype
        assert result.row_count == 5
        assert len(result.content) > 100
