"""Racing horse form: same-distance / recent / place + handicap stats (read-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hppv.db import get_connection, init_db
from hppv.racing_download import ensure_racecard_db
from hppv.racing_horse_form import (
    DEFAULT_HORSE_FORM_LIMIT,
    HORSE_FORM_LIMIT,
    HORSE_FORM_LIMIT_OPTIONS,
    HorseFormValidationError,
    classify_finish_pos,
    compute_same_venue_distance_place_rate_pct_points,
    compute_same_venue_distance_win_place_pct_points,
    fetch_horse_form,
    is_awt_course,
    normalize_distance,
    normalize_horse_form_limit,
    parse_anchor,
    parse_handicap_pounds,
    summarize_handicap_groups,
    summarize_places,
    summarize_win_counts,
    update_hv_same_distance_win_rates_for_meeting,
)
from hppv.schema import (
    RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN,
    RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN,
)


def _seed(db: Path) -> None:
    init_db(db)
    with get_connection(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO horses (horse_id, brand_no, name_zh, name_en, status) "
            "VALUES ('HK_2024_T100', 'T100', '測試馬', 'Test Horse', '現役')"
        )
        # horse_id, date, venue, race_no, distance, class, draw, handicap, jockey,
        # finish, running, win_odds, time
        rows = [
            ("HK_2024_T100", "01/01/2026", "沙田", "1", "1200", "第四班", "3", "126", "騎甲", "01", "3 2 1", "4.5", "1:09.50"),
            ("HK_2024_T100", "10/01/2026", "跑馬地", "2", "1200", "第四班", "5", "125", "騎乙", "05", "5 5 5", "12", "1:10.10"),
            ("HK_2024_T100", "20/01/2026", "沙田", "3", "1400", "第三班", "2", "128", "騎丙", "02", "2 2 2", "6.0", "1:22.00"),
            ("HK_2024_T100", "01/02/2026", "沙田", "4", "1200", "第四班", "1", "124", "騎丁", "WV", "", "", ""),
            ("HK_2024_T100", "10/02/2026", "沙田", "5", "1200", "第四班", "7", "123", "騎戊", "03", "4 4 3", "8.1", "1:09.90"),
            ("HK_2024_T100", "15/02/2026", "跑馬地", "6", "1000", "第五班", "8", "120", "騎己", "08", "8 8 8", "20", "0:57.00"),
            ("HK_2024_T100", "01/03/2026", "沙田", "1", "1200", "第四班", "4", "122", "騎庚", "01", "1 1 1", "3.0", "1:09.20"),
            ("HK_2024_T100", "05/03/2026", "沙田", "2", "1200", "第四班", "6", "121", "騎辛", "04", "6 6 4", "9.0", "1:10.00"),
        ]
        for r in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO race_records (
                  horse_id, race_date, venue, race_no, course, distance, going, class_band,
                  draw, rating, handicap, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
                ) VALUES (?, ?, ?, ?, '草地', ?, '好', ?, ?, '', ?, ?, '練甲', ?, ?, ?, ?, '')
                """,
                r,
            )
        # One AWT start at ST (counts under AWT, not ST turf); distance ≠ 1200
        # so same-venue-distance samples stay unchanged for other tests.
        conn.execute(
            """
            INSERT OR IGNORE INTO race_records (
              horse_id, race_date, venue, race_no, course, distance, going, class_band,
              draw, rating, handicap, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
            ) VALUES (
              'HK_2024_T100', '25/02/2026', '沙田', '7', '全天候', '1650', '好', '第四班',
              '2', '', '122', '騎壬', '練甲', '06', '6 6 6', '15', '1:39.50', ''
            )
            """
        )
        conn.commit()


def test_shared_limit_settings() -> None:
    assert HORSE_FORM_LIMIT == 12
    assert HORSE_FORM_LIMIT_OPTIONS == (12,)
    assert DEFAULT_HORSE_FORM_LIMIT == 12
    assert normalize_horse_form_limit(None) == 12
    assert normalize_horse_form_limit("") == 12
    assert normalize_horse_form_limit(12) == 12
    assert normalize_horse_form_limit("12") == 12
    with pytest.raises(HorseFormValidationError):
        normalize_horse_form_limit(10)
    with pytest.raises(HorseFormValidationError):
        normalize_horse_form_limit(5)


