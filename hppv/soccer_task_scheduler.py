"""Soccer web task scheduler: update_matches + update_odds + update_results.

Limitation: in-process locks only. Not safe across multiple WSGI workers /
processes. Current deployment assumes ``python -m hppv.web_app`` (one process).

Cross-task coordination: GraphQL+ingest jobs share ``_SHARED_JOB_LOCK`` so
concurrent timers cannot race the same soccer_daily rows / overwrite with an
older response. Per-task ``_running`` still prevents same-task overlap.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from hppv.schema import SOCCER_TASK_SETTINGS_TABLE
from hppv.soccer_db import get_connection, init_soccer_db, table_exists
from hppv.soccer_graphql import SoccerGraphQLSendError, send_soccer_graphql
from hppv.soccer_ingest import (
    HISTORY_SCOPE_DAILY_ONLY,
    REQUEST_KEY_MATCHES_AND_ODDS,
    REQUEST_KEY_MATCH_RESULTS,
    REQUEST_KEY_UPDATE_ODDS_AND_RESULTS,
    REQUEST_KEY_UPDATE_RESULTS,
    SoccerIngestError,
    assert_match_results_completeness,
    format_hkt_display,
    ingest_matches_and_odds,
    ingest_official_results_to_history,
    soccer_daily_date_bounds,
    soccer_daily_distinct_dates,
    update_odds_for_existing_matches,
    utc_now_iso,
)
from hppv.soccer_ingest import (
    _extract_match_results_envelope as extract_match_results_envelope,
)
from hppv.soccer_requests import (
    SoccerRequestPresetError,
    apply_match_results_date_window,
    load_soccer_request_preset,
)

logger = logging.getLogger(__name__)

TASK_KEY_UPDATE_MATCHES = "update_matches"
TASK_KEY_UPDATE_ODDS = "update_odds"
TASK_KEY_UPDATE_RESULTS = "update_results"
INTERVAL_UNITS = frozenset({"mins", "hour", "day"})
DEFAULT_INTERVAL_VALUE = 1
DEFAULT_INTERVAL_UNIT = "mins"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Serialize network download + ingest across tasks (process-local only).
_SHARED_JOB_LOCK = threading.RLock()

UPDATE_MATCHES_SCOPE = {
    "request_key": REQUEST_KEY_MATCHES_AND_ODDS,
    "preset": "matches_and_odds",
    "end_index": 40,
    "date_window": "source_default_null_dates",
    "official_results": False,
    "full_catalog": False,
    "note": (
        "Reuses matches_and_odds manual chain (paged endIndex=40, "
        "startDate/endDate null). Not a full match catalog download. "
        "Official results are out of scope. HAD odds columns may update "
        "as part of this ingest. Concurrent update_odds runs are serialized "
        "via an in-process shared job lock (not multi-worker safe)."
    ),
    "multi_worker_safe": False,
}

UPDATE_ODDS_SCOPE = {
    "request_key": REQUEST_KEY_UPDATE_ODDS_AND_RESULTS,
    "preset": "matches_and_odds",  # same JSON file; odds-only ingest path
    "end_index": 40,
    "date_window": "source_default_null_dates",
    "official_results": False,
    "full_catalog": False,
    "insert_new_matches": False,
    "note": (
        "Reuses update_odds_and_results / matches_and_odds.json (endIndex=40, "
        "startDate/endDate null). Updates HAD odds on existing soccer_daily "
        "rows only; no INSERT; no basic field overwrite; no official results. "
        "Not a full soccer_daily scan. Concurrent update_matches runs are "
        "serialized via an in-process shared job lock (not multi-worker safe)."
    ),
    "multi_worker_safe": False,
}

UPDATE_RESULTS_SCOPE = {
    "request_key": REQUEST_KEY_UPDATE_RESULTS,
    "preset": REQUEST_KEY_MATCH_RESULTS,
    "end_index": 0,
    "date_window": "soccer_daily_min_max_match_date",
    "official_results": True,
    "history_table": "footylogic_matches",
    "source": "hkjc",
    "delete_daily_after_ft": True,
    "note": (
        "Reuses match_results query; date window = MIN/MAX soccer_daily.match_date "
        "(day-chunk fallback if endIndex=0 incomplete). Confirmed FT in "
        "matchResults upserts footylogic_matches (source=hkjc, source_key=hkjc|{id}); "
        "source status is not a gate. Then DELETE soccer_daily + tombstone. "
        "HT-only keeps daily."
    ),
    "multi_worker_safe": False,
}


class SoccerTaskError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SoccerTaskState:
    task_key: str
    enabled: bool
    interval_value: int
    interval_unit: str
    next_run_at: str | None
    last_run_at: str | None
    last_status: str | None
    last_error: str | None
    last_detail: dict[str, Any]
    updated_at: str
    running: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        next_hkt = None
        last_hkt = None
        if self.next_run_at:
            try:
                next_hkt = format_hkt_display(self.next_run_at)
            except Exception:
                next_hkt = None
        if self.last_run_at:
            try:
                last_hkt = format_hkt_display(self.last_run_at)
            except Exception:
                last_hkt = None
        return {
            "task_key": self.task_key,
            "enabled": self.enabled,
            "interval_value": self.interval_value,
            "interval_unit": self.interval_unit,
            "next_run_at": self.next_run_at,
            "next_run_at_hkt": next_hkt,
            "last_run_at": self.last_run_at,
            "last_run_at_hkt": last_hkt,
            "last_status": self.last_status or STATUS_IDLE,
            "last_error": self.last_error or "",
            "last_detail": self.last_detail,
            "updated_at": self.updated_at,
            "running": self.running,
            "scope": scope_for_task(self.task_key),
        }


# Backward-compatible alias used by existing tests / imports.
UpdateMatchesTaskState = SoccerTaskState


def scope_for_task(task_key: str) -> dict[str, Any]:
    if task_key == TASK_KEY_UPDATE_ODDS:
        return UPDATE_ODDS_SCOPE
    if task_key == TASK_KEY_UPDATE_RESULTS:
        return UPDATE_RESULTS_SCOPE
    return UPDATE_MATCHES_SCOPE


def _parse_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    text = str(iso).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def interval_to_timedelta(value: int, unit: str) -> timedelta:
    unit_n = (unit or DEFAULT_INTERVAL_UNIT).strip().lower()
    n = max(1, int(value or DEFAULT_INTERVAL_VALUE))
    if unit_n == "hour":
        return timedelta(hours=n)
    if unit_n == "day":
        return timedelta(days=n)
    return timedelta(minutes=n)


def normalize_interval(value: Any, unit: Any) -> tuple[int, str]:
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise SoccerTaskError("invalid_interval", "interval_value must be int") from e
    if n < 1 or n > 10:
        raise SoccerTaskError("invalid_interval", "interval_value must be 1..10")
    unit_n = str(unit or "").strip().lower()
    if unit_n not in INTERVAL_UNITS:
        raise SoccerTaskError("invalid_interval_unit", unit_n)
    return n, unit_n


def ensure_task_settings_table(db_path: Path) -> None:
    init_soccer_db(db_path)


def _row_to_state(row: Any, *, running: bool) -> SoccerTaskState:
    detail_raw = row["last_detail_json"] if "last_detail_json" in row.keys() else None
    detail: dict[str, Any] = {}
    if detail_raw:
        try:
            parsed = json.loads(str(detail_raw))
            if isinstance(parsed, dict):
                detail = parsed
        except json.JSONDecodeError:
            detail = {"raw": str(detail_raw)}
    return SoccerTaskState(
        task_key=str(row["task_key"]),
        enabled=bool(int(row["enabled"] or 0)),
        interval_value=int(row["interval_value"] or DEFAULT_INTERVAL_VALUE),
        interval_unit=str(row["interval_unit"] or DEFAULT_INTERVAL_UNIT),
        next_run_at=(str(row["next_run_at"]) if row["next_run_at"] else None),
        last_run_at=(str(row["last_run_at"]) if row["last_run_at"] else None),
        last_status=(str(row["last_status"]) if row["last_status"] else None),
        last_error=(str(row["last_error"]) if row["last_error"] else None),
        last_detail=detail,
        updated_at=str(row["updated_at"] or ""),
        running=running,
    )


def get_task_state(
    db_path: Path,
    task_key: str,
    *,
    running: bool = False,
) -> SoccerTaskState:
    ensure_task_settings_table(db_path)
    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_TASK_SETTINGS_TABLE):
            raise SoccerTaskError("table_missing", SOCCER_TASK_SETTINGS_TABLE)
        row = conn.execute(
            f"SELECT * FROM {SOCCER_TASK_SETTINGS_TABLE} WHERE task_key = ?;",
            (task_key,),
        ).fetchone()
        if row is None:
            stamp = utc_now_iso()
            return SoccerTaskState(
                task_key=task_key,
                enabled=False,
                interval_value=DEFAULT_INTERVAL_VALUE,
                interval_unit=DEFAULT_INTERVAL_UNIT,
                next_run_at=None,
                last_run_at=None,
                last_status=STATUS_IDLE,
                last_error=None,
                last_detail={},
                updated_at=stamp,
                running=running,
            )
        return _row_to_state(row, running=running)


def get_update_matches_state(db_path: Path, *, running: bool = False) -> SoccerTaskState:
    return get_task_state(db_path, TASK_KEY_UPDATE_MATCHES, running=running)


def get_update_odds_state(db_path: Path, *, running: bool = False) -> SoccerTaskState:
    return get_task_state(db_path, TASK_KEY_UPDATE_ODDS, running=running)


def get_update_results_state(db_path: Path, *, running: bool = False) -> SoccerTaskState:
    return get_task_state(db_path, TASK_KEY_UPDATE_RESULTS, running=running)


def _upsert_state(
    db_path: Path,
    *,
    task_key: str = TASK_KEY_UPDATE_MATCHES,
    enabled: bool,
    interval_value: int,
    interval_unit: str,
    next_run_at: str | None,
    last_run_at: str | None,
    last_status: str | None,
    last_error: str | None,
    last_detail: dict[str, Any] | None,
    updated_at: str,
) -> SoccerTaskState:
    ensure_task_settings_table(db_path)
    detail_json = json.dumps(last_detail or {}, ensure_ascii=False)
    with get_connection(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {SOCCER_TASK_SETTINGS_TABLE} (
              task_key, enabled, interval_value, interval_unit,
              next_run_at, last_run_at, last_status, last_error,
              last_detail_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_key) DO UPDATE SET
              enabled = excluded.enabled,
              interval_value = excluded.interval_value,
              interval_unit = excluded.interval_unit,
              next_run_at = excluded.next_run_at,
              last_run_at = excluded.last_run_at,
              last_status = excluded.last_status,
              last_error = excluded.last_error,
              last_detail_json = excluded.last_detail_json,
              updated_at = excluded.updated_at;
            """,
            (
                task_key,
                1 if enabled else 0,
                interval_value,
                interval_unit,
                next_run_at,
                last_run_at,
                last_status,
                last_error,
                detail_json,
                updated_at,
            ),
        )
        conn.commit()
    return get_task_state(db_path, task_key)


