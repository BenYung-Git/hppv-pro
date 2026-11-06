from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_layout import default_layout_settings
from hppv.soccer_matches import (
    STATUS_FILTER_MAPPING_CONNECTED,
    SoccerDataError,
    fetch_matches_by_date,
    fetch_matches_filtered,
    format_kickoff_hhmm,
    format_odds,
    format_score_pair,
    hong_kong_today,
    normalize_record_index,
    normalize_status_filter,
    parse_requested_date,
    search_keys_from_layout,
    shift_date,
    sort_specs_from_layout,
)


def _insert_match(
    db_path: Path,
    *,
    match_id: str,
    match_date: str,
    kickoff_at: str,
    match_code: str = "001",
    competition_code: str = "ACL2",
    competition_name_zh: str = "亞冠2",
    home_team: str = "主隊甲",
    away_team: str = "客隊乙",
    home_odds: float | None = 1.85,
    draw_odds: float | None = None,
    away_odds: float | None = 3.40,
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
                match_code,
                match_date,
                kickoff_at,
                competition_code,
                competition_name_zh,
                home_team,
                away_team,
                home_odds,
                draw_odds,
                away_odds,
                "2026-11-04T00:00:00+00:00",
                "2026-11-04T00:00:00+00:00",
            ),
        )


def test_parse_date_and_shift() -> None:
    d, invalid = parse_requested_date(None)
    assert invalid is False
    assert d == hong_kong_today()

    d2, invalid2 = parse_requested_date("2026-11-04")
    assert invalid2 is False
    assert d2 == date(2026, 11, 4)
    assert shift_date(d2, -1) == date(2026, 11, 3)
    assert shift_date(date(2026, 12, 31), 1) == date(2027, 1, 1)
    assert shift_date(date(2026, 3, 1), -1) == date(2026, 2, 28)

    d3, invalid3 = parse_requested_date("2026-13-40")
    assert invalid3 is True
    assert d3 is None

    d4, invalid4 = parse_requested_date("not-a-date")
    assert invalid4 is True


def test_format_kickoff_and_odds() -> None:
    assert format_kickoff_hhmm("2026-11-04T20:00:00+08:00") == "20:00"
    assert format_kickoff_hhmm("2026-11-04T12:00:00+00:00") == "20:00"
    assert format_odds(None) == "—"
    assert format_odds(1.856) == "1.86"
    assert format_odds(0) == "—"
    assert format_odds(-1.2) == "—"
    assert format_odds("abc") == "—"


def test_format_score_pair_non_negative_int() -> None:
    assert format_score_pair(None, None) == "—"
    assert format_score_pair(0, 0) == "0 : 0"
    assert format_score_pair(1, 2) == "1 : 2"
    assert format_score_pair("3", "0") == "3 : 0"
    assert format_score_pair(1, None) == "—"
    assert format_score_pair(None, 1) == "—"
    assert format_score_pair(-1, 0) == "—"
    assert format_score_pair(0, -2) == "—"
    assert format_score_pair(1.5, 0) == "—"
    assert format_score_pair(0, 2.0) == "0 : 2"
    assert format_score_pair("1.5", "0") == "—"
    assert format_score_pair("abc", "1") == "—"
    assert format_score_pair(True, 1) == "—"


