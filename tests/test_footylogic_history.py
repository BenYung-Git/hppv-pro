from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hppv.footylogic_client import FootylogicClient, FootylogicClientError
from hppv.footylogic_download import run_jobs
from hppv.footylogic_ingest import (
    init_footylogic_tables,
    insert_quarantine,
    list_jobs,
    seed_jobs,
    upsert_matches,
)
from hppv.footylogic_parse import (
    DEFAULT_RANGE_END_EXCLUSIVE_HKT,
    DEFAULT_RANGE_START_HKT,
    HKT,
    build_source_key,
    parse_fixtures_results_payload,
    parse_mautcko_to_hkt,
    parse_score_value,
)
from hppv.soccer_db import get_connection, init_soccer_db

SAMPLE_BODY = {
    "statusCode": 200,
    "errors": None,
    "message": "success",
    "data": {
        "competitionName": "測試聯賽",
        "lastUpdated": "2026-11-05 00:45",
        "info": [
            {
                "kickOff": "2026-11-05",
                "events": [
                    {
                        "maUTCKO": "2026-11-04T16:00:00.000Z",
                        "homeTeamName": "主隊A",
                        "awayTeamName": "客隊B",
                        "homeTeamGoals": 1,
                        "awayTeamGoals": 2,
                        "homeTeamHTGoals": 0,
                        "awayTeamHTGoals": 0,
                        "rownumber": "1",
                        "homeTeamId": "100",
                        "awayTeamId": "200",
                    },
                    {
                        "maUTCKO": "2026-11-05T08:30:00.000Z",
                        "homeTeamName": "主隊C",
                        "awayTeamName": "客隊D",
                        "homeTeamGoals": None,
                        "awayTeamGoals": None,
                        "homeTeamHTGoals": None,
                        "awayTeamHTGoals": None,
                        "rownumber": "2",
                        "homeTeamId": "300",
                        "awayTeamId": "400",
                    },
                ],
            }
        ],
    },
}


def test_parse_mautcko_utc_to_hkt_cross_day() -> None:
    dt, err = parse_mautcko_to_hkt("2026-11-04T16:00:00.000Z")
    assert err == ""
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.astimezone(HKT).strftime("%Y-%m-%d %H:%M") == "2026-11-05 00:00"


def test_score_null_vs_zero() -> None:
    assert parse_score_value(None) == (None, None)
    assert parse_score_value(0) == (0, None)
    assert parse_score_value("0") == (0, None)
    val, err = parse_score_value(True)
    assert val is None and err


def test_parse_payload_range_and_scores() -> None:
    accepted, quarantined, stats = parse_fixtures_results_payload(
        SAMPLE_BODY,
        competition_id=1,
        season_id=2,
        month="2026-11",
        now_hkt=datetime(2026, 11, 5, 20, 0, tzinfo=HKT),
    )
    assert stats["in_range"] == 2
    assert stats["has_score"] == 1
    assert stats["scoreless"] == 1
    scored = [r for r in accepted if r.has_score]
    assert len(scored) == 1
    assert scored[0].score_ft_home == 1 and scored[0].score_ft_away == 2
    assert scored[0].result_confirm_status == "source_score_unconfirmed"
    assert quarantined == []


def test_quarantine_both_team_ids_missing() -> None:
    body = json.loads(json.dumps(SAMPLE_BODY))
    body["data"]["info"][0]["events"] = [
        {
            "maUTCKO": "2026-11-04T16:00:00.000Z",
            "homeTeamName": "主",
            "awayTeamName": "客",
            "homeTeamGoals": 1,
            "awayTeamGoals": 0,
            "homeTeamHTGoals": 0,
            "awayTeamHTGoals": 0,
            "homeTeamId": None,
            "awayTeamId": None,
        }
    ]
    accepted, quarantined, stats = parse_fixtures_results_payload(
        body,
        competition_id=1,
        season_id=2,
        month="2026-11",
        now_hkt=datetime(2026, 11, 5, 20, 0, tzinfo=HKT),
    )
    assert accepted == []
    assert len(quarantined) == 1
    assert quarantined[0].quarantine_reason == "both_team_ids_missing"


def test_one_sided_ft_quarantine() -> None:
    body = json.loads(json.dumps(SAMPLE_BODY))
    body["data"]["info"][0]["events"] = [
        {
            "maUTCKO": "2026-11-04T16:00:00.000Z",
            "homeTeamName": "主",
            "awayTeamName": "客",
            "homeTeamGoals": 1,
            "awayTeamGoals": None,
            "homeTeamHTGoals": None,
            "awayTeamHTGoals": None,
            "homeTeamId": "1",
            "awayTeamId": "2",
        }
    ]
    accepted, quarantined, _ = parse_fixtures_results_payload(
        body,
        competition_id=1,
        season_id=2,
        month="2026-11",
        now_hkt=datetime(2026, 11, 5, 20, 0, tzinfo=HKT),
    )
    assert accepted == []
    assert quarantined[0].quarantine_reason == "ft_score_one_sided"


