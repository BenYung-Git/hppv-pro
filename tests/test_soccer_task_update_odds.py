"""update_odds task scheduler: settings, isolation, shared lock, ingest rules."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_graphql import SoccerGraphQLSendResult
from hppv.soccer_ingest import IngestResult, update_odds_for_existing_matches
from hppv.soccer_task_scheduler import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    TASK_KEY_UPDATE_MATCHES,
    TASK_KEY_UPDATE_ODDS,
    UpdateMatchesScheduler,
    UpdateOddsScheduler,
    _SHARED_JOB_LOCK,
    _upsert_state,
    get_update_matches_state,
    get_update_odds_state,
    restore_update_odds_on_startup,
    save_update_matches_settings,
    save_update_odds_settings,
)


@pytest.fixture()
def soccer_db(tmp_path: Path) -> Path:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    return db


def _insert_match(
    db: Path,
    *,
    match_id: str = "M1",
    home_odds: float | None = 1.5,
    draw_odds: float | None = 3.2,
    away_odds: float | None = 4.0,
    home_team: str = "主隊甲",
    away_team: str = "客隊乙",
) -> None:
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO soccer_daily (
                match_id, match_code, match_date, kickoff_at,
                competition_code, competition_name_zh,
                home_team, away_team, match_status,
                home_odds, draw_odds, away_odds, odds_status,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                match_id,
                "FB1",
                "2026-11-12",
                "2026-11-12T20:00:00+08:00",
                "EPL",
                "英超",
                home_team,
                away_team,
                "PreMatch",
                home_odds,
                draw_odds,
                away_odds,
                "Defined",
                "2026-11-12T00:00:00+00:00",
                "2026-11-12T00:00:00+00:00",
            ),
        )


def _match_payload(
    match_id: str,
    *,
    home: float = 1.8,
    draw: float = 3.5,
    away: float = 4.2,
    home_name: str = "來源主隊",
    away_name: str = "來源客隊",
) -> dict:
    return {
        "id": match_id,
        "frontEndId": "FB9",
        "kickOffTime": "2026-11-12T12:00:00.000Z",
        "status": "PreMatch",
        "homeTeam": {"name_ch": home_name},
        "awayTeam": {"name_ch": away_name},
        "tournament": {"code": "X", "name_ch": "聯賽"},
        "foPools": [
            {
                "oddsType": "HAD",
                "status": "Defined",
                "lines": [
                    {
                        "main": True,
                        "combinations": [
                            {"str": "H", "currentOdds": str(home)},
                            {"str": "D", "currentOdds": str(draw)},
                            {"str": "A", "currentOdds": str(away)},
                        ],
                    }
                ],
            }
        ],
    }


def test_odds_default_disabled(soccer_db: Path) -> None:
    state = get_update_odds_state(soccer_db)
    assert state.enabled is False
    assert state.next_run_at is None
    assert state.task_key == TASK_KEY_UPDATE_ODDS


def test_odds_settings_independent_from_matches(soccer_db: Path) -> None:
    save_update_matches_settings(
        soccer_db, enabled=True, interval_value=3, interval_unit="mins"
    )
    save_update_odds_settings(
        soccer_db, enabled=True, interval_value=5, interval_unit="mins"
    )
    m = get_update_matches_state(soccer_db)
    o = get_update_odds_state(soccer_db)
    assert m.enabled is True and m.interval_value == 3
    assert o.enabled is True and o.interval_value == 5
    save_update_odds_settings(
        soccer_db, enabled=False, interval_value=5, interval_unit="mins"
    )
    m2 = get_update_matches_state(soccer_db)
    o2 = get_update_odds_state(soccer_db)
    assert m2.enabled is True and m2.interval_value == 3
    assert o2.enabled is False and o2.next_run_at is None


def test_odds_enable_disable_interval_restore(soccer_db: Path) -> None:
    before = datetime.now(timezone.utc)
    st = save_update_odds_settings(
        soccer_db, enabled=True, interval_value=1, interval_unit="mins"
    )
    assert st.enabled is True
    nxt = datetime.fromisoformat(st.next_run_at)
    assert timedelta(seconds=50) <= (nxt - before) <= timedelta(seconds=70)

    before2 = datetime.now(timezone.utc)
    st2 = save_update_odds_settings(
        soccer_db, enabled=True, interval_value=5, interval_unit="mins"
    )
    delta = datetime.fromisoformat(st2.next_run_at) - before2
    assert timedelta(minutes=4, seconds=50) <= delta <= timedelta(minutes=5, seconds=15)

    st3 = save_update_odds_settings(
        soccer_db, enabled=False, interval_value=5, interval_unit="mins"
    )
    assert st3.next_run_at is None

    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    save_update_odds_settings(
        soccer_db, enabled=True, interval_value=1, interval_unit="mins"
    )
    _upsert_state(
        soccer_db,
        task_key=TASK_KEY_UPDATE_ODDS,
        enabled=True,
        interval_value=1,
        interval_unit="mins",
        next_run_at=past,
        last_run_at=None,
        last_status="idle",
        last_error=None,
        last_detail={},
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    before3 = datetime.now(timezone.utc)
    restored = restore_update_odds_on_startup(soccer_db)
    d = datetime.fromisoformat(restored.next_run_at) - before3
    assert timedelta(seconds=50) <= d <= timedelta(seconds=70)


def test_odds_ingest_existing_only_and_no_basic_overwrite(soccer_db: Path) -> None:
    _insert_match(soccer_db, match_id="KEEP", home_odds=1.5, home_team="原主隊")
    payload = {
        "data": {
            "matches": [
                _match_payload("KEEP", home=2.1, home_name="來源主隊"),
                _match_payload("NEW", home=1.9, home_name="新主隊"),
            ]
        }
    }
    r = update_odds_for_existing_matches(soccer_db, payload)
    assert r.code == "success"
    assert r.updated == 1
    assert r.skipped_missing == 1
    assert r.inserted == 0
    assert r.odds_changed == 1
    assert r.results_applied is False

    with get_connection(soccer_db) as conn:
        keep = conn.execute(
            "SELECT home_team, home_odds, kickoff_at FROM soccer_daily WHERE match_id='KEEP'"
        ).fetchone()
        new = conn.execute(
            "SELECT 1 FROM soccer_daily WHERE match_id='NEW'"
        ).fetchone()
    assert keep["home_team"] == "原主隊"
    assert float(keep["home_odds"]) == pytest.approx(2.1)
    assert keep["kickoff_at"] == "2026-11-12T20:00:00+08:00"
    assert new is None


def test_odds_incomplete_does_not_blank_existing(soccer_db: Path) -> None:
    _insert_match(soccer_db, match_id="M2", home_odds=1.7, draw_odds=3.1, away_odds=5.0)
    payload = {
        "data": {
            "matches": [
                {
                    "id": "M2",
                    "frontEndId": "FB2",
                    "kickOffTime": "2026-11-12T12:00:00.000Z",
                    "status": "PreMatch",
                    "homeTeam": {"name_ch": "主"},
                    "awayTeam": {"name_ch": "客"},
                    "tournament": {"code": "X", "name_ch": "聯"},
                    "foPools": [],  # incomplete HAD
                }
            ]
        }
    }
    r = update_odds_for_existing_matches(soccer_db, payload)
    assert r.code == "success"
    assert r.had_incomplete == 1
    assert r.odds_unchanged == 1
    with get_connection(soccer_db) as conn:
        row = conn.execute(
            "SELECT home_odds, draw_odds, away_odds FROM soccer_daily WHERE match_id='M2'"
        ).fetchone()
    assert float(row["home_odds"]) == pytest.approx(1.7)
    assert float(row["draw_odds"]) == pytest.approx(3.1)
    assert float(row["away_odds"]) == pytest.approx(5.0)


def test_odds_scheduler_success_skip_failure_and_no_overlap(
    soccer_db: Path, tmp_path: Path
) -> None:
    req_dir = tmp_path / "req"
    req_dir.mkdir()
    (req_dir / "matches_and_odds.json").write_text(
        json.dumps(
            {
                "operationName": "matchList",
                "variables": {"endIndex": 40},
                "query": "query matchList { matches { id } }",
            }
        ),
        encoding="utf-8",
    )
    _insert_match(soccer_db, match_id="M1")
    sched = UpdateOddsScheduler(soccer_db, req_dir)
    sched.apply_settings(enabled=True, interval_value=1, interval_unit="mins")
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    cur = get_update_odds_state(soccer_db)
    _upsert_state(
        soccer_db,
        task_key=TASK_KEY_UPDATE_ODDS,
        enabled=True,
        interval_value=1,
        interval_unit="mins",
        next_run_at=past,
        last_run_at=cur.last_run_at,
        last_status=cur.last_status,
        last_error=cur.last_error,
        last_detail=cur.last_detail,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    body = json.dumps({"data": {"matches": [_match_payload("M1", home=2.05)]}})
    fake_send = SoccerGraphQLSendResult(
        outcome="success",
        http_status=200,
        elapsed_ms=10,
        body_text=body,
        truncated=False,
        match_count=1,
        has_had_odds=True,
    )
    try:
        with patch(
            "hppv.soccer_task_scheduler.send_soccer_graphql", return_value=fake_send
        ), patch(
            "hppv.soccer_task_scheduler.load_soccer_request_preset",
            return_value=(
                type("P", (), {"query": "q", "variables": {}, "operation_name": None})(),
                "{}",
            ),
        ):
            sched._execute_due_job()
        st = get_update_odds_state(soccer_db)
        assert st.last_status == STATUS_SUCCESS
        assert st.last_detail.get("odds_changed") == 1
        assert st.next_run_at
        assert st.running is False

        # no existing → skipped
        empty = SoccerGraphQLSendResult(
            outcome="success",
            http_status=200,
            elapsed_ms=5,
            body_text=json.dumps({"data": {"matches": [_match_payload("ZZ")]}}),
            truncated=False,
            match_count=1,
            has_had_odds=True,
        )
        _upsert_state(
            soccer_db,
            task_key=TASK_KEY_UPDATE_ODDS,
            enabled=True,
            interval_value=1,
            interval_unit="mins",
            next_run_at=past,
            last_run_at=st.last_run_at,
            last_status=st.last_status,
            last_error=None,
            last_detail={},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        with patch(
            "hppv.soccer_task_scheduler.send_soccer_graphql", return_value=empty
        ), patch(
            "hppv.soccer_task_scheduler.load_soccer_request_preset",
            return_value=(
                type("P", (), {"query": "q", "variables": {}, "operation_name": None})(),
                "{}",
            ),
        ):
            sched._execute_due_job()
        st2 = get_update_odds_state(soccer_db)
        assert st2.last_status == STATUS_SKIPPED
        assert st2.last_detail.get("ingest_code") == "skipped_no_existing"

        # failure still schedules next
        _upsert_state(
            soccer_db,
            task_key=TASK_KEY_UPDATE_ODDS,
            enabled=True,
            interval_value=1,
            interval_unit="mins",
            next_run_at=past,
            last_run_at=st2.last_run_at,
            last_status=st2.last_status,
            last_error=None,
            last_detail={},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        with patch(
            "hppv.soccer_task_scheduler.load_soccer_request_preset",
            side_effect=Exception("boom"),
        ):
            sched._execute_due_job()
        st3 = get_update_odds_state(soccer_db)
        assert st3.last_status == STATUS_FAILED
        assert st3.last_error
        assert st3.enabled is True
        assert st3.next_run_at

        sched._running = True
        sched._execute_due_job()
        sched._running = False
    finally:
        sched.stop()


def test_shared_job_lock_serializes_matches_and_odds(
    soccer_db: Path, tmp_path: Path
) -> None:
    req_dir = tmp_path / "req"
    req_dir.mkdir()
    (req_dir / "matches_and_odds.json").write_text("{}", encoding="utf-8")
    matches = UpdateMatchesScheduler(soccer_db, req_dir)
    odds = UpdateOddsScheduler(soccer_db, req_dir)
    marks: list[tuple[str, str, float]] = []

    def slow_matches(_db: Path, _req: Path) -> dict:
        marks.append(("matches", "enter", time.monotonic()))
        time.sleep(0.2)
        marks.append(("matches", "exit", time.monotonic()))
        return {
            "ingest_status": "success",
            "ingest_code": "success",
            "processed": 0,
            "updated": 0,
        }

    def slow_odds(_db: Path, _req: Path) -> dict:
        marks.append(("odds", "enter", time.monotonic()))
        time.sleep(0.05)
        marks.append(("odds", "exit", time.monotonic()))
        return {
            "ingest_status": "success",
            "ingest_code": "success",
            "processed": 0,
            "updated": 0,
            "odds_changed": 0,
            "odds_unchanged": 0,
        }

    past = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    for key in (TASK_KEY_UPDATE_MATCHES, TASK_KEY_UPDATE_ODDS):
        _upsert_state(
            soccer_db,
            task_key=key,
            enabled=True,
            interval_value=1,
            interval_unit="mins",
            next_run_at=past,
            last_run_at=None,
            last_status="idle",
            last_error=None,
            last_detail={},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    try:
        with patch.object(matches, "_run_once", side_effect=slow_matches), patch.object(
            odds, "_run_once", side_effect=slow_odds
        ):
            t1 = threading.Thread(target=matches._execute_due_job)
            t2 = threading.Thread(target=odds._execute_due_job)
            t1.start()
            time.sleep(0.02)
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        by = {f"{name}_{ev}": ts for name, ev, ts in marks}
        assert "matches_enter" in by and "odds_enter" in by
        # Intervals must not overlap under shared job lock.
        m_enter, m_exit = by["matches_enter"], by["matches_exit"]
        o_enter, o_exit = by["odds_enter"], by["odds_exit"]
        no_overlap = m_exit <= o_enter or o_exit <= m_enter
        assert no_overlap, marks
        assert matches.get_state().last_status == STATUS_SUCCESS
        assert odds.get_state().last_status == STATUS_SUCCESS
        assert matches.get_state().enabled is True
        assert odds.get_state().enabled is True
        assert _SHARED_JOB_LOCK is not None
    finally:
        matches.stop()
        odds.stop()