def test_fetch_by_date_order_and_readonly(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    _insert_match(
        db_path,
        match_id="M2",
        match_date="2026-11-04",
        kickoff_at="2026-11-04T21:00:00+08:00",
        home_team="第二場",
    )
    _insert_match(
        db_path,
        match_id="M1",
        match_date="2026-11-04",
        kickoff_at="2026-11-04T20:00:00+08:00",
        home_team="第一場",
    )
    _insert_match(
        db_path,
        match_id="M0",
        match_date="2026-11-04",
        kickoff_at="2026-11-04T20:00:00+08:00",
        home_team="同時間較早 id",
    )
    _insert_match(
        db_path,
        match_id="OTHER",
        match_date="2026-11-05",
        kickoff_at="2026-11-05T20:00:00+08:00",
    )

    rows = fetch_matches_by_date(db_path, date(2026, 11, 4))
    assert [r["match_id"] for r in rows] == ["M0", "M1", "M2"]
    assert rows[1]["kickoff_hhmm"] == "20:00"
    assert rows[1]["competition_code"] == "ACL2"
    assert rows[1]["competition_name_zh"] == "亞冠2"
    assert rows[1]["draw_odds"] == "—"
    assert rows[1]["home_odds"] == "1.85"

    with get_connection(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) AS c FROM soccer_daily").fetchone()["c"]
    fetch_matches_by_date(db_path, date(2026, 11, 4))
    with get_connection(db_path) as conn:
        after = conn.execute("SELECT COUNT(*) AS c FROM soccer_daily").fetchone()["c"]
    assert before == after == 4

    empty = fetch_matches_by_date(db_path, date(2026, 1, 1))
    assert empty == []


def test_fetch_missing_db_is_error(tmp_path: Path) -> None:
    with pytest.raises(SoccerDataError):
        fetch_matches_by_date(tmp_path / "missing.sqlite3", date(2026, 11, 4))


def test_fetch_null_scores_and_extra_column_do_not_break_list(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    _insert_match(
        db_path,
        match_id="S1",
        match_date="2026-11-05",
        kickoff_at="2026-11-05T20:00:00+08:00",
        home_team="主隊可搜尋",
        away_team="客隊",
    )
    with get_connection(db_path) as conn:
        conn.execute('ALTER TABLE soccer_daily ADD COLUMN "legacy_extra_flag" TEXT;')
        conn.execute(
            "UPDATE soccer_daily SET legacy_extra_flag='x', "
            "score_ht_home=NULL, score_ht_away=NULL, score_ft_home=NULL, score_ft_away=NULL "
            "WHERE match_id='S1'"
        )
        conn.commit()

    rows = fetch_matches_by_date(db_path, date(2026, 11, 5))
    assert len(rows) == 1
    assert rows[0]["match_id"] == "S1"
    assert rows[0]["score_ht"] == "—"
    assert rows[0]["score_ft"] == "—"
    assert rows[0]["score_pen"] == "—"
    assert rows[0]["result_status"] == "—"
    assert "ui_et" not in rows[0]

    empty = fetch_matches_by_date(db_path, date(2026, 11, 6))
    assert empty == []


def test_api_daily_matches_returns_full_day_and_n_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    _insert_match(
        db_path,
        match_id="API1",
        match_date="2026-11-05",
        kickoff_at="2026-11-05T18:00:00+08:00",
    )
    from hppv import web_app as wa

    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db_path)
    client = wa.app.test_client()
    resp = client.get("/api/soccer/daily-matches?date=2026-11-05")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["date"] == "2026-11-05"
    assert len(body["matches"]) == 1
    assert body["matches"][0]["match_id"] == "API1"
    assert body["n"] == 1
    assert body["total_count"] == 1
    assert "page_size" not in body
    assert "total_pages" not in body
    assert body["status_filter_mapping"]["connected"] is False

    page = client.get("/soccer/daily?date=2026-11-05")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "賽事資料讀取失敗" not in html
    assert 'id="matches-pager"' in html
    assert "1 / 1" in html


def test_normalize_record_index_and_status() -> None:
    assert normalize_record_index(None, total_count=0) == 1
    assert normalize_record_index("abc", total_count=5) == 1
    assert normalize_record_index("0", total_count=5) == 1
    assert normalize_record_index("99", total_count=3) == 3
    assert normalize_record_index("2", total_count=3) == 2
    assert normalize_status_filter(None) == "prematch"
    assert normalize_status_filter("results") == "results"
    assert STATUS_FILTER_MAPPING_CONNECTED is False


def test_filtered_list_returns_all_no_page_cap(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    d = date(2026, 11, 5)
    empty = fetch_matches_filtered(db, d, n=1)
    assert empty.total_count == 0 and empty.matches == []

    for i in range(26):
        _insert_match(
            db,
            match_id=f"M{i:02d}",
            match_date="2026-11-05",
            kickoff_at=f"2026-11-05T{10 + i // 60:02d}:{i % 60:02d}:00+08:00",
        )
    full = fetch_matches_filtered(db, d, n=1)
    assert full.total_count == 26
    assert len(full.matches) == 26  # no 20/page truncation
    assert full.n == 1
    assert fetch_matches_filtered(db, d, n=99).n == 26
    assert fetch_matches_filtered(db, d, n=0).n == 1


def test_search_filter_reduces_total(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    for i in range(25):
        _insert_match(
            db,
            match_id=f"Q{i:02d}",
            match_date="2026-11-05",
            kickoff_at=f"2026-11-05T12:{i:02d}:00+08:00",
            home_team="普通隊",
        )
    _insert_match(
        db,
        match_id="HIT",
        match_date="2026-11-05",
        kickoff_at="2026-11-05T15:00:00+08:00",
        home_team="可搜尋特別隊",
    )
    layout = default_layout_settings()
    hit = fetch_matches_filtered(
        db,
        date(2026, 11, 5),
        n=1,
        q="特別隊",
        status="prematch",
        search_keys=search_keys_from_layout(layout),
        sort_specs=sort_specs_from_layout(layout),
    )
    assert hit.total_count == 1
    assert hit.matches[0]["match_id"] == "HIT"
    assert hit.status_filter_applied is False

    same = fetch_matches_filtered(
        db,
        date(2026, 11, 5),
        n=1,
        status="results",
        search_keys=search_keys_from_layout(layout),
    )
    assert same.total_count == 26
    assert same.status == "results"
    assert same.status_filter_applied is False


def test_api_n_over_total_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    for i in range(25):
        _insert_match(
            db,
            match_id=f"P{i:02d}",
            match_date="2026-11-05",
            kickoff_at=f"2026-11-05T{8 + i // 60:02d}:{i % 60:02d}:00+08:00",
            home_team=f"主{i}",
        )
    from hppv import web_app as wa

    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)
    client = wa.app.test_client()

    resp = client.get("/api/soccer/daily-matches?date=2026-11-05&n=5&status=prematch")
    body = resp.get_json()
    assert body["n"] == 5
    assert body["total_count"] == 25
    assert len(body["matches"]) == 25  # full list
    assert body["status"] == "prematch"

    # legacy page= accepted as n
    legacy = client.get("/api/soccer/daily-matches?date=2026-11-05&page=3")
    assert legacy.get_json()["n"] == 3
    assert legacy.get_json()["total_count"] == 25

    html = client.get("/soccer/daily?date=2026-11-05&n=5").get_data(as_text=True)
    assert 'id="matches-pager"' in html
    assert "5 / 25" in html
