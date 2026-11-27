"""Racing daily (racecard): active meeting + race cards; all cols default visible."""

from __future__ import annotations

from pathlib import Path

import pytest

from hppv.db import get_connection, init_db
from hppv.racing_daily import (
    fetch_racing_daily_filtered,
    fetch_racing_daily_race,
    list_available_race_dates,
    resolve_active_meeting,
)
from hppv.racing_download import ensure_racecard_db, update_finish_pos_for_meeting
from hppv.schema import RACECARD_FINISH_POS_COLUMN
from hppv.racing_layout import (
    LAYOUT_KEY_RACING_DAILY,
    LAYOUT_KEY_RACING_HISTORY,
    RACING_DAILY_COLUMN_CATALOG,
    default_layout_settings,
    get_layout_settings,
    save_layout_settings,
)
from hppv.soccer_db import init_soccer_db


def _seed_racecard(db: Path) -> None:
    init_db(db)
    with get_connection(db) as conn:
        for race_no, name in ((1, "測試賽一"), (2, "測試賽二")):
            conn.execute(
                """
                INSERT INTO racecard_race_info (
                  race_date, racecourse, race_no, event_name, distance, race_class,
                  post_time, race_course_area, track_name, meeting_venue
                ) VALUES (?, 'ST', ?, ?, '1200米', '第三班', '13:00', '草地', 'C', '沙田')
                """,
                ("2026/11/08", race_no, name),
            )
            for horse_no in (1, 2):
                conn.execute(
                    """
                    INSERT INTO racecard_starters (
                      race_date, racecourse, race_no, "後備", horse_id, "馬號", "馬名",
                      "檔位", "騎師", "練馬師", "負磅", "評分", "馬匹體重", "配備", "預測賠率", "即時賠率"
                    ) VALUES (
                      '2026/11/08', 'ST', ?, '否', ?, ?, ?,
                      '3', '騎師甲', '練馬師乙', '120', '60', '1100', 'B', '5.5', '6.0'
                    )
                    """,
                    (
                        race_no,
                        f"HK_2024_K{race_no}{horse_no:02d}",
                        str(horse_no),
                        f"測試馬{race_no}-{horse_no}",
                    ),
                )
        # Older meeting should not mix into active meta meeting.
        conn.execute(
            """
            INSERT INTO racecard_starters (
              race_date, racecourse, race_no, "後備", horse_id, "馬號", "馬名"
            ) VALUES ('2026/10/01', 'HV', 1, '否', 'HK_OLD', '1', '舊會馬')
            """
        )
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value) VALUES
              ('last_source_race_date', '2026/11/08'),
              ('last_source_racecourse_code', 'ST')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """
        )
        conn.commit()


def test_daily_catalog_all_default_visible() -> None:
    assert RACING_DAILY_COLUMN_CATALOG
    assert all(bool(c.get("default_visible")) for c in RACING_DAILY_COLUMN_CATALOG)
    assert any(c["key"] == "finish_pos" for c in RACING_DAILY_COLUMN_CATALOG)
    assert any(c["key"] == "same_distance_place_rate" for c in RACING_DAILY_COLUMN_CATALOG)
    settings = default_layout_settings(LAYOUT_KEY_RACING_DAILY)
    assert all(c["visible"] for c in settings["columns"])
    assert len(settings["columns"]) == len(RACING_DAILY_COLUMN_CATALOG)
    assert any(c["key"] == "finish_pos" for c in settings["columns"])
    assert any(c["key"] == "same_distance_place_rate" for c in settings["columns"])


def test_update_finish_pos_from_race_records(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed_racecard(db)
    ensure_racecard_db(db)
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO race_records (
              horse_id, race_date, venue, race_no, course, distance, going, class_band,
              draw, rating, handicap, jockey, trainer, finish_pos, running_pos,
              win_odds, time, prize
            ) VALUES (
              'HK_2024_K101', '08/11/26', '沙田', '1', '沙田草地"C"', '1200', '好',
              '第三班', '3', '60', '120', '騎師甲', '練馬師乙', '02', '',
              '5.5', '1.10.00', ''
            )
            """
        )
        n = update_finish_pos_for_meeting(conn, race_date="2026/11/08", racecourse="ST")
        conn.commit()
        assert n >= 1
        row = conn.execute(
            f"""
            SELECT "{RACECARD_FINISH_POS_COLUMN}" AS fp
              FROM racecard_starters
             WHERE horse_id = 'HK_2024_K101' AND race_no = 1
            """
        ).fetchone()
        assert row["fp"] == "02"
    view = fetch_racing_daily_race(db, race_no=1)
    assert any(r.get("finish_pos") == "02" for r in view.rows)


def test_daily_pref_isolated_from_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hppv.racing_layout as rl

    path = tmp_path / "racing_layout_settings.json"
    monkeypatch.setattr(rl, "layout_settings_path", lambda: path)
    daily = default_layout_settings(LAYOUT_KEY_RACING_DAILY)
    daily["columns"][0]["visible"] = False
    save_layout_settings(daily, layout_key=LAYOUT_KEY_RACING_DAILY)
    hist = get_layout_settings(LAYOUT_KEY_RACING_HISTORY)
    assert all(c["visible"] for c in hist["columns"])
    loaded = get_layout_settings(LAYOUT_KEY_RACING_DAILY)
    assert loaded["columns"][0]["visible"] is False


