"""Same-day racing results → race_records (INSERT OR IGNORE)."""

from __future__ import annotations

from pathlib import Path

from hppv.db import fetch_race_records, get_connection, init_db
from hppv.racing_download import run_daily_results_download
from hppv.racing_hkjc_graphql import (
    RACE_MEETINGS_QUERY,
    horse_rows_from_meeting,
    race_records_from_meeting,
)
from hppv.racing_requests import (
    REQUEST_KEY_DAILY_RESULTS,
    RacingRequestPresetError,
    RacingRequestPayload,
    assert_payload_matches_whitelist,
    ensure_racing_preset_files,
    expected_query_for_preset,
    load_racing_request_preset,
)


def _finished_meeting() -> dict:
    return {
        "date": "2026-11-16",
        "venueCode": "ST",
        "status": "RESULT",
        "races": [
            {
                "no": 1,
                "status": "RESULT",
                "distance": 1200,
                "go_ch": "好",
                "raceClass_ch": "第四班",
                "raceTrack": {"description_ch": "草地"},
                "raceCourse": {
                    "description_ch": '"C" 賽道',
                    "displayCode": "C",
                },
                "runners": [
                    {
                        "no": "1",
                        "status": "declared",
                        "finalPosition": 1,
                        "deadHeat": False,
                        "winOdds": "3.5",
                        "barrierDrawNumber": "5",
                        "currentRating": "062",
                        "name_ch": "測試一",
                        "name_en": "TEST ONE",
                        "horse": {"id": "HK_2024_T001", "code": "T001"},
                        "jockey": {"name_ch": "騎師甲"},
                        "trainer": {"name_ch": "練馬師甲"},
                    },
                    {
                        "no": "2",
                        "status": "declared",
                        "finalPosition": 0,
                        "horse": {"id": "HK_2024_T002", "code": "T002"},
                        "jockey": {"name_ch": "騎師乙"},
                        "trainer": {"name_ch": "練馬師乙"},
                    },
                    {
                        "no": "3",
                        "status": "standby",
                        "standbyNo": "1",
                        "finalPosition": 2,
                        "horse": {"id": "HK_2024_T003", "code": "T003"},
                        "jockey": {"name_ch": "騎師丙"},
                        "trainer": {"name_ch": "練馬師丙"},
                    },
                    {
                        "no": "4",
                        "status": "declared",
                        "finalPosition": "WV",
                        "name_ch": "退出馬",
                        "name_en": "WITHDRAWN HORSE",
                        "horse": {"id": "HK_2024_T004", "code": "T004"},
                        "jockey": {"name_ch": "騎師丁"},
                        "trainer": {"name_ch": "練馬師丁"},
                    },
                ],
            }
        ],
    }


def test_race_records_from_meeting_only_finished() -> None:
    rows = race_records_from_meeting(_finished_meeting())
    assert len(rows) == 2
    by_id = {r.horse_id: r for r in rows}
    r = by_id["HK_2024_T001"]
    assert r.race_date == "16/11/26"
    assert r.venue == "沙田"
    assert r.race_no == "1"
    assert r.course == '沙田草地"C"'
    assert r.distance == "1200"
    assert r.going == "好"
    assert r.class_band == "第四班"
    assert r.draw == "5"
    assert r.rating == "062"
    assert r.jockey == "騎師甲"
    assert r.trainer == "練馬師甲"
    assert r.finish_pos == "01"
    assert r.win_odds == "3.5"
    assert r.running_pos == ""
    assert r.time == ""
    assert r.prize == ""
    assert by_id["HK_2024_T004"].finish_pos == "WV"


def test_horse_rows_from_meeting_keeps_wv_name() -> None:
    rows = horse_rows_from_meeting(_finished_meeting())
    by_id = {r["horse_id"]: r for r in rows}
    assert set(by_id) == {"HK_2024_T001", "HK_2024_T004"}
    assert by_id["HK_2024_T004"]["name_zh"] == "退出馬"
    assert by_id["HK_2024_T004"]["name_en"] == "WITHDRAWN HORSE"
    assert by_id["HK_2024_T004"]["brand_no"] == "T004"


def test_daily_results_insert_then_ignore_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    meeting = _finished_meeting()

    def fake_fetch(date: str, venue: str, **kwargs):
        return {"raceMeetings": [meeting]}

    monkeypatch.setattr(
        "hppv.racing_download.fetch_race_meetings", fake_fetch
    )
    monkeypatch.setattr(
        "hppv.racing_download.resolve_meeting_for_download",
        lambda **kwargs: type(
            "M",
            (),
            {
                "date_iso": "2026-11-16",
                "venue_code": "ST",
                "status": "RESULT",
                "total_number_of_race": 1,
            },
        )(),
    )

    first = run_daily_results_download(db)
    assert first.code == "success"
    assert first.progress.updated == 2
    assert first.progress.skipped == 0
    assert first.detail.get("horses_filled") == 2
    assert len(fetch_race_records(db, "HK_2024_T001")) == 1
    assert len(fetch_race_records(db, "HK_2024_T004")) == 1
    with get_connection(db) as conn:
        wv = conn.execute(
            "SELECT name_zh, name_en, brand_no FROM horses WHERE horse_id = ?",
            ("HK_2024_T004",),
        ).fetchone()
    assert wv is not None
    assert wv["name_zh"] == "退出馬"
    assert wv["name_en"] == "WITHDRAWN HORSE"
    assert wv["brand_no"] == "T004"

    second = run_daily_results_download(db)
    assert second.code == "success"
    assert second.progress.updated == 0
    assert second.progress.skipped == 2
    assert second.detail.get("horses_filled") == 0
    assert len(fetch_race_records(db, "HK_2024_T001")) == 1


