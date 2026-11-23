"""Shared Web download shell: preset load + send dispatch by sport.

Soccer and racing both use this module; sport-specific logic is registered once.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from hppv.racing_download import (
    RacingDownloadError,
    run_daily_results_download_locked,
    run_horses_download_locked,
    run_odds_download_locked,
    run_racecard_download_locked,
    run_results_period_download_locked,
)
from hppv.racing_requests import (
    RACING_REQUEST_PRESETS,
    REQUEST_KEY_DAILY_RESULTS as RACING_REQUEST_KEY_DAILY_RESULTS,
    REQUEST_KEY_HORSES as RACING_REQUEST_KEY_HORSES,
    REQUEST_KEY_RACECARD,
    REQUEST_KEY_RESULTS_PERIOD as RACING_REQUEST_KEY_RESULTS_PERIOD,
    REQUEST_KEY_UPDATE_ODDS as RACING_REQUEST_KEY_UPDATE_ODDS,
    RacingRequestPresetError,
    assert_payload_matches_whitelist,
    load_racing_request_preset,
    parse_racing_send_payload,
)
from hppv.soccer_graphql import (
    SoccerGraphQLSendError,
    parse_and_validate_send_payload,
    send_soccer_graphql,
)
from hppv.soccer_ingest import (
    REQUEST_KEY_MATCHES_AND_ODDS,
    REQUEST_KEY_MATCH_RESULTS,
    REQUEST_KEY_MATCH_RESULTS_BY_YEAR,
    REQUEST_KEY_UPDATE_ODDS_AND_RESULTS,
    SoccerIngestError,
    format_hkt_display,
    get_last_success_at,
    HISTORY_SCOPE_DAILY_ONLY,
    HISTORY_SCOPE_PAYLOAD_ALL as INGEST_HISTORY_SCOPE_PAYLOAD_ALL,
    ingest_matches_and_odds,
    ingest_official_match_results,
    ingest_official_results_to_history,
    update_odds_for_existing_matches,
)
from hppv.soccer_requests import (
    SOCCER_REQUEST_PRESETS,
    SoccerRequestPresetError,
    load_soccer_request_preset,
)

# Synthetic dispatch keys (not GraphQL presets): shared entry still required.
REQUEST_KEY_FOOTYLOGIC_CLI_RUN = "footylogic_cli_run"
HISTORY_SCOPE_PAYLOAD_ALL = INGEST_HISTORY_SCOPE_PAYLOAD_ALL
# Re-export for callers that pass task-scoped daily_only.
HISTORY_SCOPE_DAILY_ONLY = HISTORY_SCOPE_DAILY_ONLY

logger = logging.getLogger(__name__)

SPORT_SOCCER = "soccer"
SPORT_RACING = "racing"
SUPPORTED_SPORTS = frozenset({SPORT_SOCCER, SPORT_RACING})


class DownloadShellError(Exception):
    def __init__(self, code: str, detail: str = "", http_status: int = 400) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.http_status = http_status


def normalize_sport(sport: str) -> str:
    s = (sport or "").strip().lower()
    if s not in SUPPORTED_SPORTS:
        raise DownloadShellError("unsupported_sport", f"sport={sport!r}", http_status=404)
    return s


def load_preset_display(
    sport: str,
    preset_id: str,
    *,
    soccer_requests_dir: Path,
    racing_requests_dir: Path,
) -> str:
    sport_n = normalize_sport(sport)
    try:
        if sport_n == SPORT_SOCCER:
            _payload, display = load_soccer_request_preset(soccer_requests_dir, preset_id)
            return display
        _payload, display = load_racing_request_preset(racing_requests_dir, preset_id)
        return display
    except (SoccerRequestPresetError, RacingRequestPresetError) as e:
        raise DownloadShellError(e.code, e.detail) from e


def presets_for_sport(sport: str) -> dict[str, str]:
    sport_n = normalize_sport(sport)
    if sport_n == SPORT_SOCCER:
        return dict(SOCCER_REQUEST_PRESETS)
    return dict(RACING_REQUEST_PRESETS)


def _parse_send_body(data: Any, raw_fallback: str) -> tuple[str, str]:
    """Return (request_key_or_empty, raw_payload_text)."""
    request_key = ""
    raw = raw_fallback or ""
    if isinstance(data, dict):
        rk = data.get("request_key")
        if isinstance(rk, str) and rk.strip():
            request_key = rk.strip()
        if isinstance(data.get("payload"), str):
            raw = data["payload"]
        elif "query" in data:
            raw = json.dumps(
                {
                    k: data[k]
                    for k in ("query", "variables", "operationName")
                    if k in data
                },
                ensure_ascii=False,
            )
    return request_key, raw


def _raw_payload_from_preset(
    requests_dir: Path,
    request_key: str,
    *,
    variables_override: dict[str, Any] | None = None,
) -> str:
    """Load whitelist preset display; optionally override variables only (query untouched)."""
    _payload, display = load_racing_request_preset(requests_dir, request_key)
    if not variables_override:
        return display
    obj = json.loads(display)
    if not isinstance(obj, dict):
        raise DownloadShellError("invalid_json", "preset display is not an object")
    vars_obj = dict(obj.get("variables") or {})
    for key, value in variables_override.items():
        vars_obj[key] = value
    obj["variables"] = vars_obj
    return json.dumps(obj, ensure_ascii=False)


def send_racing_download(
    *,
    db_path: Path,
    request_key: str,
    raw_payload: str,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    """Shared racing download entry: whitelist payload → locked download/write."""
    if request_key not in RACING_REQUEST_PRESETS:
        raise DownloadShellError("unsupported_type", request_key)
    try:
        payload = parse_racing_send_payload(raw_payload)
        assert_payload_matches_whitelist(request_key, payload)
    except RacingRequestPresetError as e:
        raise DownloadShellError(e.code, e.detail) from e

    variables = dict(payload.variables or {})
    race_date = variables.get("date")
    venue = variables.get("venueCode")
    race_date = race_date.strip() if isinstance(race_date, str) and race_date.strip() else None
    venue = venue.strip() if isinstance(venue, str) and venue.strip() else None

    t0 = time.perf_counter()
    try:
        if request_key == RACING_REQUEST_KEY_UPDATE_ODDS:
            result = run_odds_download_locked(
                db_path,
                race_date=race_date,
                venue_code=venue,
                progress_cb=progress_cb,
            )
        elif request_key == RACING_REQUEST_KEY_DAILY_RESULTS:
            result = run_daily_results_download_locked(
                db_path,
                race_date=race_date,
                venue_code=venue,
                progress_cb=progress_cb,
            )
        elif request_key == RACING_REQUEST_KEY_RESULTS_PERIOD:
            result = run_results_period_download_locked(
                db_path, variables=variables, progress_cb=progress_cb
            )
        elif request_key == RACING_REQUEST_KEY_HORSES:
            result = run_horses_download_locked(
                db_path, progress_cb=progress_cb
            )
        else:
            result = run_racecard_download_locked(
                db_path,
                race_date=race_date,
                venue_code=venue,
                fill_predicted=True,
                progress_cb=progress_cb,
            )
    except RacingDownloadError as e:
        raise DownloadShellError(e.code, e.detail) from e
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    detail = dict(result.detail or {})
    date_window = None
    if request_key == RACING_REQUEST_KEY_RESULTS_PERIOD:
        vars_out = detail.get("variables") or {}
        date_window = {
            "startDate": vars_out.get("startDate"),
            "endDate": vars_out.get("endDate"),
            "venueCodes": vars_out.get("venueCodes"),
        }

    ingest = {
        "status": "success" if result.code in {"success", "partial_failure"} else "failed",
        "code": result.code,
        "processed": result.progress.processed,
        "updated": result.progress.updated,
        "skipped": result.progress.skipped,
        "failed": result.progress.failed,
        "total": result.progress.total,
        "request_key": request_key,
        "detail": detail,
    }
    if date_window is not None:
        ingest["date_window"] = date_window
    return {
        "ok": ingest["status"] == "success",
        "outcome": "success" if ingest["status"] == "success" else "failed",
        "http_status": 200,
        "elapsed_ms": elapsed_ms,
        "request_key": request_key,
        "ingest": ingest,
        "processed": result.progress.processed,
        "total": result.progress.total,
        "updated": result.progress.updated,
        "skipped": result.progress.skipped,
        "failed": result.progress.failed,
        "message": result.progress.message,
        "race_date": result.progress.race_date,
        "venue_code": result.progress.venue_code,
        "detail": detail,
        "unit": "record",
    }


def execute_racing_download_from_preset(
    *,
    db_path: Path,
    requests_dir: Path,
    request_key: str,
    variables_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Racing non-UI entry (scheduler / Desktop / CLI):
    load whitelist preset → send_racing_download (same path as Web send).
    """
    raw = _raw_payload_from_preset(
        requests_dir, request_key, variables_override=variables_override
    )
    return send_racing_download(
        db_path=db_path, request_key=request_key, raw_payload=raw
    )


