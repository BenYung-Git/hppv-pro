from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE, footylogic_schema_sql
from hppv.soccer_history import (
    HISTORY_PAGE_SIZE,
    fetch_history_filtered,
    format_name_or_id,
    parse_history_sort_param,
)
from hppv.soccer_matches import EMPTY_DISPLAY, SoccerDataError


def _init_history_db(tmp_path: Path) -> Path:
    db = tmp_path / "soccer.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(footylogic_schema_sql())
    # soccer_daily absent on purpose — history must not require it.
    rows = [
        # source_key, comp_id, season, month, comp_name, home_id, away_id, home_name, away_name,
        # kickoff_utc, kickoff_hkt, match_date_hkt, ft_h, ft_a, ht_h, ht_a, has_score
        (
            "fl|c1|s1|h1|a1|2026-11-05T12:00:00.000Z",
            "c1",
            "s1",
            "2026-11",
            "英超",
            "h1",
            "a1",
            "主隊甲",
            "客隊乙",
            "2026-11-05T12:00:00.000Z",
            "2026-11-05T20:00:00+08:00",
            "2026-11-05",
            2,
            1,
            1,
            0,
            1,
        ),
        (
            "fl|c1|s1|h2|a2|2026-11-05T10:00:00.000Z",
            "c1",
            "s1",
            "2026-11",
            "英超",
            "h2",
            "a2",
            "主隊丙",
            "客隊丁",
            "2026-11-05T10:00:00.000Z",
            "2026-11-05T18:00:00+08:00",
            "2026-11-05",
            None,
            None,
            None,
            None,
            0,
        ),
        (
            "fl|c2|s1|h3|a3|2026-11-04T12:00:00.000Z",
            "c2",
            "s1",
            "2026-11",
            None,  # name missing → show competition_id
            "h3",
            "a3",
            None,  # team name missing → show id
            "客隊戊",
            "2026-11-04T12:00:00.000Z",
            "2026-11-04T20:00:00+08:00",
            "2026-11-04",
            0,
            0,
            0,
            0,
            1,
        ),
    ]
    for r in rows:
        conn.execute(
            f"""
            INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
              source_key, source, competition_id, season_id, month, competition_name,
              home_team_id, away_team_id, home_team_name, away_team_name,
              kickoff_utc, kickoff_hkt, match_date_hkt,
              score_ft_home, score_ft_away, score_ht_home, score_ht_away,
              has_score, score_complete, result_confirm_status, event_json,
              source_last_updated, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r[0],
                "footylogic",
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                r[6],
                r[7],
                r[8],
                r[9],
                r[10],
                r[11],
                r[12],
                r[13],
                r[14],
                r[15],
                r[16],
                1 if r[16] else 0,
                "source_score_unconfirmed",
                "{}",
                None,
                "2026-11-06T00:00:00Z",
                "2026-11-06T00:00:00Z",
            ),
        )
    conn.commit()
    conn.close()
    return db


def test_format_name_or_id_prefers_name() -> None:
    assert format_name_or_id("英超", "c1") == "英超"
    assert format_name_or_id("", "c1") == "c1"
    assert format_name_or_id(None, "c1") == "c1"
    assert format_name_or_id(None, None) == EMPTY_DISPLAY


def test_history_default_sort_newest_first(tmp_path: Path) -> None:
    db = _init_history_db(tmp_path)
    result = fetch_history_filtered(db, None, n=1, q="")
    assert result.all_matches is True
    assert result.total_count == 3
    assert result.page_size == HISTORY_PAGE_SIZE
    # Newest kickoff_hkt first: 2026-11-05 20:00, then 18:00, then 11-04.
    assert [m["kickoff_hhmm"] for m in result.matches] == ["20:00", "18:00", "20:00"]
    assert result.matches[0]["match_date"] == "2026-11-05"
    assert result.matches[2]["match_date"] == "2026-11-04"


def test_history_date_filter_and_missing_scores(tmp_path: Path) -> None:
    db = _init_history_db(tmp_path)
    result = fetch_history_filtered(db, date(2026, 11, 5), n=1)
    assert result.all_matches is False
    assert result.total_count == 2
    by_home = {m["home_team"]: m for m in result.matches}
    assert by_home["主隊甲"]["score_ft"] == "2 : 1"
    assert by_home["主隊甲"]["score_ht"] == "1 : 0"
    # Missing scores → em dash, never coerced to 0.
    assert by_home["主隊丙"]["score_ft"] == EMPTY_DISPLAY
    assert by_home["主隊丙"]["score_ht"] == EMPTY_DISPLAY
    # Schema-driven fields (incl. migrated score/odds columns) present; NULL → —.
    for col in (
        "score_2h_home",
        "score_2h_away",
        "score_et_home",
        "score_et_away",
        "score_pens_home",
        "score_pens_away",
        "final_odds_home",
        "final_odds_draw",
        "final_odds_away",
        "final_odds_timestamp",
        "final_odds_source",
    ):
        assert col in by_home["主隊甲"]
        assert by_home["主隊甲"][col] == EMPTY_DISPLAY
        assert by_home["主隊甲"][col] != "0"
    assert "score_2h_home" in result.field_keys


def test_history_id_fallback_and_search(tmp_path: Path) -> None:
    db = _init_history_db(tmp_path)
    result = fetch_history_filtered(db, date(2026, 11, 4), n=1)
    assert result.total_count == 1
    m = result.matches[0]
    assert m["competition_name"] == "c2"
    assert m["home_team"] == "h3"
    assert m["score_ft"] == "0 : 0"  # real zeros preserved

    found = fetch_history_filtered(db, None, n=1, q="主隊甲")
    assert found.total_count == 1
    assert found.matches[0]["home_team"] == "主隊甲"

    by_id = fetch_history_filtered(db, None, n=1, q="c2")
    assert by_id.total_count == 1
    assert by_id.matches[0]["competition_name"] == "c2"


def test_history_backend_paging(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(footylogic_schema_sql())
    for i in range(55):
        day = 1 + (i % 28)
        conn.execute(
            f"""
            INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
              source_key, source, competition_id, season_id, month, competition_name,
              home_team_id, away_team_id, home_team_name, away_team_name,
              kickoff_utc, kickoff_hkt, match_date_hkt,
              score_ft_home, score_ft_away, score_ht_home, score_ht_away,
              has_score, score_complete, result_confirm_status, event_json,
              created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"fl|x|s|h{i}|a{i}|2026-10-{day:02d}T{i%20:02d}:00:00.000Z",
                "footylogic",
                "x",
                "s",
                "2026-10",
                "聯賽",
                f"h{i}",
                f"a{i}",
                f"Home{i}",
                f"Away{i}",
                f"2026-10-{day:02d}T{i%20:02d}:00:00.000Z",
                f"2026-10-{day:02d}T{(i%20)+8:02d}:00:00+08:00",
                f"2026-10-{day:02d}",
                None,
                None,
                None,
                None,
                0,
                0,
                "source_score_unconfirmed",
                "{}",
                "2026-11-06T00:00:00Z",
                "2026-11-06T00:00:00Z",
            ),
        )
    conn.commit()
    conn.close()

    page1 = fetch_history_filtered(db, None, n=1, page_size=50)
    assert page1.total_count == 55
    assert page1.total_pages == 2
    assert len(page1.matches) == 50
    page2 = fetch_history_filtered(db, None, n=2, page_size=50)
    assert len(page2.matches) == 5
    assert page2.n == 2