def save_task_settings(
    db_path: Path,
    task_key: str,
    *,
    enabled: bool,
    interval_value: Any,
    interval_unit: Any,
) -> SoccerTaskState:
    """Persist settings. On enable (or interval change while enabled), next_run = now + interval."""
    value, unit = normalize_interval(interval_value, interval_unit)
    prev = get_task_state(db_path, task_key)
    now = _now_utc()
    stamp = now.isoformat()
    if enabled:
        next_run = (now + interval_to_timedelta(value, unit)).isoformat()
    else:
        next_run = None
    return _upsert_state(
        db_path,
        task_key=task_key,
        enabled=enabled,
        interval_value=value,
        interval_unit=unit,
        next_run_at=next_run,
        last_run_at=prev.last_run_at,
        last_status=prev.last_status or STATUS_IDLE,
        last_error=prev.last_error,
        last_detail=prev.last_detail,
        updated_at=stamp,
    )


def save_update_matches_settings(
    db_path: Path,
    *,
    enabled: bool,
    interval_value: Any,
    interval_unit: Any,
) -> SoccerTaskState:
    return save_task_settings(
        db_path,
        TASK_KEY_UPDATE_MATCHES,
        enabled=enabled,
        interval_value=interval_value,
        interval_unit=interval_unit,
    )