def _soccer_ingest_public_dict(db_path: Path, request_key: str) -> dict[str, Any]:
    ingest: dict[str, Any] = {
        "status": "not_attempted",
        "code": "",
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "had_complete": 0,
        "had_incomplete": 0,
        "skipped_missing": 0,
        "results_applied": False,
        "request_key": request_key,
        "ht_applied": 0,
        "ft_applied": 0,
        "sh_applied": 0,
        "skipped_status": 0,
        "skipped_unconfirmed": 0,
        "skipped_insufficient": 0,
        "skipped_no_kickoff": 0,
        "skipped_not_in_daily": 0,
        "daily_deleted": 0,
        "match_num_total": None,
        "matches_returned": None,
        "last_success_at": get_last_success_at(db_path, request_key),
        "last_success_at_hkt": None,
        "detail": "",
        "date_window": None,
        "fetch_mode": None,
        "history_scope": None,
        "download_status": None,
    }
    if ingest["last_success_at"]:
        try:
            ingest["last_success_at_hkt"] = format_hkt_display(
                str(ingest["last_success_at"])
            )
        except Exception:
            ingest["last_success_at_hkt"] = None
    return ingest


def _soccer_apply_ingest_result(
    ingest: dict[str, Any], ing: Any, db_path: Path, request_key: str
) -> None:
    ingest.update(
        {
            "status": "success" if ing.code == "success" else "skipped",
            "code": ing.code,
            "processed": ing.processed,
            "inserted": ing.inserted,
            "updated": ing.updated,
            "had_complete": ing.had_complete,
            "had_incomplete": ing.had_incomplete,
            "skipped_missing": getattr(ing, "skipped_missing", 0) or 0,
            "results_applied": ing.results_applied,
            "request_key": ing.request_key,
            "ht_applied": ing.ht_applied,
            "ft_applied": ing.ft_applied,
            "sh_applied": ing.sh_applied,
            "skipped_status": ing.skipped_status,
            "skipped_unconfirmed": ing.skipped_unconfirmed,
            "skipped_insufficient": getattr(ing, "skipped_insufficient", 0) or 0,
            "skipped_no_kickoff": getattr(ing, "skipped_no_kickoff", 0) or 0,
            "skipped_not_in_daily": getattr(ing, "skipped_not_in_daily", 0) or 0,
            "daily_deleted": getattr(ing, "daily_deleted", 0) or 0,
            "match_num_total": ing.match_num_total,
            "matches_returned": ing.matches_returned,
            "last_success_at": ing.last_success_at
            or get_last_success_at(db_path, request_key),
            "detail": ing.detail,
            "odds_changed": getattr(ing, "odds_changed", None),
            "odds_unchanged": getattr(ing, "odds_unchanged", None),
        }
    )
    if ingest["last_success_at"]:
        ingest["last_success_at_hkt"] = format_hkt_display(
            str(ingest["last_success_at"])
        )