def test_history_never_reads_quarantine(tmp_path: Path) -> None:
    db = _init_history_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO footylogic_quarantine (
          job_key, reason, competition_id, season_id, month,
          kickoff_utc, home_team_id, away_team_id, home_team_name, away_team_name,
          event_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "j1",
            "bad",
            "qx",
            "s",
            "2026-11",
            "2026-11-05T00:00:00.000Z",
            "qh",
            "qa",
            "隔離主隊",
            "隔離客隊",
            "{}",
            "2026-11-06T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    result = fetch_history_filtered(db, None, n=1, q="隔離")
    assert result.total_count == 0


def test_history_api_and_tab_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hppv.soccer_db import init_soccer_db

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    # Seed footylogic rows into the full soccer DB used by the web app.
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    seed = _init_history_db(seed_dir)
    src = sqlite3.connect(seed)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(db)
    dst.executescript(footylogic_schema_sql())
    for row in src.execute(f"SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE}"):
        keys = list(row.keys())
        placeholders = ",".join("?" for _ in keys)
        dst.execute(
            f"INSERT OR REPLACE INTO {FOOTYLOGIC_MATCHES_TABLE} ({','.join(keys)}) VALUES ({placeholders})",
            [row[k] for k in keys],
        )
    dst.commit()
    src.close()
    dst.close()

    import hppv.web_app as web_app

    monkeypatch.setattr(web_app, "SOCCER_DB_PATH", db)
    client = web_app.app.test_client()

    html = client.get("/soccer/daily?tab=history&all_matches=1").get_data(as_text=True)
    hist_slice = html.split('id="tab-panel-history"')[1].split('id="tab-panel-download"')[0]
    assert 'id="history-all-matches"' in hist_slice
    assert 'id="history-search"' in hist_slice
    assert 'id="history-pager"' in hist_slice
    assert "更新資料" not in hist_slice
    assert 'id="match-status-filter"' not in hist_slice
    assert "功能建置中" not in hist_slice
    # No history layout button in v1.
    assert 'id="btn-open-layout"' not in hist_slice
    assert "主隊甲" in hist_slice or "英超" in hist_slice

    api = client.get("/api/soccer/history-matches?all_matches=1&n=1").get_json()
    assert api["ok"] is True
    assert api["source"] == "footylogic_matches"
    assert api["all_matches"] is True
    assert api["total_count"] == 3
    assert api["page_size"] == HISTORY_PAGE_SIZE

    dated = client.get(
        "/api/soccer/history-matches?all_matches=0&date=2026-11-05&n=1"
    ).get_json()
    assert dated["ok"] is True
    assert dated["all_matches"] is False
    assert dated["total_count"] == 2

    # Daily matches UI still present and layout entry remains on daily toolbar only.
    daily_html = client.get("/soccer/daily?tab=matches").get_data(as_text=True)
    assert 'id="btn-open-layout"' in daily_html
    assert 'id="match-status-filter"' in daily_html


def test_history_sort_param_default() -> None:
    from hppv.soccer_history import default_history_sort_specs, history_sort_specs_from_layout

    assert parse_history_sort_param(None) is None
    assert parse_history_sort_param("") is None
    assert parse_history_sort_param("match_date:asc") == [("match_date", "asc")]
    assert parse_history_sort_param("score_ft:desc") is None  # composite score not sortable
    assert default_history_sort_specs() == [("kickoff_hhmm", "desc")]
    assert history_sort_specs_from_layout(None) == [("kickoff_hhmm", "desc")]


def test_history_translation_keys_seeded(tmp_path: Path) -> None:
    from hppv.soccer_db import get_connection, init_soccer_db

    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    keys = {
        "soccer.history.empty",
        "soccer.history.hint",
        "soccer.history.search_placeholder",
        "soccer.history.pager.status",
        "soccer.history.read_error",
    }
    with get_connection(db_path) as conn:
        for key in keys:
            row = conn.execute(
                "SELECT zh_hant, en, zh_hans FROM translations WHERE text_key=?",
                (key,),
            ).fetchone()
            assert row is not None, key
            assert row["zh_hant"] and row["en"] and row["zh_hans"]