def save_update_odds_settings(
    db_path: Path,
    *,
    enabled: bool,
    interval_value: Any,
    interval_unit: Any,
) -> SoccerTaskState:
    return save_task_settings(
        db_path,
        TASK_KEY_UPDATE_ODDS,
        enabled=enabled,
        interval_value=interval_value,
        interval_unit=interval_unit,
    )


def save_update_results_settings(
    db_path: Path,
    *,
    enabled: bool,
    interval_value: Any,
    interval_unit: Any,
) -> SoccerTaskState:
    return save_task_settings(
        db_path,
        TASK_KEY_UPDATE_RESULTS,
        enabled=enabled,
        interval_value=interval_value,
        interval_unit=interval_unit,
    )


def restore_task_on_startup(db_path: Path, task_key: str) -> SoccerTaskState:
    """
    If enabled: keep future next_run; if missing/expired → now + interval (no catch-up).
    """
    state = get_task_state(db_path, task_key)
    if not state.enabled:
        return state
    now = _now_utc()
    next_dt = _parse_utc(state.next_run_at)
    if next_dt is None or next_dt <= now:
        next_run = (
            now + interval_to_timedelta(state.interval_value, state.interval_unit)
        ).isoformat()
        state = _upsert_state(
            db_path,
            task_key=task_key,
            enabled=True,
            interval_value=state.interval_value,
            interval_unit=state.interval_unit,
            next_run_at=next_run,
            last_run_at=state.last_run_at,
            last_status=state.last_status,
            last_error=state.last_error,
            last_detail=state.last_detail,
            updated_at=now.isoformat(),
        )
        logger.info(
            "%s next_run restored to %s (was missing/expired)",
            task_key,
            next_run,
        )
    return state