def execute_soccer_official_results(
    *,
    db_path: Path,
    raw_payload: str,
    history_scope: str = HISTORY_SCOPE_PAYLOAD_ALL,
    chunk_dates: list[str] | None = None,
    acquire_job_lock: bool = True,
) -> dict[str, Any]:
    """Official match_results: shared entry → existing day-chunk download + history ingest."""
    from hppv.soccer_task_scheduler import (
        SoccerTaskError,
        download_match_results_complete,
        soccer_shared_job_lock,
    )

    try:
        payload = parse_and_validate_send_payload(raw_payload)
    except SoccerGraphQLSendError as e:
        raise DownloadShellError(
            e.code, e.detail, http_status=429 if e.code == "rate_limited" else 400
        ) from e

    vars_in = dict(payload.variables or {})
    start_date = vars_in.get("startDate")
    end_date = vars_in.get("endDate")
    if not isinstance(start_date, str) or not start_date.strip():
        raise DownloadShellError("invalid_fields", "startDate required")
    if not isinstance(end_date, str) or not end_date.strip():
        raise DownloadShellError("invalid_fields", "endDate required")
    start_date = start_date.strip()
    end_date = end_date.strip()

    ingest = _soccer_ingest_public_dict(db_path, REQUEST_KEY_MATCH_RESULTS)
    ingest["history_scope"] = history_scope
    ingest["date_window"] = {"startDate": start_date, "endDate": end_date}

    def _run() -> tuple[Any, dict[str, Any], dict[str, Any]]:
        response_obj, variables, fetch_meta = download_match_results_complete(
            payload,
            start_date=start_date,
            end_date=end_date,
            chunk_dates=chunk_dates,
        )
        ingest["download_status"] = "success"
        ingest["fetch_mode"] = fetch_meta.get("fetch_mode")
        if isinstance(fetch_meta.get("date_window"), dict):
            ingest["date_window"] = fetch_meta["date_window"]
        ing = ingest_official_results_to_history(
            db_path,
            response_obj,
            request_key=REQUEST_KEY_MATCH_RESULTS,
            variables=variables,
            history_scope=history_scope,
        )
        _soccer_apply_ingest_result(ingest, ing, db_path, REQUEST_KEY_MATCH_RESULTS)
        return response_obj, variables, fetch_meta

    try:
        if acquire_job_lock:
            with soccer_shared_job_lock():
                response_obj, variables, fetch_meta = _run()
        else:
            response_obj, variables, fetch_meta = _run()
    except SoccerTaskError as e:
        raise DownloadShellError(
            e.code, e.detail, http_status=429 if e.code == "rate_limited" else 400
        ) from e
    except SoccerIngestError as e:
        ingest["download_status"] = (
            "failed" if e.code == "incomplete_results" else "success"
        )
        ingest["status"] = "failed"
        ingest["code"] = e.code
        ingest["detail"] = e.detail
        return {
            "ok": True,
            "outcome": "success" if ingest["download_status"] == "success" else "http_error",
            "http_status": 200 if ingest["download_status"] == "success" else None,
            "elapsed_ms": 0,
            "body": "",
            "truncated": False,
            "match_count": ingest.get("matches_returned") or 0,
            "has_had_odds": False,
            "ingest": ingest,
            "request_key": REQUEST_KEY_MATCH_RESULTS,
        }

    body_text = json.dumps(response_obj, ensure_ascii=False)
    match_count = int(ingest.get("matches_returned") or fetch_meta.get("match_count") or 0)
    return {
        "ok": True,
        "outcome": "success",
        "http_status": fetch_meta.get("http_status"),
        "elapsed_ms": fetch_meta.get("elapsed_ms") or 0,
        "body": body_text,
        "truncated": bool(fetch_meta.get("truncated")),
        "match_count": match_count,
        "has_had_odds": False,
        "ingest": ingest,
        "request_key": REQUEST_KEY_MATCH_RESULTS,
    }