def test_resolve_active_meeting_uses_meta(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed_racecard(db)
    meeting = resolve_active_meeting(db)
    assert meeting is not None
    assert meeting.race_date == "2026/11/08"
    assert meeting.racecourse == "ST"
    assert meeting.source == "meta"


def test_list_available_race_dates_newest_first(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed_racecard(db)
    meetings = list_available_race_dates(db)
    assert len(meetings) == 2
    assert meetings[0]["race_date"] == "2026/11/08"
    assert meetings[0]["racecourse"] == "ST"
    assert meetings[1]["race_date"] == "2026/10/01"
    assert meetings[1]["racecourse"] == "HV"


def test_fetch_daily_race_isolated(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed_racecard(db)
    before = get_connection(db).execute("select count(*) c from racecard_starters").fetchone()["c"]
    view = fetch_racing_daily_race(db, race_no=1)
    assert view.selected_race_no == 1
    assert view.total_count == 2
    assert len(view.races) == 2
    assert all(r["race_no"] == "1" for r in view.rows)
    assert all("舊會馬" not in r["horse_name"] for r in view.rows)
    assert view.summary["event_name"] == "測試賽一"
    assert view.summary["race_course_area"] == "草地"
    assert view.summary["race_date"] == "2026-11-08"
    assert all(r["race_date"] == "2026-11-08" for r in view.rows)
    view2 = fetch_racing_daily_race(db, race_no=2)
    assert view2.total_count == 2
    assert all(r["race_no"] == "2" for r in view2.rows)
    assert view2.summary["event_name"] == "測試賽二"
    after = get_connection(db).execute("select count(*) c from racecard_starters").fetchone()["c"]
    assert before == after


def test_fetch_daily_legacy_filtered_still_works(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed_racecard(db)
    result = fetch_racing_daily_filtered(db, all_matches=True, q="測試馬1-1", q_op="contains")
    assert result.total_count >= 1


def test_api_racing_daily_race_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hppv import web_app as wa
    import hppv.racing_layout as rl

    db = tmp_path / "hppv.sqlite3"
    soccer = tmp_path / "soccer.sqlite3"
    _seed_racecard(db)
    init_soccer_db(soccer)
    monkeypatch.setattr(wa, "DB_PATH", db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", soccer)
    monkeypatch.setattr(rl, "layout_settings_path", lambda: tmp_path / "racing_layout_settings.json")

    client = wa.app.test_client()
    r = client.get("/api/records/racing/racing_daily")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["mode"] == "race"
    assert body["source"] == "racecard_starters"
    assert body["meeting"]["race_date"] == "2026/11/08"
    assert body["meeting"]["source"] == "meta"
    assert body["selected_race_no"] == 1
    assert len(body["races"]) == 2
    assert body["total_count"] == 2
    assert len(body["meetings"]) == 2
    assert body["meetings"][0]["race_date"] == "2026/11/08"
    assert all(c.get("default_visible") for c in body["catalog"])

    r2 = client.get("/api/records/racing/racing_daily?race_no=2")
    b2 = r2.get_json()
    assert b2["selected_race_no"] == 2
    assert b2["total_count"] == 2
    assert b2["summary"]["event_name"] == "測試賽二"

    r_old = client.get(
        "/api/records/racing/racing_daily?date=2026-10-01&racecourse=HV"
    )
    b_old = r_old.get_json()
    assert r_old.status_code == 200
    assert b_old["meeting"]["race_date"] == "2026/10/01"
    assert b_old["meeting"]["racecourse"] == "HV"
    assert b_old["meeting"]["source"] == "request"
    assert b_old["total_count"] == 1
    assert b_old["rows"][0]["horse_name"] == "舊會馬"

    page = client.get("/racing/info")
    html = page.get_data(as_text=True)
    assert "racing-daily-table" in html
    assert "racing-daily-race-cards" in html
    assert "racing-daily-meeting-select" in html
    assert "racing-daily-all-records" not in html
    assert "racing-daily-search" not in html
    assert "racing-daily-pager" not in html
    assert "馬匹往績" in html
    assert "每日賽事 — 版面設定" in html

    export = client.post(
        "/api/racing/daily-export",
        json={
            "file_type": "csv",
            "filename": "t.csv",
            "date": "2026-11-08",
            "racecourse": "ST",
            "race_no": 1,
            "columns": [{"key": "horse_name", "width": 120}, {"key": "race_no", "width": 60}],
            "lang": "zh-Hant",
        },
    )
    assert export.status_code == 200
    assert int(export.headers.get("X-HPPV-Export-Rows", "0")) == 2
    text = export.get_data(as_text=True)
    assert "測試馬1-1" in text
    assert "測試馬2-1" not in text

    bad = client.post(
        "/api/racing/daily-export",
        json={
            "file_type": "csv",
            "filename": "t.csv",
            "columns": [{"key": "horse_name", "width": 120}],
            "lang": "zh-Hant",
        },
    )
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "no_selected_race"