def restore_update_matches_on_startup(db_path: Path) -> SoccerTaskState:
    return restore_task_on_startup(db_path, TASK_KEY_UPDATE_MATCHES)


def restore_update_odds_on_startup(db_path: Path) -> SoccerTaskState:
    return restore_task_on_startup(db_path, TASK_KEY_UPDATE_ODDS)


def restore_update_results_on_startup(db_path: Path) -> SoccerTaskState:
    return restore_task_on_startup(db_path, TASK_KEY_UPDATE_RESULTS)


def run_update_matches_once(db_path: Path, requests_dir: Path) -> dict[str, Any]:
    """Execute one matches_and_odds download + ingest (manual chain reuse)."""
    started = utc_now_iso()
    detail: dict[str, Any] = {
        "started_at": started,
        "request_key": REQUEST_KEY_MATCHES_AND_ODDS,
        "scope": UPDATE_MATCHES_SCOPE,
    }
    try:
        payload, _display = load_soccer_request_preset(
            requests_dir, REQUEST_KEY_MATCHES_AND_ODDS
        )
    except SoccerRequestPresetError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    try:
        result = send_soccer_graphql(payload, wait_for_rate_limit=True)
    except SoccerGraphQLSendError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    detail.update(
        {
            "outcome": result.outcome,
            "http_status": result.http_status,
            "elapsed_ms": result.elapsed_ms,
            "truncated": result.truncated,
            "match_count": result.match_count,
            "has_had_odds": result.has_had_odds,
        }
    )

    if result.truncated:
        raise SoccerTaskError("truncated", "response truncated")
    if result.outcome != "success":
        raise SoccerTaskError(result.outcome, "response not eligible for ingest")

    try:
        response_obj = json.loads(result.body_text)
    except json.JSONDecodeError as e:
        raise SoccerTaskError("invalid_json", str(e)) from e

    try:
        ing = ingest_matches_and_odds(
            db_path, response_obj, request_key=REQUEST_KEY_MATCHES_AND_ODDS
        )
    except SoccerIngestError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    detail.update(
        {
            "ingest_status": "success" if ing.code == "success" else "skipped",
            "ingest_code": ing.code,
            "processed": ing.processed,
            "inserted": ing.inserted,
            "updated": ing.updated,
            "had_complete": ing.had_complete,
            "had_incomplete": ing.had_incomplete,
            "finished_at": utc_now_iso(),
            "detail": ing.detail,
        }
    )
    if ing.code != "success" and ing.code != "skipped_empty":
        if ing.code.startswith("skipped"):
            detail["ingest_status"] = "skipped"
        else:
            raise SoccerTaskError(ing.code, ing.detail or "ingest failed")
    return detail