def execute_soccer_year_download(
    *,
    db_path: Path,
    raw_payload: str,
) -> dict[str, Any]:
    """Year history: shared entry → existing start_year_download_background / run_year_download."""
    from hppv.footylogic_history_year import (
        start_year_download_background,
        validate_year,
    )

    try:
        data = json.loads(raw_payload) if raw_payload.strip() else {}
    except json.JSONDecodeError as e:
        raise DownloadShellError("invalid_json", "year payload must be JSON object") from e
    if not isinstance(data, dict):
        raise DownloadShellError("invalid_json", "year payload must be JSON object")
    try:
        year = validate_year(data.get("year"))
    except (TypeError, ValueError) as e:
        raise DownloadShellError("invalid_year", "year required") from e
    resume = bool(data.get("resume"))
    limit = data.get("limit")
    limit_n: int | None = None
    if limit is not None and str(limit).strip() != "":
        try:
            limit_n = int(limit)
            if limit_n < 1:
                raise DownloadShellError("invalid_limit", "limit must be >= 1")
        except (TypeError, ValueError) as e:
            raise DownloadShellError("invalid_limit", "limit must be integer") from e

    out = start_year_download_background(
        db_path, year, resume=resume, limit=limit_n
    )
    if not out.get("ok") and out.get("error") == "already_running":
        raise DownloadShellError(
            "already_running",
            "year download already running",
            http_status=409,
        )
    return {
        "ok": True,
        "outcome": "started",
        "request_key": REQUEST_KEY_MATCH_RESULTS_BY_YEAR,
        "year": out.get("year"),
        "run_id": out.get("run_id"),
        "status": out.get("status"),
        "resume": out.get("resume"),
        "ingest": {
            "status": "started",
            "code": "started",
            "request_key": REQUEST_KEY_MATCH_RESULTS_BY_YEAR,
            "detail": "year download started in background",
        },
    }


