from __future__ import annotations

from pathlib import Path

import pytest

from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_layout import (
    COLUMN_WIDTH_MAX,
    COLUMN_WIDTH_MIN,
    HISTORY_COLUMN_CATALOG,
    LAYOUT_KEY_DAILY_MATCHES,
    LAYOUT_KEY_MATCH_HISTORY,
    SoccerLayoutError,
    default_layout_settings,
    get_layout_settings,
    save_layout_settings,
    validate_column_width,
)


def test_width_validation_bounds() -> None:
    assert validate_column_width(60) == 60
    assert validate_column_width(600) == 600
    with pytest.raises(SoccerLayoutError) as e1:
        validate_column_width(59)
    assert e1.value.code == "invalid_width"
    with pytest.raises(SoccerLayoutError) as e2:
        validate_column_width(601)
    assert e2.value.code == "invalid_width"
    with pytest.raises(SoccerLayoutError):
        validate_column_width(100.5)
    with pytest.raises(SoccerLayoutError):
        validate_column_width("100px")
    assert COLUMN_WIDTH_MIN == 60
    assert COLUMN_WIDTH_MAX == 600


def test_layout_save_and_reload(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    settings = default_layout_settings()
    settings["zebra_enabled"] = False
    settings["zebra_color_id"] = "row_alt_blue"
    settings["columns"][0]["width"] = 120
    settings["columns"][0]["sort"] = "desc"
    saved = save_layout_settings(db, settings)
    assert saved["zebra_color_id"] == "row_alt_blue"
    loaded = get_layout_settings(db)
    assert loaded["zebra_enabled"] is False
    assert loaded["columns"][0]["width"] == 120
    assert loaded["columns"][0]["sort"] == "desc"


def test_layout_rejects_unknown_column(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    settings = default_layout_settings()
    settings["columns"][0]["key"] = "not_a_column"
    with pytest.raises(SoccerLayoutError) as exc:
        save_layout_settings(db, settings)
    assert exc.value.code == "invalid_settings"


def test_layout_table_created_on_init(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='soccer_layout_settings'"
        ).fetchone()
        assert row is not None


def test_get_layout_persists_match_date_without_reset(tmp_path: Path) -> None:
    import json

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    legacy = default_layout_settings()
    legacy["columns"] = [c for c in legacy["columns"] if c["key"] != "match_date"]
    # Preserve a non-default width on an existing column.
    for c in legacy["columns"]:
        if c["key"] == "home_team":
            c["width"] = 177
            c["visible"] = False
    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO soccer_layout_settings (layout_key, settings_json, updated_at) VALUES (?,?,?)",
            ("daily_matches", json.dumps(legacy), "2026-11-04T00:00:00+00:00"),
        )
        conn.commit()

    loaded = get_layout_settings(db)
    keys = [c["key"] for c in loaded["columns"]]
    assert "match_date" in keys
    by_key = {c["key"]: c for c in loaded["columns"]}
    assert by_key["home_team"]["width"] == 177
    assert by_key["home_team"]["visible"] is False

    # Write-back persisted match_date into stored JSON.
    with get_connection(db) as conn:
        raw = json.loads(
            conn.execute(
                "SELECT settings_json FROM soccer_layout_settings WHERE layout_key=?",
                ("daily_matches",),
            ).fetchone()[0]
        )
    stored_keys = [c["key"] for c in raw["columns"]]
    assert "match_date" in stored_keys
    assert next(c for c in raw["columns"] if c["key"] == "home_team")["width"] == 177


def test_default_layout_includes_match_date_separate_from_kickoff() -> None:
    settings = default_layout_settings()
    keys = [c["key"] for c in settings["columns"]]
    assert "match_date" in keys
    assert "kickoff_hhmm" in keys
    assert keys.index("match_date") < keys.index("kickoff_hhmm")
    by_key = {c["key"]: c for c in settings["columns"]}
    assert by_key["match_date"]["visible"] is True
    assert by_key["kickoff_hhmm"]["sort"] == "asc"
    assert by_key["match_date"]["sort"] == ""


def test_layout_migrates_legacy_ui_keys(tmp_path: Path) -> None:
    import json

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    # Distinct width/visible/sort to prove migration preserves settings (not defaults).
    legacy = {
        "zebra_enabled": True,
        "zebra_color_id": "row_alt_gray",
        "columns": [
            {"key": "kickoff_hhmm", "visible": True, "width": 100, "sort": "asc"},
            {"key": "match_code", "visible": False, "width": 88, "sort": ""},
            {"key": "icon", "visible": True, "width": 60, "sort": ""},
            {"key": "competition_code", "visible": True, "width": 100, "sort": ""},
            {"key": "competition_name_zh", "visible": True, "width": 100, "sort": ""},
            {"key": "home_team", "visible": True, "width": 140, "sort": ""},
            {"key": "away_team", "visible": True, "width": 100, "sort": ""},
            {"key": "home_odds", "visible": True, "width": 100, "sort": ""},
            {"key": "draw_odds", "visible": False, "width": 72, "sort": ""},
            {"key": "away_odds", "visible": True, "width": 100, "sort": ""},
            {"key": "ui_ht", "visible": True, "width": 111, "sort": ""},
            {"key": "ui_2h", "visible": False, "width": 122, "sort": ""},
            {"key": "ui_ft", "visible": True, "width": 133, "sort": "desc"},
            {"key": "ui_et", "visible": False, "width": 144, "sort": ""},
            {"key": "ui_pen", "visible": True, "width": 155, "sort": ""},
        ],
    }
    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO soccer_layout_settings (layout_key, settings_json, updated_at) VALUES (?,?,?)",
            ("daily_matches", json.dumps(legacy), "2026-11-04T00:00:00+00:00"),
        )
        conn.commit()
    loaded = get_layout_settings(db)
    keys = [c["key"] for c in loaded["columns"]]
    by_key = {c["key"]: c for c in loaded["columns"]}

    # Legacy ui_* renamed; ui_et retained as layout-only pending slot (never silently dropped).
    assert "ui_ht" not in keys
    assert "ui_2h" not in keys
    assert "ui_ft" not in keys
    assert "ui_pen" not in keys
    assert "ui_et" in keys
    assert "score_ht" in keys
    assert "score_2h" in keys
    assert "score_ft" in keys
    assert "score_pen" in keys
    assert "result_status" in keys

    # Relative order of migrated keys preserved; result_status appended at end.
    assert keys.index("score_ht") < keys.index("score_2h") < keys.index("score_ft")
    assert keys.index("score_ft") < keys.index("ui_et") < keys.index("score_pen")
    assert keys.index("score_pen") < keys.index("result_status")

    assert by_key["match_code"]["visible"] is False
    assert by_key["match_code"]["width"] == 88
    assert by_key["draw_odds"]["visible"] is False
    assert by_key["draw_odds"]["width"] == 72
    assert by_key["score_ht"]["visible"] is True
    assert by_key["score_ht"]["width"] == 111
    assert by_key["score_2h"]["visible"] is False
    assert by_key["score_2h"]["width"] == 122
    assert by_key["score_ft"]["visible"] is True
    assert by_key["score_ft"]["width"] == 133
    assert by_key["score_ft"]["sort"] == "desc"
    assert by_key["ui_et"]["visible"] is False
    assert by_key["ui_et"]["width"] == 144
    assert by_key["score_pen"]["visible"] is True
    assert by_key["score_pen"]["width"] == 155
    # Newly introduced catalog key gets default visible/width.
    assert by_key["result_status"]["visible"] is False
    assert by_key["result_status"]["width"] == 100
    # match_date appended for existing layouts without resetting other columns.
    assert "match_date" in keys
    assert keys.index("result_status") < keys.index("match_date") or keys.index(
        "kickoff_hhmm"
    ) < keys.index("match_date")
    assert by_key["match_date"]["visible"] is True
    assert by_key["match_date"]["width"] == 110
    assert by_key["match_code"]["width"] == 88


