from __future__ import annotations

from pathlib import Path

import pytest

from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_layout import (
    COLUMN_WIDTH_MAX,
    COLUMN_WIDTH_MIN,
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