def test_daily_results_skips_when_complete_period_row_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """Sparse GraphQL daily must not twin a complete localresults row for the day."""
    from hppv.db import upsert_race_records
    from hppv.models import RaceRecord

    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    upsert_race_records(
        db,
        [
            RaceRecord(
                horse_id="HK_2024_T001",
                race_date="16/11/26",
                venue="沙田",
                race_no="200",
                course='沙田草地"C"',
                distance="1200",
                going="好",
                class_band="第四班",
                draw="5",
                rating="062",
                handicap="126",
                jockey="騎師甲",
                trainer="練馬師甲",
                finish_pos="01",
                running_pos="1 1 1",
                win_odds="3.5",
                time="1:09.88",
                prize="1000000",
            )
        ],
    )

    meeting = _finished_meeting()

    def fake_fetch(date: str, venue: str, **kwargs):
        return {"raceMeetings": [meeting]}

    monkeypatch.setattr("hppv.racing_download.fetch_race_meetings", fake_fetch)
    monkeypatch.setattr(
        "hppv.racing_download.resolve_meeting_for_download",
        lambda **kwargs: type(
            "M",
            (),
            {
                "date_iso": "2026-11-16",
                "venue_code": "ST",
                "status": "RESULT",
                "total_number_of_race": 1,
            },
        )(),
    )

    result = run_daily_results_download(db)
    assert result.code == "success"
    # T001 already complete → skip; T004 WV sparse still inserts.
    assert result.progress.updated == 1
    assert result.progress.skipped == 1
    rows = fetch_race_records(db, "HK_2024_T001")
    assert len(rows) == 1
    assert rows[0].race_no == "200"
    assert rows[0].time == "1:09.88"


def test_period_replaces_sparse_daily_duplicate(tmp_path: Path) -> None:
    from hppv.db import get_connection, upsert_race_records
    from hppv.models import RaceRecord

    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    # Legacy GraphQL-style sparse row (old YYYY/MM/DD + meeting race_no).
    upsert_race_records(
        db,
        [
            RaceRecord(
                horse_id="HK_2024_T001",
                race_date="2026/11/16",
                venue="沙田",
                race_no="1",
                course='沙田草地"C"',
                distance="1200",
                going="好",
                class_band="第四班",
                draw="5",
                rating="062",
                handicap="",
                jockey="騎師甲",
                trainer="練馬師甲",
                finish_pos="01",
                running_pos="",
                win_odds="3.5",
                time="",
                prize="",
            )
        ],
    )
    n = upsert_race_records(
        db,
        [
            RaceRecord(
                horse_id="HK_2024_T001",
                race_date="16/11/26",
                venue="沙田",
                race_no="200",
                course='沙田草地"C"',
                distance="1200",
                going="好",
                class_band="第四班",
                draw="5",
                rating="062",
                handicap="126",
                jockey="騎師甲",
                trainer="練馬師甲",
                finish_pos="01",
                running_pos="1 1 1",
                win_odds="3.5",
                time="1:09.88",
                prize="1000000",
            )
        ],
    )
    assert n == 1
    with get_connection(db) as conn:
        rows = conn.execute(
            "SELECT race_date, race_no, time FROM race_records WHERE horse_id=?",
            ("HK_2024_T001",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["race_date"] == "16/11/26"
    assert rows[0]["race_no"] == "200"
    assert rows[0]["time"] == "1:09.88"


def test_daily_results_preset_whitelist(tmp_path: Path) -> None:
    requests_dir = tmp_path / "racing_requests"
    ensure_racing_preset_files(requests_dir)
    assert expected_query_for_preset(REQUEST_KEY_DAILY_RESULTS) == RACE_MEETINGS_QUERY
    payload, _ = load_racing_request_preset(requests_dir, REQUEST_KEY_DAILY_RESULTS)
    assert_payload_matches_whitelist(REQUEST_KEY_DAILY_RESULTS, payload)
    assert payload.operation_name == "raceMeetings"
    # Reject slimmed query
    bad = RacingRequestPayload(
        query="query raceMeetings { raceMeetings { id } }",
        variables={"date": None, "venueCode": None},
        operation_name="raceMeetings",
    )
    try:
        assert_payload_matches_whitelist(REQUEST_KEY_DAILY_RESULTS, bad)
        raise AssertionError("expected query_not_whitelisted")
    except RacingRequestPresetError as e:
        assert e.code == "query_not_whitelisted"