def test_daily_page_layout_mode_markup_and_legacy_tab() -> None:
    """List/layout modes are exclusive; ?tab=layout opens settings mode."""
    from hppv.web_app import app

    client = app.test_client()
    html = client.get("/soccer/daily").get_data(as_text=True)

    assert 'id="matches-list-mode"' in html
    assert 'id="matches-layout-mode"' in html
    assert "matches-mode-list" in html
    assert 'id="matches-layout-panel"' not in html
    assert 'id="tab-btn-layout"' not in html
    assert 'id="layout-mode-toolbar"' in html
    assert "每日賽事 — 版面設定" in html or "Daily Matches — Layout" in html
    assert 'id="btn-layout-save"' in html
    assert 'id="btn-layout-cancel"' in html
    # Layout controls stay inside layout mode (not stacked over the list chrome).
    list_mode = html.find('id="matches-list-mode"')
    layout_mode = html.find('id="matches-layout-mode"')
    save_btn = html.find('id="btn-layout-save"')
    pager = html.find('id="matches-pager"')
    assert 0 <= list_mode < pager < layout_mode < save_btn
    # Default SSR: layout mode marked hidden.
    layout_chunk = html[layout_mode : layout_mode + 180]
    assert "hidden" in layout_chunk

    layout_html = client.get("/soccer/daily?tab=layout").get_data(as_text=True)
    assert "matches-mode-layout" in layout_html
    assert 'id="tab-btn-matches"' in layout_html
    assert 'id="tab-btn-history"' in layout_html
    assert 'aria-expanded="true"' in layout_html
    # History tab remains independent (no daily layout controls there).
    hist = layout_html.find('id="tab-panel-history"')
    assert hist > 0
    hist_slice = layout_html[hist:]
    assert 'id="btn-open-layout"' not in hist_slice.split('id="tab-panel-download"')[0]
    assert 'id="history-all-matches"' in hist_slice


