"""Racing history list/layout: uses race_records; all columns default visible."""

from __future__ import annotations

from pathlib import Path

import pytest

from hppv.db import get_connection, init_db
from hppv.racing_history import fetch_racing_history_filtered
from hppv.racing_layout import (
    RACING_HISTORY_COLUMN_CATALOG,
    default_layout_settings,
    get_layout_settings,
    layout_settings_path,
    save_layout_settings,
)
from hppv.soccer_db import init_soccer_db


def _seed_minimal_racing_db(db: Path) -> None:
    init_db(db)
    with get_connection(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO horses (horse_id, brand_no, name_zh, name_en, status) "
            "VALUES ('HK_2020_A001', 'A001', '測試馬', 'Test Horse', '現役')"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO race_records (
              horse_id, race_date, venue, race_no, course, distance, going, class_band,
              draw, rating, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
            ) VALUES (
              'HK_2020_A001', '01/02/2026', '沙田', '1', '草地', '1200', '好', '第四班',
              '3', '60', '騎師甲', '練馬師乙', '02', '2 2 2', '5.5', '1.10.00', '100000'
            )
            """
        )
        conn.commit()


def test_all_catalog_columns_default_visible() -> None:
    assert RACING_HISTORY_COLUMN_CATALOG
    assert any(c["key"] == "handicap" for c in RACING_HISTORY_COLUMN_CATALOG)
    assert all(bool(c.get("default_visible")) for c in RACING_HISTORY_COLUMN_CATALOG)
    settings = default_layout_settings()
    assert all(c["visible"] for c in settings["columns"])
    assert len(settings["columns"]) == len(RACING_HISTORY_COLUMN_CATALOG)


def test_layout_reset_restores_all_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPPV_DATA_DIR", str(tmp_path))  # may be unused
    # Point layout file into tmp via monkeypatch of layout_settings_path
    import hppv.racing_layout as rl

    path = tmp_path / "racing_layout_settings.json"
    monkeypatch.setattr(rl, "layout_settings_path", lambda: path)
    settings = default_layout_settings()
    settings["columns"][0]["visible"] = False
    save_layout_settings(settings)
    loaded = get_layout_settings()
    assert loaded["columns"][0]["visible"] is False
    restored = save_layout_settings(default_layout_settings())
    assert all(c["visible"] for c in restored["columns"])


def test_fetch_racing_history_from_race_records(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed_minimal_racing_db(db)
    before = get_connection(db).execute("select count(*) c from race_records").fetchone()["c"]
    result = fetch_racing_history_filtered(db, all_matches=True, q="測試馬", q_op="contains")
    assert result.total_count >= 1
    assert result.rows[0]["name_zh"] == "測試馬"
    assert result.rows[0]["horse_id"] == "HK_2020_A001"
    assert result.rows[0]["race_date"] == "2026-02-01"
    after = get_connection(db).execute("select count(*) c from race_records").fetchone()["c"]
    assert before == after  # read-only


def test_blank_date_defaults_to_latest_record(tmp_path: Path) -> None:
    from hppv.racing_history import latest_race_record_date_iso

    db = tmp_path / "hppv.sqlite3"
    _seed_minimal_racing_db(db)
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO race_records (
              horse_id, race_date, venue, race_no, course, distance, going, class_band,
              draw, rating, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
            ) VALUES (
              'HK_2020_A001', '15/03/2026', '沙田', '2', '草地', '1400', '好', '第三班',
              '1', '62', '騎師甲', '練馬師乙', '01', '1 1 1', '3.2', '1.22.00', '200000'
            )
            """
        )
        conn.commit()
    assert latest_race_record_date_iso(db) == "2026-03-15"
    result = fetch_racing_history_filtered(db, all_matches=False, race_date=None)
    assert result.race_date == "2026-03-15"
    assert result.total_count >= 1
    assert all(r["race_date"] == "2026-03-15" for r in result.rows)


def test_api_racing_history_and_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa
    import hppv.racing_layout as rl

    db = tmp_path / "hppv.sqlite3"
    soccer = tmp_path / "soccer.sqlite3"
    _seed_minimal_racing_db(db)
    init_soccer_db(soccer)
    monkeypatch.setattr(wa, "DB_PATH", db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", soccer)
    layout_path = tmp_path / "racing_layout_settings.json"
    monkeypatch.setattr(rl, "layout_settings_path", lambda: layout_path)

    client = wa.app.test_client()
    r = client.get("/api/racing/history-records?all_matches=1&n=1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["source"] == "race_records"
    assert body["table"] == "race_records"
    assert body["total_count"] >= 1
    assert all(c.get("default_visible") for c in body["catalog"])

    lay = client.get("/api/racing/layout/history")
    assert lay.status_code == 200
    assert all(c["visible"] for c in lay.get_json()["layout_settings"]["columns"])

    hide = default_layout_settings()
    hide["columns"][0]["visible"] = False
    saved = client.post("/api/racing/layout/history", json={"layout_settings": hide})
    assert saved.status_code == 200
    assert saved.get_json()["layout_settings"]["columns"][0]["visible"] is False

    reset = client.post("/api/racing/layout/history", json={"reset": True})
    assert reset.status_code == 200
    assert all(c["visible"] for c in reset.get_json()["layout_settings"]["columns"])

    page = client.get("/racing/info?tab=history")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "racing-history-table" in html
    assert "/api/racing/history-records" in html
    assert 'id="racing-history-layout-mode"' in html
    assert "matches-layout-mode" in html
    assert "layout-zebra-controls" in html
    assert "儲存版面設定" in html
    assert "賽馬往績 — 版面設定" in html
    # Soccer-aligned single scrollport (nested overflow breaks sticky thead).
    assert "history-scroll .table-wrap" in html
    assert "overflow: visible" in html
    assert "border-collapse: separate" in html


def test_racing_layout_page_uses_shared_chrome_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa
    import hppv.racing_layout as rl

    db = tmp_path / "hppv.sqlite3"
    soccer = tmp_path / "soccer.sqlite3"
    _seed_minimal_racing_db(db)
    init_soccer_db(soccer)
    monkeypatch.setattr(wa, "DB_PATH", db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", soccer)
    monkeypatch.setattr(rl, "layout_settings_path", lambda: tmp_path / "racing_layout_settings.json")
    html = wa.app.test_client().get("/racing/info?tab=history").get_data(as_text=True)
    for needle in (
        "racing-layout-zebra-color",
        "racing-layout-zebra-enable",
        "layout-cols",
        "欄名說明",
        "排序方向",
        "拖曳",
        "UI 版面設定",
        "儲存版面設定",
        "layoutSortAsc",
        "layoutSortDesc",
        "HppvLayoutEditor",
    ):
        assert needle in html, needle
    assert "恢復預設" not in html
    assert "racing-layout-reset" not in html
    # Sort labels come from shared soccer.daily.layout.sort_* (may be \u-escaped in tojson).
    assert "layoutSortAsc:" in html and "layoutSortDesc:" in html
    assert "[soccer.daily.layout" not in html
    assert "[racing.history.layout.mode_title]" not in html