def run_update_odds_once(db_path: Path, requests_dir: Path) -> dict[str, Any]:
    """Execute one odds-only download + update_odds_for_existing_matches."""
    started = utc_now_iso()
    detail: dict[str, Any] = {
        "started_at": started,
        "request_key": REQUEST_KEY_UPDATE_ODDS_AND_RESULTS,
        "scope": UPDATE_ODDS_SCOPE,
    }
    try:
        # Same verified JSON as manual update_odds_and_results.
        payload, _display = load_soccer_request_preset(
            requests_dir, REQUEST_KEY_UPDATE_ODDS_AND_RESULTS
        )
    except SoccerRequestPresetError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    try:
        result = send_soccer_graphql(payload, wait_for_rate_limit=True)
    except SoccerGraphQLSendError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    detail.update(
        {
            "outcome": result.outcome,
            "http_status": result.http_status,
            "elapsed_ms": result.elapsed_ms,
            "truncated": result.truncated,
            "match_count": result.match_count,
            "has_had_odds": result.has_had_odds,
        }
    )

    if result.truncated:
        raise SoccerTaskError("truncated", "response truncated")
    if result.outcome != "success":
        raise SoccerTaskError(result.outcome, "response not eligible for ingest")

    try:
        response_obj = json.loads(result.body_text)
    except json.JSONDecodeError as e:
        raise SoccerTaskError("invalid_json", str(e)) from e

    try:
        ing = update_odds_for_existing_matches(
            db_path, response_obj, request_key=REQUEST_KEY_UPDATE_ODDS_AND_RESULTS
        )
    except SoccerIngestError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    detail.update(
        {
            "ingest_status": "success" if ing.code == "success" else "skipped",
            "ingest_code": ing.code,
            "processed": ing.processed,
            "inserted": ing.inserted,
            "updated": ing.updated,
            "had_complete": ing.had_complete,
            "had_incomplete": ing.had_incomplete,
            "skipped_missing": ing.skipped_missing,
            "odds_changed": ing.odds_changed,
            "odds_unchanged": ing.odds_unchanged,
            "results_applied": ing.results_applied,
            "finished_at": utc_now_iso(),
            "detail": ing.detail,
        }
    )
    if ing.code != "success":
        if ing.code.startswith("skipped"):
            detail["ingest_status"] = "skipped"
        else:
            raise SoccerTaskError(ing.code, ing.detail or "odds update failed")
    return detail


def soccer_shared_job_lock() -> threading.RLock:
    """Process-local lock shared by tasks and manual GraphQL+ingest."""
    return _SHARED_JOB_LOCK