def test_history_layout_independent_from_daily(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    daily = default_layout_settings(LAYOUT_KEY_DAILY_MATCHES)
    daily["columns"][0]["width"] = 222
    save_layout_settings(db, daily, layout_key=LAYOUT_KEY_DAILY_MATCHES)

    hist = default_layout_settings(LAYOUT_KEY_MATCH_HISTORY)
    keys = [c["key"] for c in hist["columns"]]
    assert keys == [c["key"] for c in HISTORY_COLUMN_CATALOG]
    assert "match_code" not in keys
    assert "home_odds" not in keys
    assert hist["columns"][1]["key"] == "kickoff_hhmm"
    assert hist["columns"][1]["sort"] == "desc"
    # Composite scores exist but are not sortable.
    score_ht = next(c for c in HISTORY_COLUMN_CATALOG if c["key"] == "score_ht")
    assert score_ht["sortable"] is False
    hist["columns"][0]["width"] = 133
    save_layout_settings(db, hist, layout_key=LAYOUT_KEY_MATCH_HISTORY)

    loaded_daily = get_layout_settings(db, LAYOUT_KEY_DAILY_MATCHES)
    loaded_hist = get_layout_settings(db, LAYOUT_KEY_MATCH_HISTORY)
    assert loaded_daily["columns"][0]["width"] == 222
    assert loaded_hist["columns"][0]["width"] == 133
    assert len(loaded_hist["columns"]) == len(HISTORY_COLUMN_CATALOG)

    # Reject invented sort on composite score columns.
    bad = default_layout_settings(LAYOUT_KEY_MATCH_HISTORY)
    for c in bad["columns"]:
        if c["key"] == "score_ft":
            c["sort"] = "desc"
    with pytest.raises(SoccerLayoutError) as exc:
        save_layout_settings(db, bad, layout_key=LAYOUT_KEY_MATCH_HISTORY)
    assert exc.value.code == "invalid_settings"


def test_history_layout_api_and_toolbar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hppv.web_app as web_app

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    monkeypatch.setattr(web_app, "SOCCER_DB_PATH", db)
    client = web_app.app.test_client()

    html = client.get("/soccer/daily?tab=history").get_data(as_text=True)
    hist = html.split('id="tab-panel-history"')[1].split('id="tab-panel-download"')[0]
    assert 'id="btn-open-history-layout"' in hist
    assert 'id="history-layout-mode"' in hist
    assert "球賽往績 — 版面設定" in html or "Match History — Layout Settings" in html
    assert 'id="btn-open-layout"' not in hist

    layout_html = client.get("/soccer/daily?tab=history-layout").get_data(as_text=True)
    assert "matches-mode-layout" in layout_html
    assert 'id="tab-btn-history"' in layout_html

    got = client.get("/api/soccer/layout/match-history").get_json()
    assert got["ok"] is True
    assert got["layout_key"] == LAYOUT_KEY_MATCH_HISTORY
    assert len(got["settings"]["columns"]) == len(HISTORY_COLUMN_CATALOG)

    settings = got["settings"]
    settings["columns"][2]["visible"] = False
    settings["columns"][2]["width"] = 180
    saved = client.post(
        "/api/soccer/layout/match-history",
        json={"settings": settings},
    ).get_json()
    assert saved["ok"] is True

    # Daily layout untouched.
    daily = client.get("/api/soccer/layout/daily-matches").get_json()
    assert daily["ok"] is True
    assert daily["layout_key"] == LAYOUT_KEY_DAILY_MATCHES
    assert len(daily["settings"]["columns"]) > len(HISTORY_COLUMN_CATALOG)


def test_layout_mode_title_translation_seeded(tmp_path: Path) -> None:
    from hppv.soccer_db import get_connection, init_soccer_db

    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT zh_hant, en, zh_hans FROM translations WHERE text_key=?",
            ("soccer.daily.layout.mode_title",),
        ).fetchone()
    assert row is not None
    assert row["zh_hant"] == "每日賽事 — 版面設定"
    assert row["en"] == "Daily Matches — Layout"
    assert row["zh_hans"] == "每日赛事 — 版面设定"