def test_normalize_and_classify() -> None:
    assert normalize_distance("1200米") == "1200"
    assert classify_finish_pos("01") == "first"
    assert classify_finish_pos("04") == "fourth"
    assert classify_finish_pos("05") == "other"
    assert classify_finish_pos("WV") == "withdrawn"
    assert classify_finish_pos("WV-A") == "withdrawn"
    assert classify_finish_pos("PU") == "unknown"
    assert parse_handicap_pounds("124") == 124
    assert parse_handicap_pounds("—") is None
    assert parse_handicap_pounds("") is None
    assert parse_handicap_pounds("124.5") is None


def test_same_distance_and_recent_limits(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed(db)
    payload = fetch_horse_form(
        db,
        horse_id="HK_2024_T100",
        race_date="2026-03-01",
        venue="沙田",
        distance="1200",
        handicap="124",
        recent_limit=12,
    )
    assert payload["limit"] == 12
    assert payload["limit_options"] == [12]
    assert payload["recent_limit"] == 12
    assert payload["recent_limit_options"] == ["all", 12, 20, 30]
    same = payload["same_distance"]
    assert len(same) <= 12
    assert all(normalize_distance(r["distance"]) == "1200" for r in same)
    # Newest before 2026-03-01 at 1200 should include Feb 10 then Feb 1 (WV) …
    assert same[0]["race_date"] == "2026-02-10"
    assert same[0]["handicap"] == "123"
    assert same[0]["running_pos"] == "4 4 3"
    assert same[0]["time"] == "1:09.90"
    assert same[0]["win_odds"] == "8.1"
    assert "2026-03-01" not in {r["race_date"] for r in same}
    assert "2026-03-05" not in {r["race_date"] for r in same}

    recent12 = payload["recent"]
    assert len(recent12) <= 12
    recent_all = fetch_horse_form(
        db,
        horse_id="HK_2024_T100",
        race_date="2026-03-01",
        venue="沙田",
        distance="1200",
        handicap="124",
        recent_limit="all",
    )["recent"]
    assert len(recent_all) >= len(recent12)


def test_other_stats_hv_st_turf_awt_exclusive(tmp_path: Path) -> None:
    assert is_awt_course("全天候") is True
    assert is_awt_course("草地") is False
    db = tmp_path / "hppv.sqlite3"
    _seed(db)
    payload = fetch_horse_form(
        db,
        horse_id="HK_2024_T100",
        race_date="2026-03-01",
        venue="沙田",
        distance="1200",
        handicap="124",
        recent_limit=12,
    )
    other = payload["other_stats"]
    # HV before anchor: 10/01 (5th), 15/02 (8th) → 2/2, other=2
    assert other["hv"]["code"] == "HV"
    assert other["hv"]["valid_played"] == 2
    assert other["hv"]["total"] == 2
    assert other["hv"]["other"] == 2
    assert other["hv"]["first"] == 0
    # ST turf (not AWT): 01/01(1), 20/01(2), 01/02(WV), 10/02(3) → valid 3 / total 4
    assert other["st"]["code"] == "ST"
    assert other["st"]["valid_played"] == 3
    assert other["st"]["total"] == 4
    assert other["st"]["first"] == 1
    assert other["st"]["second"] == 1
    assert other["st"]["third"] == 1
    assert other["st"]["withdrawn"] == 1
    # AWT: 25/02 (6th) → 1/1 other; must not also count under ST
    assert other["awt"]["code"] == "AWT"
    assert other["awt"]["valid_played"] == 1
    assert other["awt"]["total"] == 1
    assert other["awt"]["other"] == 1


def test_summary_wv_not_in_valid(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed(db)
    payload = fetch_horse_form(
        db,
        horse_id="HK_2024_T100",
        race_date="2026-03-01",
        venue="沙田",
        distance="1200",
        handicap="124",
        recent_limit=12,
    )
    recent = payload["recent"]
    assert any(r["finish_pos"] == "WV" for r in recent)
    summary = payload["summary"]
    assert summary["withdrawn"] >= 1
    assert summary["valid_played"] == (
        summary["first"]
        + summary["second"]
        + summary["third"]
        + summary["fourth"]
        + summary["other"]
    )
    assert summary["win_rate"] != "—"
    empty = summarize_places([])
    assert empty["valid_played"] == 0
    assert empty["win_rate"] == "—"
    assert empty["place_rate"] == "—"


def test_handicap_groups_nonexclusive_and_shared_win_counts(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed(db)
    payload = fetch_horse_form(
        db,
        horse_id="HK_2024_T100",
        race_date="2026-03-01",
        venue="沙田",
        distance="1200",
        handicap="124",
        recent_limit=12,
    )
    hs = payload["handicap_stats"]
    assert hs["today_handicap"] == "124"
    assert hs["today_handicap_valid"] is True
    # Handicap splits use 同場同程 sample (not recent-all).
    # ST+1200 before 2026-03-01: 126/01, 124/WV, 123/03 → ge valid 1 win 1; le valid 1 win 0.
    assert hs["ge"]["wins"] == 1
    assert hs["ge"]["valid_played"] == 1
    assert hs["ge"]["win_rate"] == 1.0
    assert hs["le"]["wins"] == 0
    assert hs["le"]["valid_played"] == 1
    assert hs["le"]["win_rate"] == 0.0
    shared = summarize_win_counts(payload["recent"])
    assert shared["wins"] == payload["summary"]["first"]
    assert shared["valid_played"] == payload["summary"]["valid_played"]


def test_handicap_missing_today_no_invent() -> None:
    rows = [
        {
            "finish_pos": "01",
            "place_bucket": "first",
            "handicap": "126",
        }
    ]
    hs = summarize_handicap_groups(rows, today_handicap="")
    assert hs["today_handicap"] == "—"
    assert hs["today_handicap_valid"] is False
    assert hs["ge"]["wins"] == 0
    assert hs["ge"]["valid_played"] == 0
    assert hs["ge"]["win_rate"] == "—"
    assert hs["le"]["win_rate"] == "—"


def test_same_venue_distance_place_rate_and_local_write(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    _seed(db)
    ensure_racecard_db(db)
    with get_connection(db) as conn:
        anchor = parse_anchor(
            horse_id="HK_2024_T100",
            race_date="2026-03-10",
            venue="跑馬地",
            distance="1200",
            race_no="1",
        )
        place_pct = compute_same_venue_distance_place_rate_pct_points(conn, anchor)
        win_pct, place_pct2 = compute_same_venue_distance_win_place_pct_points(
            conn, anchor
        )
        assert place_pct == place_pct2
        assert win_pct is not None
        assert place_pct is not None
        # HV history for 1200 before 2026-03-10: one row finish 05 → place rate 0.
        assert place_pct == 0.0

        conn.execute(
            """
            INSERT INTO racecard_race_info (
              race_date, racecourse, race_no, distance, race_class
            ) VALUES ('2026/03/10', 'HV', 1, '1200米', '第四班')
            """
        )
        conn.execute(
            """
            INSERT INTO racecard_race_info (
              race_date, racecourse, race_no, distance, race_class
            ) VALUES ('2026/03/10', 'ST', 1, '1200米', '第四班')
            """
        )
        conn.execute(
            """
            INSERT INTO racecard_starters (
              race_date, racecourse, race_no, "後備", horse_id, "馬號", "馬名"
            ) VALUES ('2026/03/10', 'HV', 1, '否', 'HK_2024_T100', '1', '測試馬')
            """
        )
        conn.execute(
            """
            INSERT INTO racecard_starters (
              race_date, racecourse, race_no, "後備", horse_id, "馬號", "馬名"
            ) VALUES ('2026/03/10', 'ST', 1, '否', 'HK_2024_T100', '1', '測試馬ST')
            """
        )
        n_hv = update_hv_same_distance_win_rates_for_meeting(
            conn, race_date="2026/03/10", racecourse="HV"
        )
        n_st = update_hv_same_distance_win_rates_for_meeting(
            conn, race_date="2026/03/10", racecourse="ST"
        )
        n_foreign = update_hv_same_distance_win_rates_for_meeting(
            conn, race_date="2026/03/10", racecourse="S1"
        )
        conn.commit()
        assert n_hv == 1
        assert n_st == 1
        assert n_foreign == 0
        hv = conn.execute(
            f"""
            SELECT "{RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN}" AS w,
                   "{RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN}" AS p
              FROM racecard_starters
             WHERE racecourse = 'HV' AND horse_id = 'HK_2024_T100'
            """
        ).fetchone()
        st = conn.execute(
            f"""
            SELECT "{RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN}" AS w,
                   "{RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN}" AS p
              FROM racecard_starters
             WHERE racecourse = 'ST' AND horse_id = 'HK_2024_T100'
            """
        ).fetchone()
        assert hv["p"] == "0"
        assert hv["w"] is not None
        # ST+1200 before 2026-03-10: wins 2 / valid 4 → 50
        assert st["w"] == "50"
        assert st["p"] is not None