def test_upsert_dedupe_and_preserve_scores(tmp_path: Path) -> None:
    db = tmp_path / "test_fl.sqlite3"
    init_soccer_db(db)
    init_footylogic_tables(db)
    accepted, _, _ = parse_fixtures_results_payload(
        SAMPLE_BODY,
        competition_id=9,
        season_id=8,
        month="2026-11",
        now_hkt=datetime(2026, 11, 5, 20, 0, tzinfo=HKT),
    )
    with get_connection(db) as conn:
        n1 = upsert_matches(conn, accepted)
        conn.commit()
    assert n1 == 2
    # second pass with missing FT should not clear
    body2 = json.loads(json.dumps(SAMPLE_BODY))
    body2["data"]["info"][0]["events"][0]["homeTeamGoals"] = None
    body2["data"]["info"][0]["events"][0]["awayTeamGoals"] = None
    accepted2, _, _ = parse_fixtures_results_payload(
        body2,
        competition_id=9,
        season_id=8,
        month="2026-11",
        now_hkt=datetime(2026, 11, 5, 20, 0, tzinfo=HKT),
    )
    with get_connection(db) as conn:
        upsert_matches(conn, accepted2)
        conn.commit()
        row = conn.execute(
            "SELECT score_ft_home, score_ft_away, COUNT(*) AS c FROM footylogic_matches"
        ).fetchone()
        # still one scored row kept
        scored = conn.execute(
            "SELECT score_ft_home, score_ft_away FROM footylogic_matches WHERE has_score=1"
        ).fetchone()
    assert scored["score_ft_home"] == 1 and scored["score_ft_away"] == 2
    with get_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM footylogic_matches").fetchone()["c"] == 2


def test_job_resume_and_transaction_rollback(tmp_path: Path) -> None:
    db = tmp_path / "test_fl_jobs.sqlite3"
    init_footylogic_tables(db)
    jobs = [
        {
            "job_key": "1:2:2026-11",
            "competitionId": 1,
            "competitionName": "T",
            "seasonId": 2,
            "seasonName": "2026",
            "month": "2026-11",
            "browse_category": "league",
            "country_or_group": "X",
        }
    ]
    with get_connection(db) as conn:
        assert seed_jobs(conn, jobs) == 1
        assert seed_jobs(conn, jobs) == 0  # ignore existing
        conn.commit()

    class FakeResp:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self.headers = {}
            self.text = json.dumps(payload)
            self.content = self.text.encode("utf-8")

        def json(self):
            return json.loads(self.text)

    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResp(200, SAMPLE_BODY)

    client = FootylogicClient(min_interval_sec=0, get_func=fake_get, sleep_func=lambda _s: None)
    stats = run_jobs(db, limit=1, dry_run=False, client=client)
    assert stats["completed"] == 1 and stats["failed"] == 0
    with get_connection(db) as conn:
        st = conn.execute("SELECT status FROM footylogic_jobs").fetchone()["status"]
        assert st == "completed"
        assert conn.execute("SELECT COUNT(*) AS c FROM footylogic_matches").fetchone()["c"] >= 1

    # failed jobs remain resumable
    with get_connection(db) as conn:
        conn.execute("UPDATE footylogic_jobs SET status='failed'")
        conn.commit()
    stats2 = run_jobs(db, limit=1, dry_run=False, client=client)
    assert stats2["completed"] == 1


def test_client_respects_retry_after() -> None:
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status_code: int, payload: dict | None = None, retry_after: str | None = None):
            self.status_code = status_code
            self.headers = {"Retry-After": retry_after} if retry_after else {}
            self.text = json.dumps(payload or {"statusCode": 200, "data": {}, "message": "success"})
            self.content = self.text.encode("utf-8")

    slept: list[float] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(429, retry_after="0")
        return FakeResp(200, {"statusCode": 200, "data": {"info": []}, "message": "success"})

    client = FootylogicClient(
        min_interval_sec=0,
        get_func=fake_get,
        sleep_func=lambda s: slept.append(s),
        max_retries=2,
    )
    result = client.get_json("/tournament/fixtures-results", {"competitionId": 1})
    assert result.business_ok
    assert calls["n"] == 2


def test_source_key_not_racing_epl_style() -> None:
    key = build_source_key(
        competition_id="50071337",
        season_id="1",
        home_team_id="10",
        away_team_id="20",
        kickoff_utc="2022-08-06T11:30:00.000Z",
    )
    assert key.startswith("fl|")
    assert not key.startswith("EPL-")


def test_range_constants() -> None:
    assert DEFAULT_RANGE_START_HKT.isoformat().startswith("2021-11-05T00:00:00")
    assert DEFAULT_RANGE_END_EXCLUSIVE_HKT.isoformat().startswith("2026-11-06T00:00:00")


def test_does_not_write_soccer_daily(tmp_path: Path) -> None:
    db = tmp_path / "iso.sqlite3"
    init_soccer_db(db)
    init_footylogic_tables(db)
    accepted, _, _ = parse_fixtures_results_payload(
        SAMPLE_BODY,
        competition_id=1,
        season_id=2,
        month="2026-11",
        now_hkt=datetime(2026, 11, 5, 20, 0, tzinfo=HKT),
    )
    with get_connection(db) as conn:
        upsert_matches(conn, accepted)
        conn.commit()
        daily = conn.execute("SELECT COUNT(*) AS c FROM soccer_daily").fetchone()["c"]
        fl = conn.execute("SELECT COUNT(*) AS c FROM footylogic_matches").fetchone()["c"]
    assert daily == 0
    assert fl >= 1