def _fetch_match_results_json(
    payload: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send GraphQL and return (response_obj, send_detail)."""
    try:
        result = send_soccer_graphql(payload, wait_for_rate_limit=True)
    except SoccerGraphQLSendError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    send_detail = {
        "outcome": result.outcome,
        "http_status": result.http_status,
        "elapsed_ms": result.elapsed_ms,
        "truncated": result.truncated,
        "match_count": result.match_count,
    }
    if result.truncated:
        raise SoccerTaskError("truncated", "response truncated")
    if result.outcome != "success":
        raise SoccerTaskError(result.outcome, "response not eligible for ingest")
    try:
        response_obj = json.loads(result.body_text)
    except json.JSONDecodeError as e:
        raise SoccerTaskError("invalid_json", str(e)) from e
    return response_obj, send_detail


def inclusive_calendar_dates(start_date: str, end_date: str) -> list[str]:
    """Inclusive YYYY-MM-DD list from start_date through end_date."""
    from datetime import date, timedelta

    start = date.fromisoformat(str(start_date).strip())
    end = date.fromisoformat(str(end_date).strip())
    if end < start:
        raise SoccerTaskError(
            "invalid_fields", f"endDate {end_date!r} before startDate {start_date!r}"
        )
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def download_match_results_complete(
    base_payload: Any,
    *,
    start_date: str,
    end_date: str,
    chunk_dates: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """
    Fetch matchResults for [start_date, end_date] with endIndex=0.

    If the window is incomplete, re-fetch each day in chunk_dates (default:
    every calendar day in the range) and merge by match id.

    Returns (response_obj, variables_for_ingest, meta).
    """
    window_payload = apply_match_results_date_window(
        base_payload, start_date=start_date, end_date=end_date
    )
    response_obj, send_detail = _fetch_match_results_json(window_payload)
    variables = dict(window_payload.variables or {})
    meta: dict[str, Any] = {
        **send_detail,
        "fetch_mode": "window_endIndex_0",
        "date_window": {"startDate": start_date, "endDate": end_date},
    }
    matches, total, _ = extract_match_results_envelope(response_obj)
    try:
        assert_match_results_completeness(
            matches_returned=len(matches),
            match_num_total=total,
            variables=variables,
        )
        return response_obj, variables, meta
    except SoccerIngestError as e:
        if e.code != "incomplete_results":
            raise SoccerTaskError(e.code, e.detail) from e

    dates = list(chunk_dates) if chunk_dates is not None else inclusive_calendar_dates(
        start_date, end_date
    )
    meta["fetch_mode"] = "day_chunk_fallback"
    meta["day_chunks"] = dates
    merged: dict[str, Any] = {}
    for day in dates:
        day_payload = apply_match_results_date_window(
            base_payload, start_date=day, end_date=day
        )
        day_obj, day_send = _fetch_match_results_json(day_payload)
        meta.setdefault("day_fetches", []).append({"date": day, **day_send})
        day_matches, day_total, _ = extract_match_results_envelope(day_obj)
        day_vars = dict(day_payload.variables or {})
        assert_match_results_completeness(
            matches_returned=len(day_matches),
            match_num_total=day_total,
            variables=day_vars,
        )
        for item in day_matches:
            if isinstance(item, dict) and item.get("id") is not None:
                merged[str(item["id"]).strip()] = item
    response_obj = {
        "data": {
            "matchNumByDate": {"total": len(merged)},
            "matches": list(merged.values()),
        }
    }
    variables = {
        "startDate": start_date,
        "endDate": end_date,
        "startIndex": 0,
        "endIndex": 0,
        "teamId": None,
    }
    meta["match_count"] = len(merged)
    return response_obj, variables, meta


def run_update_results_once(db_path: Path, requests_dir: Path) -> dict[str, Any]:
    """
    Official results → footylogic_matches; delete daily after confirmed FT.

    Date window from soccer_daily MIN/MAX match_date. If endIndex=0 window is
    incomplete, re-fetch each distinct daily date and merge by match id.
    """
    started = utc_now_iso()
    detail: dict[str, Any] = {
        "started_at": started,
        "request_key": REQUEST_KEY_UPDATE_RESULTS,
        "scope": UPDATE_RESULTS_SCOPE,
    }
    bounds = soccer_daily_date_bounds(db_path)
    if bounds is None:
        detail.update(
            {
                "ingest_status": "skipped",
                "ingest_code": "skipped_empty_daily",
                "processed": 0,
                "inserted": 0,
                "updated": 0,
                "daily_deleted": 0,
                "finished_at": utc_now_iso(),
                "detail": "soccer_daily empty",
            }
        )
        return detail

    start_date, end_date = bounds
    detail["date_window"] = {"startDate": start_date, "endDate": end_date}

    try:
        base_payload, _display = load_soccer_request_preset(
            requests_dir, REQUEST_KEY_MATCH_RESULTS
        )
    except SoccerRequestPresetError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    # Task keeps daily pending date scope; chunk by distinct daily dates only.
    response_obj, variables, fetch_meta = download_match_results_complete(
        base_payload,
        start_date=start_date,
        end_date=end_date,
        chunk_dates=soccer_daily_distinct_dates(db_path),
    )
    detail.update(fetch_meta)

    try:
        ing = ingest_official_results_to_history(
            db_path,
            response_obj,
            request_key=REQUEST_KEY_UPDATE_RESULTS,
            variables=variables,
            history_scope=HISTORY_SCOPE_DAILY_ONLY,
        )
    except SoccerIngestError as e:
        raise SoccerTaskError(e.code, e.detail) from e

    detail.update(
        {
            "ingest_status": "success" if ing.code == "success" else "skipped",
            "ingest_code": ing.code,
            "processed": ing.processed,
            "inserted": ing.inserted,
            "updated": ing.updated,
            "daily_deleted": ing.daily_deleted,
            "ht_applied": ing.ht_applied,
            "ft_applied": ing.ft_applied,
            "sh_applied": ing.sh_applied,
            "skipped_status": ing.skipped_status,
            "skipped_unconfirmed": ing.skipped_unconfirmed,
            "skipped_no_kickoff": ing.skipped_no_kickoff,
            "skipped_not_in_daily": ing.skipped_not_in_daily,
            "match_num_total": ing.match_num_total,
            "matches_returned": ing.matches_returned,
            "results_applied": ing.results_applied,
            "finished_at": utc_now_iso(),
            "detail": ing.detail,
        }
    )
    if ing.code != "success":
        if ing.code.startswith("skipped"):
            detail["ingest_status"] = "skipped"
        else:
            raise SoccerTaskError(ing.code, ing.detail or "results update failed")
    return detail


class _TaskSchedulerBase:
    """Shared timer / anti-overlap / shared-job-lock machinery."""

    task_key: str
    _run_once: Callable[[Path, Path], dict[str, Any]]

    def __init__(self, db_path: Path, requests_dir: Path) -> None:
        self.db_path = Path(db_path)
        self.requests_dir = Path(requests_dir)
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._running = False
        self._started = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def get_state(self) -> SoccerTaskState:
        with self._lock:
            return get_task_state(self.db_path, self.task_key, running=self._running)

    def start(self) -> SoccerTaskState:
        with self._lock:
            if self._started:
                return self.get_state()
            self._started = True
            state = restore_task_on_startup(self.db_path, self.task_key)
            self._arm_timer_locked(state)
            logger.info(
                "%s scheduler started enabled=%s next_run=%s "
                "(single-process only; not multi-worker safe)",
                self.task_key,
                state.enabled,
                state.next_run_at,
            )
            return self.get_state()

    def stop(self) -> None:
        """Cancel pending timer (tests / clean shutdown). Does not disable settings."""
        with self._lock:
            self._cancel_timer_locked()
            self._started = False

    def apply_settings(
        self,
        *,
        enabled: bool,
        interval_value: Any,
        interval_unit: Any,
    ) -> SoccerTaskState:
        with self._lock:
            state = save_task_settings(
                self.db_path,
                self.task_key,
                enabled=enabled,
                interval_value=interval_value,
                interval_unit=interval_unit,
            )
            self._arm_timer_locked(state)
            return self.get_state()

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _arm_timer_locked(self, state: SoccerTaskState) -> None:
        self._cancel_timer_locked()
        if not state.enabled or not state.next_run_at:
            return
        next_dt = _parse_utc(state.next_run_at)
        if next_dt is None:
            return
        delay = (next_dt - _now_utc()).total_seconds()
        if delay < 0.05:
            delay = 0.05
        timer = threading.Timer(delay, self._on_timer)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timer(self) -> None:
        try:
            self._execute_due_job()
        except Exception:
            logger.exception("%s timer callback crashed", self.task_key)

    def _execute_due_job(self) -> None:
        with self._lock:
            if self._running:
                logger.warning("%s skipped: previous run still in progress", self.task_key)
                return
            state = get_task_state(self.db_path, self.task_key, running=False)
            if not state.enabled:
                return
            next_dt = _parse_utc(state.next_run_at)
            now = _now_utc()
            if next_dt is not None and next_dt > now + timedelta(seconds=1):
                self._arm_timer_locked(state)
                return
            self._running = True
            _upsert_state(
                self.db_path,
                task_key=self.task_key,
                enabled=True,
                interval_value=state.interval_value,
                interval_unit=state.interval_unit,
                next_run_at=state.next_run_at,
                last_run_at=state.last_run_at,
                last_status=STATUS_RUNNING,
                last_error=None,
                last_detail=state.last_detail,
                updated_at=now.isoformat(),
            )

        detail: dict[str, Any] = {}
        status = STATUS_FAILED
        error = ""
        try:
            # Cross-task serialization: wait for the other task's GraphQL+ingest.
            with _SHARED_JOB_LOCK:
                detail = self._run_once(self.db_path, self.requests_dir)
            status = (
                STATUS_SUCCESS
                if detail.get("ingest_status") == "success"
                else STATUS_SKIPPED
            )
            logger.info(
                "%s finished status=%s processed=%s updated=%s changed=%s",
                self.task_key,
                status,
                detail.get("processed"),
                detail.get("updated"),
                detail.get("odds_changed"),
            )
        except SoccerTaskError as e:
            status = STATUS_FAILED
            error = f"{e.code}: {e.detail}" if e.detail else e.code
            detail = {"error_code": e.code, "error_detail": e.detail}
            logger.warning("%s failed: %s", self.task_key, error)
        except Exception as e:
            status = STATUS_FAILED
            error = f"unexpected: {e}"
            detail = {"error_code": "unexpected", "error_detail": str(e)}
            logger.exception("%s unexpected failure", self.task_key)

        with self._lock:
            self._running = False
            cur = get_task_state(self.db_path, self.task_key, running=False)
            finished = _now_utc()
            next_run = None
            # Re-read enabled after job: disable mid-run must not re-arm.
            if cur.enabled:
                next_run = (
                    finished
                    + interval_to_timedelta(cur.interval_value, cur.interval_unit)
                ).isoformat()
            state = _upsert_state(
                self.db_path,
                task_key=self.task_key,
                enabled=cur.enabled,
                interval_value=cur.interval_value,
                interval_unit=cur.interval_unit,
                next_run_at=next_run,
                last_run_at=finished.isoformat(),
                last_status=status,
                last_error=error or None,
                last_detail=detail,
                updated_at=finished.isoformat(),
            )
            self._arm_timer_locked(state)


class UpdateMatchesScheduler(_TaskSchedulerBase):
    """In-process scheduler for update_matches (single worker only)."""

    task_key = TASK_KEY_UPDATE_MATCHES
    _run_once = staticmethod(run_update_matches_once)


class UpdateOddsScheduler(_TaskSchedulerBase):
    """In-process scheduler for update_odds (single worker only)."""

    task_key = TASK_KEY_UPDATE_ODDS
    _run_once = staticmethod(run_update_odds_once)


class UpdateResultsScheduler(_TaskSchedulerBase):
    """In-process scheduler for update_results (single worker only)."""

    task_key = TASK_KEY_UPDATE_RESULTS
    _run_once = staticmethod(run_update_results_once)


_matches_scheduler: UpdateMatchesScheduler | None = None
_odds_scheduler: UpdateOddsScheduler | None = None
_results_scheduler: UpdateResultsScheduler | None = None
_scheduler_lock = threading.Lock()


def get_update_matches_scheduler(
    db_path: Path,
    requests_dir: Path,
) -> UpdateMatchesScheduler:
    global _matches_scheduler
    with _scheduler_lock:
        if _matches_scheduler is None:
            _matches_scheduler = UpdateMatchesScheduler(db_path, requests_dir)
        return _matches_scheduler


def get_update_odds_scheduler(
    db_path: Path,
    requests_dir: Path,
) -> UpdateOddsScheduler:
    global _odds_scheduler
    with _scheduler_lock:
        if _odds_scheduler is None:
            _odds_scheduler = UpdateOddsScheduler(db_path, requests_dir)
        return _odds_scheduler


def get_update_results_scheduler(
    db_path: Path,
    requests_dir: Path,
) -> UpdateResultsScheduler:
    global _results_scheduler
    with _scheduler_lock:
        if _results_scheduler is None:
            _results_scheduler = UpdateResultsScheduler(db_path, requests_dir)
        return _results_scheduler


__all__ = [
    "TASK_KEY_UPDATE_MATCHES",
    "TASK_KEY_UPDATE_ODDS",
    "TASK_KEY_UPDATE_RESULTS",
    "UPDATE_MATCHES_SCOPE",
    "UPDATE_ODDS_SCOPE",
    "UPDATE_RESULTS_SCOPE",
    "UpdateMatchesScheduler",
    "UpdateOddsScheduler",
    "UpdateResultsScheduler",
    "UpdateMatchesTaskState",
    "SoccerTaskState",
    "SoccerTaskError",
    "get_update_matches_scheduler",
    "get_update_odds_scheduler",
    "get_update_results_scheduler",
    "get_update_matches_state",
    "get_update_odds_state",
    "get_update_results_state",
    "get_task_state",
    "restore_update_matches_on_startup",
    "restore_update_odds_on_startup",
    "restore_update_results_on_startup",
    "run_update_matches_once",
    "run_update_odds_once",
    "run_update_results_once",
    "save_update_matches_settings",
    "save_update_odds_settings",
    "save_update_results_settings",
]
