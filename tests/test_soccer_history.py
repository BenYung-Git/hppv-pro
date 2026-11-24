from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE, footylogic_schema_sql
from hppv.soccer_history import (
    DEFAULT_HISTORY_SEARCH_KEYS,
    DEFAULT_HISTORY_SEARCH_OP,
    HISTORY_PAGE_SIZE,
    HISTORY_SEARCH_OP_APPROX,
    HISTORY_SEARCH_OP_CONTAINS,
    HISTORY_SEARCH_OP_EQ,
    HISTORY_SEARCH_OP_NE,
    HISTORY_SEARCH_OP_NOT_CONTAINS,
    HISTORY_SEARCH_OP_STARTS,
    HISTORY_SEARCH_OPS_VISIBLE,
    HISTORY_TEAM_RESULT_DRAW,
    HISTORY_TEAM_RESULT_LOSE,
    HISTORY_TEAM_RESULT_NEUTRAL,
    HISTORY_TEAM_RESULT_WIN,
    HISTORY_VOID_SCORE_DISPLAY,
    HistorySearchError,
    fetch_history_filtered,
    format_name_or_id,
    history_search_keys_from_layout,
    history_team_result_classes,
    normalize_history_search_op,
    parse_history_sort_param,
)
from hppv.soccer_ingest import HKJC_RESULT_CONFIRM_STATUS, HKJC_RESULT_VOID_STATUS
from hppv.soccer_history_export import (
    count_history_export_rows,
    parse_history_export_request,
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
    assert found.q_op == DEFAULT_HISTORY_SEARCH_OP
    assert list(found.search_keys) == list(DEFAULT_HISTORY_SEARCH_KEYS)

    # Default keys include competition_name (NULL name falls back to competition_id).
    by_comp = fetch_history_filtered(db, None, n=1, q="c2")
    assert by_comp.total_count == 1
    assert by_comp.matches[0]["competition_name"] == "c2"

    # Missing home name falls back to home_team_id.
    by_home_id = fetch_history_filtered(db, None, n=1, q="h3")
    assert by_home_id.total_count == 1
    assert by_home_id.matches[0]["home_team"] == "h3"

    # Away-only hit.
    by_away = fetch_history_filtered(db, None, n=1, q="客隊戊")
    assert by_away.total_count == 1
    assert by_away.matches[0]["away_team"] == "客隊戊"

    # Hide competition_name → competition id no longer matches.
    home_away_only = fetch_history_filtered(
        db, None, n=1, q="c2", search_keys=["home_team", "away_team"]
    )
    assert home_away_only.total_count == 0


def test_history_search_ops_and_export_q_op(tmp_path: Path) -> None:
    db = _init_history_db(tmp_path)
    assert normalize_history_search_op("") == HISTORY_SEARCH_OP_APPROX
    assert normalize_history_search_op("contains") == HISTORY_SEARCH_OP_CONTAINS
    assert list(HISTORY_SEARCH_OPS_VISIBLE)[0] == HISTORY_SEARCH_OP_APPROX

    approx = fetch_history_filtered(db, None, n=1, q="主隊", q_op=HISTORY_SEARCH_OP_APPROX)
    assert approx.total_count == 2  # 主隊甲 + 主隊丙

    exact = fetch_history_filtered(db, None, n=1, q="主隊甲", q_op=HISTORY_SEARCH_OP_EQ)
    assert exact.total_count == 1
    assert exact.matches[0]["home_team"] == "主隊甲"

    starts = fetch_history_filtered(db, None, n=1, q="客隊", q_op=HISTORY_SEARCH_OP_STARTS)
    assert starts.total_count == 3

    # approx is case-insensitive; contains is case-sensitive on stored text.
    # Seed uses Chinese names — use ASCII team via id fallback for case contrast.
    ci = fetch_history_filtered(db, None, n=1, q="H3", q_op=HISTORY_SEARCH_OP_APPROX)
    assert ci.total_count == 1
    cs = fetch_history_filtered(db, None, n=1, q="H3", q_op=HISTORY_SEARCH_OP_CONTAINS)
    assert cs.total_count == 0
    cs2 = fetch_history_filtered(db, None, n=1, q="h3", q_op=HISTORY_SEARCH_OP_CONTAINS)
    assert cs2.total_count == 1

    req = parse_history_export_request(
        {
            "file_type": "csv",
            "filename": "t.csv",
            "q": "主隊甲",
            "q_op": "eq",
            "all_matches": True,
            "sort": "kickoff_hhmm:desc",
            "columns": [{"key": "home_team", "width": 120}],
            "lang": "zh-Hant",
        },
        catalog_keys=["home_team", "away_team"],
        db_path=db,
    )
    assert req.q_op == HISTORY_SEARCH_OP_EQ
    listed = fetch_history_filtered(
        db, None, n=1, q=req.q, q_op=req.q_op, page_size=1
    )
    assert listed.total_count == 1


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


def _seed_soccer_web_db(tmp_path: Path) -> Path:
    from hppv.soccer_db import init_soccer_db

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
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
    return db


def test_history_api_and_tab_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed_soccer_web_db(tmp_path)

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

    api = client.get(
        "/api/records/soccer/match_history?all_matches=1&n=1"
    ).get_json()
    assert api["ok"] is True
    assert api["source"] == "footylogic_matches"
    assert api["all_matches"] is True
    assert api["total_count"] == 3
    assert api["page_size"] == HISTORY_PAGE_SIZE

    dated = client.get(
        "/api/records/soccer/match_history?all_matches=0&date=2026-11-05&n=1"
    ).get_json()
    assert dated["ok"] is True
    assert dated["all_matches"] is False
    assert dated["total_count"] == 2

    # Daily matches UI still present and layout entry remains on daily toolbar only.
    daily_html = client.get("/soccer/daily?tab=matches").get_data(as_text=True)
    assert 'id="btn-open-layout"' in daily_html
    assert 'id="match-status-filter"' not in daily_html
    assert "status-mapping-note" not in daily_html


def test_records_match_history_shell_q_op_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell q/q_op must win over history_* (racing-aligned records contract)."""
    db = _seed_soccer_web_db(tmp_path)
    import hppv.web_app as web_app

    monkeypatch.setattr(web_app, "SOCCER_DB_PATH", db)
    client = web_app.app.test_client()

    shell = client.get(
        "/api/records/soccer/match_history"
        "?all_matches=1&n=1&q=%E4%B8%BB%E9%9A%8A%E7%94%B2&q_op=eq"
    ).get_json()
    assert shell["ok"] is True
    assert shell["q_op"] == HISTORY_SEARCH_OP_EQ
    assert shell["q"] == "主隊甲"
    assert shell["total_count"] == 1

    # Conflicting history_q_op must not override shell q_op.
    mixed = client.get(
        "/api/records/soccer/match_history"
        "?all_matches=1&n=1&q=%E4%B8%BB%E9%9A%8A%E7%94%B2&q_op=eq"
        "&history_q_op=approx&history_q=wrong"
    ).get_json()
    assert mixed["ok"] is True
    assert mixed["q_op"] == HISTORY_SEARCH_OP_EQ
    assert mixed["q"] == "主隊甲"
    assert mixed["total_count"] == 1

    # history_* alone still works as fallback.
    legacy = client.get(
        "/api/records/soccer/match_history"
        "?all_matches=1&history_n=1&history_q=%E4%B8%BB%E9%9A%8A%E7%94%B2"
        "&history_q_op=eq"
    ).get_json()
    assert legacy["ok"] is True
    assert legacy["q_op"] == HISTORY_SEARCH_OP_EQ
    assert legacy["q"] == "主隊甲"
    assert legacy["total_count"] == 1


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
        "soccer.history.pager.first",
        "soccer.history.pager.prev",
        "soccer.history.pager.next",
        "soccer.history.pager.last",
        "soccer.history.read_error",
        "soccer.history.search_op.approx",
        "soccer.history.search_op.contains",
        "soccer.history.search_op.gt",  # hidden but seeded
    }
    with get_connection(db_path) as conn:
        for key in keys:
            row = conn.execute(
                "SELECT zh_hant, en, zh_hans FROM translations WHERE text_key=?",
                (key,),
            ).fetchone()
            assert row is not None, key
            assert row["zh_hant"] and row["en"] and row["zh_hans"]


def test_history_team_result_classes_ft_rules() -> None:
    assert history_team_result_classes(
        2, 1, result_confirm_status=HKJC_RESULT_CONFIRM_STATUS
    ) == (HISTORY_TEAM_RESULT_WIN, HISTORY_TEAM_RESULT_LOSE)
    assert history_team_result_classes(
        0, 3, result_confirm_status=HKJC_RESULT_CONFIRM_STATUS
    ) == (HISTORY_TEAM_RESULT_LOSE, HISTORY_TEAM_RESULT_WIN)
    assert history_team_result_classes(
        0, 0, result_confirm_status=HKJC_RESULT_CONFIRM_STATUS
    ) == (HISTORY_TEAM_RESULT_DRAW, HISTORY_TEAM_RESULT_DRAW)
    assert history_team_result_classes(
        None, None, result_confirm_status=HKJC_RESULT_VOID_STATUS
    ) == (HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL)
    # Missing FT must not become 0-0 draw.
    assert history_team_result_classes(
        None, None, result_confirm_status=HKJC_RESULT_CONFIRM_STATUS
    ) == (HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL)
    assert history_team_result_classes(
        1, None, result_confirm_status=HKJC_RESULT_CONFIRM_STATUS
    ) == (HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL)
    assert history_team_result_classes(
        1, 0, result_confirm_status="source_score_unconfirmed"
    ) == (HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL)


def test_history_void_status_shows_void_for_ht_ft(tmp_path: Path) -> None:
    db = tmp_path / "void_hist.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(footylogic_schema_sql())
    conn.execute(
        f"""
        INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
          source_key, source, competition_id, season_id, month, competition_name,
          home_team_id, away_team_id, home_team_name, away_team_name,
          kickoff_utc, kickoff_hkt, match_date_hkt,
          score_ft_home, score_ft_away, score_ht_home, score_ht_away,
          has_score, score_complete, result_confirm_status, event_json,
          source_last_updated, created_at, updated_at,
          hkjc_match_id, hkjc_front_end_id
        ) VALUES (
          'hkjc|50080133', 'hkjc', 'UCL', NULL, '2026-11', '女足賽',
          'h', 'a', '賓菲加女足', '奧地利維也納女足',
          '2026-11-12T12:00:00Z', '2026-11-12T20:00:00+08:00', '2026-11-12',
          NULL, NULL, NULL, NULL,
          0, 0, ?, '{{}}',
          't', 't', 't',
          '50080133', 'FB8688'
        )
        """,
        (HKJC_RESULT_VOID_STATUS,),
    )
    conn.commit()
    conn.close()
    result = fetch_history_filtered(db, date(2026, 11, 12), n=1)
    assert result.total_count == 1
    assert result.matches[0]["score_ht"] == HISTORY_VOID_SCORE_DISPLAY
    assert result.matches[0]["score_ft"] == HISTORY_VOID_SCORE_DISPLAY
    assert result.matches[0]["result_confirm_status"] == HKJC_RESULT_VOID_STATUS
    assert result.matches[0]["home_team_result"] == HISTORY_TEAM_RESULT_NEUTRAL
    assert result.matches[0]["away_team_result"] == HISTORY_TEAM_RESULT_NEUTRAL


def test_history_search_keys_from_layout_visible_whitelist() -> None:
    assert history_search_keys_from_layout(None) == list(DEFAULT_HISTORY_SEARCH_KEYS)
    layout = {
        "zebra_enabled": True,
        "zebra_color_id": "row_alt_gray",
        "columns": [
            {"key": "match_date", "visible": True, "width": 100, "sort": ""},
            {"key": "competition_name", "visible": True, "width": 160, "sort": ""},
            {"key": "home_team", "visible": False, "width": 140, "sort": ""},
            {"key": "away_team", "visible": True, "width": 140, "sort": ""},
            {"key": "score_ft", "visible": True, "width": 90, "sort": ""},
            {"key": "kickoff_hhmm", "visible": True, "width": 90, "sort": "desc"},
        ],
    }
    assert history_search_keys_from_layout(layout) == ["competition_name", "away_team"]
    # Order / width must not change semantics (same keys, different order).
    layout["columns"] = list(reversed(layout["columns"]))
    assert set(history_search_keys_from_layout(layout)) == {
        "competition_name",
        "away_team",
    }
    # Hide all searchable → empty (not silent fallback to defaults).
    for col in layout["columns"]:
        if col["key"] in DEFAULT_HISTORY_SEARCH_KEYS:
            col["visible"] = False
    assert history_search_keys_from_layout(layout) == []


def test_history_search_respects_layout_keys_and_ops(tmp_path: Path) -> None:
    db = _init_history_db(tmp_path)
    # Seed an extra row whose competition name overlaps a team-like token.
    conn = sqlite3.connect(db)
    conn.execute(
        f"""
        INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
          source_key, source, competition_id, season_id, month, competition_name,
          home_team_id, away_team_id, home_team_name, away_team_name,
          kickoff_utc, kickoff_hkt, match_date_hkt,
          score_ft_home, score_ft_away, score_ht_home, score_ht_away,
          has_score, score_complete, result_confirm_status, event_json,
          source_last_updated, created_at, updated_at
        ) VALUES (
          'fl|ucl|s|hx|ax|2026-11-03T12:00:00.000Z', 'footylogic', 'ucl', 's1', '2026-11',
          '歐洲聯賽冠軍盃', 'hx', 'ax', '主隊歐冠', '客隊歐冠',
          '2026-11-03T12:00:00.000Z', '2026-11-03T20:00:00+08:00', '2026-11-03',
          1, 0, 0, 0, 1, 1, 'source_score_unconfirmed', '{{}}',
          NULL, 't', 't'
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
          source_key, source, competition_id, season_id, month, competition_name,
          home_team_id, away_team_id, home_team_name, away_team_name,
          kickoff_utc, kickoff_hkt, match_date_hkt,
          score_ft_home, score_ft_away, score_ht_home, score_ht_away,
          has_score, score_complete, result_confirm_status, event_json,
          source_last_updated, created_at, updated_at
        ) VALUES (
          'fl|wucl|s|hy|ay|2026-11-02T12:00:00.000Z', 'footylogic', 'wucl', 's1', '2026-11',
          '女子歐洲聯賽冠軍盃', 'hy', 'ay', '女主', '女客',
          '2026-11-02T12:00:00.000Z', '2026-11-02T20:00:00+08:00', '2026-11-02',
          2, 2, 1, 1, 1, 1, 'source_score_unconfirmed', '{{}}',
          NULL, 't', 't'
        )
        """
    )
    conn.commit()
    conn.close()

    eq_all = fetch_history_filtered(
        db,
        None,
        n=1,
        q="歐洲聯賽冠軍盃",
        q_op=HISTORY_SEARCH_OP_EQ,
        search_keys=["competition_name", "home_team", "away_team"],
    )
    assert eq_all.total_count == 1
    assert eq_all.matches[0]["competition_name"] == "歐洲聯賽冠軍盃"

    approx_all = fetch_history_filtered(
        db,
        None,
        n=1,
        q="歐洲聯賽冠軍盃",
        q_op=HISTORY_SEARCH_OP_APPROX,
        search_keys=["competition_name", "home_team", "away_team"],
    )
    names = {m["competition_name"] for m in approx_all.matches}
    assert "歐洲聯賽冠軍盃" in names
    assert "女子歐洲聯賽冠軍盃" in names
    assert approx_all.total_count == 2

    # Hide competition_name → competition keyword no longer matches.
    no_comp = fetch_history_filtered(
        db,
        None,
        n=1,
        q="歐洲聯賽冠軍盃",
        q_op=HISTORY_SEARCH_OP_EQ,
        search_keys=["home_team", "away_team"],
    )
    assert no_comp.total_count == 0

    # Positive OR: home-only key still finds team.
    home_only = fetch_history_filtered(
        db, None, n=1, q="主隊甲", q_op=HISTORY_SEARCH_OP_EQ, search_keys=["home_team"]
    )
    assert home_only.total_count == 1

    # Negative AND across fields: ne excludes when ANY field equals → wait, AND means
    # all fields != needle. Row with home=主隊甲 still kept if competition/away differ
    # when only home is searchable and equals → excluded.
    ne_home = fetch_history_filtered(
        db, None, n=1, q="主隊甲", q_op=HISTORY_SEARCH_OP_NE, search_keys=["home_team"]
    )
    assert all(m["home_team"] != "主隊甲" for m in ne_home.matches)

    not_contains = fetch_history_filtered(
        db,
        None,
        n=1,
        q="歐洲聯賽冠軍盃",
        q_op=HISTORY_SEARCH_OP_NOT_CONTAINS,
        search_keys=["competition_name"],
    )
    assert all("歐洲聯賽冠軍盃" not in m["competition_name"] for m in not_contains.matches)

    # Empty searchable keys + non-empty q → explicit error (not silent all-rows).
    with pytest.raises(HistorySearchError) as exc:
        fetch_history_filtered(db, None, n=1, q="英超", search_keys=[])
    assert exc.value.code == "no_searchable_fields"

    # Empty q with no searchable keys is allowed (no text filter).
    all_rows = fetch_history_filtered(db, None, n=1, q="", search_keys=[])
    assert all_rows.total_count == 5

    # all_matches keeps date off; dated filter still applies.
    dated = fetch_history_filtered(
        db,
        date(2026, 11, 3),
        n=1,
        q="歐洲聯賽冠軍盃",
        q_op=HISTORY_SEARCH_OP_EQ,
        search_keys=["competition_name"],
    )
    assert dated.total_count == 1
    assert dated.all_matches is False


def test_history_list_and_export_share_search_keys(tmp_path: Path) -> None:
    db = _init_history_db(tmp_path)
    listed = fetch_history_filtered(
        db,
        None,
        n=1,
        q="英超",
        q_op=HISTORY_SEARCH_OP_EQ,
        search_keys=["competition_name", "home_team", "away_team"],
    )
    req = parse_history_export_request(
        {
            "file_type": "csv",
            "filename": "t.csv",
            "q": "英超",
            "q_op": "eq",
            "all_matches": True,
            "sort": "kickoff_hhmm:desc",
            "columns": [
                {"key": "competition_name", "width": 160},
                {"key": "home_team", "width": 120},
                {"key": "away_team", "width": 120},
                {"key": "score_ft", "width": 90},
            ],
            "lang": "zh-Hant",
        },
        catalog_keys=[
            "competition_name",
            "home_team",
            "away_team",
            "score_ft",
            "match_date",
        ],
        db_path=db,
    )
    assert list(req.search_keys) == ["competition_name", "home_team", "away_team"]
    assert count_history_export_rows(db, req) == listed.total_count

    # Export without searchable visible columns + q → rejected.
    with pytest.raises(Exception) as exc:
        parse_history_export_request(
            {
                "file_type": "csv",
                "filename": "t.csv",
                "q": "英超",
                "q_op": "eq",
                "all_matches": True,
                "columns": [{"key": "score_ft", "width": 90}],
                "lang": "zh-Hant",
            },
            catalog_keys=["score_ft", "competition_name"],
            db_path=db,
        )
    assert getattr(exc.value, "code", "") == "no_searchable_fields"