def execute_footylogic_cli_run(
    *,
    db_path: Path,
    raw_payload: str,
) -> dict[str, Any]:
    """FootyLogic CLI run: shared entry → existing run_jobs (behaviour unchanged)."""
    from hppv.footylogic_client import FootylogicClient
    from hppv.footylogic_download import run_jobs

    try:
        data = json.loads(raw_payload) if raw_payload.strip() else {}
    except json.JSONDecodeError as e:
        raise DownloadShellError("invalid_json", "cli payload must be JSON object") from e
    if not isinstance(data, dict):
        raise DownloadShellError("invalid_json", "cli payload must be JSON object")
    limit = data.get("limit")
    limit_n: int | None = None
    if limit is not None and str(limit).strip() != "":
        try:
            limit_n = int(limit)
        except (TypeError, ValueError) as e:
            raise DownloadShellError("invalid_limit", "limit must be integer") from e
    dry_run = bool(data.get("dry_run"))
    stop_file = data.get("stop_file")
    stop_path = Path(str(stop_file)) if stop_file else None
    min_interval = data.get("min_interval")
    try:
        min_interval_f = float(min_interval) if min_interval is not None else 1.05
    except (TypeError, ValueError) as e:
        raise DownloadShellError("invalid_min_interval", "min_interval must be number") from e
    client = FootylogicClient(min_interval_sec=min_interval_f)
    stats = run_jobs(
        db_path,
        limit=limit_n,
        dry_run=dry_run,
        client=client,
        stop_flag=stop_path,
    )
    return {
        "ok": stats.get("failed", 0) == 0,
        "outcome": "success" if stats.get("failed", 0) == 0 else "failed",
        "request_key": REQUEST_KEY_FOOTYLOGIC_CLI_RUN,
        "stats": stats,
        "ingest": {
            "status": "success" if stats.get("failed", 0) == 0 else "failed",
            "code": "success" if stats.get("failed", 0) == 0 else "failed",
            "request_key": REQUEST_KEY_FOOTYLOGIC_CLI_RUN,
            "detail": "footylogic_cli_run",
            **{str(k): v for k, v in stats.items()},
        },
    }


