from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from hppv.soccer_db import init_soccer_db
from hppv.soccer_graphql import SoccerGraphQLSendResult
from hppv.soccer_ingest import IngestResult
from hppv.soccer_task_scheduler import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    UpdateMatchesScheduler,
    get_update_matches_state,
    restore_update_matches_on_startup,
    save_update_matches_settings,
)


@pytest.fixture()
def soccer_db(tmp_path: Path) -> Path:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    return db


def test_default_disabled(soccer_db: Path) -> None:
    state = get_update_matches_state(soccer_db)
    assert state.enabled is False
    assert state.next_run_at is None


def test_enable_schedules_one_interval_ahead(soccer_db: Path) -> None:
    before = datetime.now(timezone.utc)
    state = save_update_matches_settings(
        soccer_db, enabled=True, interval_value=1, interval_unit="mins"
    )
    assert state.enabled is True
    assert state.next_run_at
    nxt = datetime.fromisoformat(state.next_run_at)
    assert nxt.tzinfo is not None
    delta = nxt - before
    assert timedelta(seconds=50) <= delta <= timedelta(seconds=70)


def test_disable_clears_next_run(soccer_db: Path) -> None:
    save_update_matches_settings(
        soccer_db, enabled=True, interval_value=2, interval_unit="mins"
    )
    state = save_update_matches_settings(
        soccer_db, enabled=False, interval_value=2, interval_unit="mins"
    )
    assert state.enabled is False
    assert state.next_run_at is None


def test_interval_change_recomputes_from_save_time(soccer_db: Path) -> None:
    save_update_matches_settings(
        soccer_db, enabled=True, interval_value=1, interval_unit="mins"
    )
    before = datetime.now(timezone.utc)
    state = save_update_matches_settings(
        soccer_db, enabled=True, interval_value=5, interval_unit="mins"
    )
    nxt = datetime.fromisoformat(state.next_run_at)
    delta = nxt - before
    assert timedelta(minutes=4, seconds=50) <= delta <= timedelta(minutes=5, seconds=15)


def test_restore_expired_next_run_becomes_now_plus_interval(soccer_db: Path) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    save_update_matches_settings(
        soccer_db, enabled=True, interval_value=1, interval_unit="mins"
    )
    # Force expired next_run
    from hppv.soccer_task_scheduler import _upsert_state

    _upsert_state(
        soccer_db,
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
    before = datetime.now(timezone.utc)
    state = restore_update_matches_on_startup(soccer_db)
    nxt = datetime.fromisoformat(state.next_run_at)
    delta = nxt - before
    assert timedelta(seconds=50) <= delta <= timedelta(seconds=70)


def test_scheduler_run_success_and_no_overlap(soccer_db: Path, tmp_path: Path) -> None:
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
    sched = UpdateMatchesScheduler(soccer_db, req_dir)
    sched.apply_settings(enabled=True, interval_value=1, interval_unit="mins")
    # Due immediately
    from hppv.soccer_task_scheduler import _upsert_state

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    cur = get_update_matches_state(soccer_db)
    _upsert_state(
        soccer_db,
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

    fake_send = SoccerGraphQLSendResult(
        outcome="success",
        http_status=200,
        elapsed_ms=12,
        body_text='{"data":{"matches":[]}}',
        truncated=False,
        match_count=0,
        has_had_odds=False,
    )
    fake_ing = IngestResult(
        code="skipped_empty",
        processed=0,
        inserted=0,
        updated=0,
        had_complete=0,
        had_incomplete=0,
        last_success_at=None,
        detail="empty matches",
    )
    try:
        with patch(
            "hppv.soccer_task_scheduler.send_soccer_graphql", return_value=fake_send
        ), patch(
            "hppv.soccer_task_scheduler.ingest_matches_and_odds", return_value=fake_ing
        ), patch(
            "hppv.soccer_task_scheduler.load_soccer_request_preset",
            return_value=(
                type("P", (), {"query": "q", "variables": {}, "operation_name": None})(),
                "{}",
            ),
        ):
            sched._execute_due_job()

        state = get_update_matches_state(soccer_db)
        assert state.last_status in (STATUS_SUCCESS, "skipped")
        assert state.last_run_at
        assert state.next_run_at
        assert state.running is False

        # Overlap guard: mark running and ensure second fire no-ops without crash
        sched._running = True
        sched._execute_due_job()
        sched._running = False
    finally:
        sched.stop()


def test_scheduler_failure_records_error_and_continues(
    soccer_db: Path, tmp_path: Path
) -> None:
    req_dir = tmp_path / "req"
    req_dir.mkdir()
    (req_dir / "matches_and_odds.json").write_text(
        json.dumps(
            {
                "operationName": "matchList",
                "variables": {},
                "query": "query matchList { matches { id } }",
            }
        ),
        encoding="utf-8",
    )
    sched = UpdateMatchesScheduler(soccer_db, req_dir)
    sched.apply_settings(enabled=True, interval_value=1, interval_unit="mins")
    from hppv.soccer_task_scheduler import _upsert_state

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    cur = get_update_matches_state(soccer_db)
    _upsert_state(
        soccer_db,
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
    try:
        with patch(
            "hppv.soccer_task_scheduler.load_soccer_request_preset",
            side_effect=Exception("boom"),
        ):
            sched._execute_due_job()
        state = get_update_matches_state(soccer_db)
        assert state.last_status == STATUS_FAILED
        assert state.last_error
        assert state.enabled is True
        assert state.next_run_at  # still scheduled
        assert state.running is False
    finally:
        sched.stop()