def send_soccer_download(
    *,
    db_path: Path,
    request_key: str,
    raw_payload: str,
    wait_for_rate_limit: bool = False,
) -> dict[str, Any]:
    """Shared soccer GraphQL send + ingest (matches/odds paths)."""
    if request_key not in SOCCER_REQUEST_PRESETS:
        raise DownloadShellError("unsupported_type", request_key)
    if request_key == REQUEST_KEY_MATCH_RESULTS:
        return execute_soccer_official_results(db_path=db_path, raw_payload=raw_payload)
    if request_key == REQUEST_KEY_MATCH_RESULTS_BY_YEAR:
        return execute_soccer_year_download(db_path=db_path, raw_payload=raw_payload)

    try:
        payload = parse_and_validate_send_payload(raw_payload)
    except SoccerGraphQLSendError as e:
        raise DownloadShellError(
            e.code, e.detail, http_status=429 if e.code == "rate_limited" else 400
        ) from e

    try:
        result = send_soccer_graphql(
            payload, wait_for_rate_limit=wait_for_rate_limit
        )
    except SoccerGraphQLSendError as e:
        raise DownloadShellError(
            e.code, e.detail, http_status=429 if e.code == "rate_limited" else 400
        ) from e

    ingest = _soccer_ingest_public_dict(db_path, request_key)
    if result.truncated:
        ingest["status"] = "skipped"
        ingest["code"] = "truncated"
        ingest["detail"] = "response truncated"
    elif result.outcome != "success":
        ingest["status"] = "skipped"
        ingest["code"] = result.outcome
        ingest["detail"] = "response not eligible for ingest"
    else:
        try:
            response_obj = json.loads(result.body_text)
            if request_key == REQUEST_KEY_UPDATE_ODDS_AND_RESULTS:
                ing = update_odds_for_existing_matches(
                    db_path, response_obj, request_key=request_key
                )
            else:
                ing = ingest_matches_and_odds(
                    db_path, response_obj, request_key=request_key
                )
            _soccer_apply_ingest_result(ingest, ing, db_path, request_key)
        except SoccerIngestError as e:
            ingest["status"] = "failed"
            ingest["code"] = e.code
            ingest["detail"] = e.detail
        except Exception as e:
            logger.exception("soccer ingest unexpected: %s", e)
            ingest["status"] = "failed"
            ingest["code"] = "transaction_failed"
            ingest["detail"] = "unexpected ingest error"

    return {
        "ok": True,
        "outcome": result.outcome,
        "http_status": result.http_status,
        "elapsed_ms": result.elapsed_ms,
        "body": result.body_text,
        "truncated": result.truncated,
        "match_count": result.match_count,
        "has_had_odds": result.has_had_odds,
        "ingest": ingest,
        "request_key": request_key,
    }


def execute_download(
    *,
    sport: str,
    db_path: Path,
    request_key: str,
    raw_payload: str,
    wait_for_rate_limit: bool = False,
) -> dict[str, Any]:
    """Shared download entry: all sports / bypasses dispatch here."""
    sport_n = normalize_sport(sport)
    if sport_n == SPORT_RACING:
        return send_racing_download(
            db_path=db_path, request_key=request_key, raw_payload=raw_payload
        )
    if request_key == REQUEST_KEY_FOOTYLOGIC_CLI_RUN:
        return execute_footylogic_cli_run(db_path=db_path, raw_payload=raw_payload)
    return send_soccer_download(
        db_path=db_path,
        request_key=request_key,
        raw_payload=raw_payload,
        wait_for_rate_limit=wait_for_rate_limit,
    )


def execute_download_from_preset(
    *,
    sport: str,
    db_path: Path,
    requests_dir: Path,
    request_key: str,
    variables_override: dict[str, Any] | None = None,
    wait_for_rate_limit: bool = False,
) -> dict[str, Any]:
    """
    Shared non-UI entry (scheduler / Desktop / CLI):
    load sport whitelist preset → execute_download.
    """
    sport_n = normalize_sport(sport)
    if sport_n == SPORT_RACING:
        return execute_racing_download_from_preset(
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=request_key,
            variables_override=variables_override,
        )
    _payload, display = load_soccer_request_preset(requests_dir, request_key)
    raw = display
    if variables_override:
        obj = json.loads(display)
        if not isinstance(obj, dict):
            raise DownloadShellError("invalid_json", "preset display is not an object")
        vars_obj = dict(obj.get("variables") or {})
        for key, value in variables_override.items():
            vars_obj[key] = value
        obj["variables"] = vars_obj
        raw = json.dumps(obj, ensure_ascii=False)
    return execute_download(
        sport=SPORT_SOCCER,
        db_path=db_path,
        request_key=request_key,
        raw_payload=raw,
        wait_for_rate_limit=wait_for_rate_limit,
    )


def parse_request_key_and_payload(
    data: Any, raw_fallback: str, *, sport: str
) -> tuple[str, str]:
    sport_n = normalize_sport(sport)
    request_key, raw = _parse_send_body(data, raw_fallback)
    presets = presets_for_sport(sport_n)
    if not request_key:
        raise DownloadShellError("missing_request_key", "request_key is required")
    if request_key not in presets:
        raise DownloadShellError("unsupported_type", request_key)
    if not raw.strip():
        raise DownloadShellError("empty_query", "payload is empty")
    return request_key, raw
