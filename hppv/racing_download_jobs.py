"""In-process racing download jobs with live progress (Web polling).

Scheduler / CLI keep using sync ``send_racing_download``.
Web send starts a job; clients poll ``get_racing_download_job``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JOBS_LOCK = threading.RLock()
_JOBS: dict[str, "RacingDownloadJob"] = {}
_MAX_JOBS = 40


class RacingDownloadJobError(Exception):
    """Payload / start errors before the worker thread is accepted."""

    def __init__(self, code: str, detail: str = "", http_status: int = 400) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.http_status = http_status


@dataclass
class RacingDownloadJob:
    job_id: str
    request_key: str
    status: str = "queued"  # queued | running | success | failed
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    detail: str = ""
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None

    def to_public(self) -> dict[str, Any]:
        if self.result is not None:
            merged = dict(self.result)
            merged["job_id"] = self.job_id
            merged["job_status"] = self.status
            return merged

        if self.status == "failed":
            return {
                "ok": False,
                "job_id": self.job_id,
                "job_status": "failed",
                "request_key": self.request_key,
                "error": self.error or "read_failed",
                "detail": self.detail,
                "unit": "record",
                "progress": dict(self.progress),
            }

        prog = self.progress
        detail: dict[str, Any] = {
            "phase": prog.get("phase"),
            "operation": prog.get("operation"),
            "meetings_scanned": prog.get("meetings_scanned"),
            "meetings_planned": prog.get("meetings_planned"),
        }
        if prog.get("variables"):
            detail["variables"] = prog.get("variables")
        return {
            "ok": True,
            "job_id": self.job_id,
            "job_status": self.status,
            "request_key": self.request_key,
            "outcome": "running",
            "http_status": 200,
            "elapsed_ms": int(
                (
                    (self.finished_at or time.time())
                    - (self.started_at or self.created_at)
                )
                * 1000
            ),
            "processed": prog.get("processed", 0),
            "total": prog.get("total"),
            "updated": prog.get("updated", 0),
            "skipped": prog.get("skipped", 0),
            "failed": prog.get("failed", 0),
            "message": prog.get("message") or "",
            "race_date": prog.get("race_date"),
            "venue_code": prog.get("venue_code"),
            "unit": "record",
            "progress": dict(prog),
            "ingest": {
                "status": "running",
                "code": "running",
                "processed": prog.get("processed", 0),
                "updated": prog.get("updated", 0),
                "skipped": prog.get("skipped", 0),
                "failed": prog.get("failed", 0),
                "total": prog.get("total"),
                "request_key": self.request_key,
                "phase": prog.get("phase"),
                "date_window": (prog.get("variables") or None),
            },
            "detail": detail,
        }


def _prune_jobs_locked() -> None:
    if len(_JOBS) <= _MAX_JOBS:
        return
    done = [
        (jid, j)
        for jid, j in _JOBS.items()
        if j.status in {"success", "failed"}
    ]
    done.sort(key=lambda x: x[1].finished_at or x[1].created_at)
    while len(_JOBS) > _MAX_JOBS and done:
        jid, _ = done.pop(0)
        _JOBS.pop(jid, None)


def get_racing_download_job(job_id: str) -> RacingDownloadJob | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def start_racing_download_job(
    *,
    db_path: Path,
    request_key: str,
    raw_payload: str,
) -> RacingDownloadJob:
    """Validate payload quickly, then run send_racing_download in a daemon thread."""
    from hppv.racing_requests import (
        RACING_REQUEST_PRESETS,
        RacingRequestPresetError,
        assert_payload_matches_whitelist,
        parse_racing_send_payload,
    )

    if request_key not in RACING_REQUEST_PRESETS:
        raise RacingDownloadJobError("unsupported_type", request_key)
    try:
        payload = parse_racing_send_payload(raw_payload)
        assert_payload_matches_whitelist(request_key, payload)
    except RacingRequestPresetError as e:
        raise RacingDownloadJobError(e.code, e.detail) from e

    job_id = uuid.uuid4().hex
    job = RacingDownloadJob(
        job_id=job_id,
        request_key=request_key,
        status="queued",
        progress={
            "phase": "queued",
            "message": "已排隊",
            "processed": 0,
            "total": None,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
        },
    )
    with _JOBS_LOCK:
        _prune_jobs_locked()
        _JOBS[job_id] = job

    def _runner() -> None:
        with _JOBS_LOCK:
            job.status = "running"
            job.started_at = time.time()
            job.progress = {
                **job.progress,
                "phase": "starting",
                "message": "下載中…",
            }

        def progress_cb(pub: dict[str, Any]) -> None:
            with _JOBS_LOCK:
                job.progress = {
                    "phase": pub.get("phase") or job.progress.get("phase"),
                    "message": pub.get("message") or "",
                    "processed": pub.get("processed", 0),
                    "total": pub.get("total"),
                    "updated": pub.get("updated", 0),
                    "skipped": pub.get("skipped", 0),
                    "failed": pub.get("failed", 0),
                    "race_date": pub.get("race_date"),
                    "venue_code": pub.get("venue_code"),
                    "operation": pub.get("operation")
                    or job.progress.get("operation"),
                    "meetings_scanned": pub.get("meetings_scanned"),
                    "meetings_planned": pub.get("meetings_planned"),
                    "variables": pub.get("variables")
                    or job.progress.get("variables"),
                    "current_meeting": pub.get("current_meeting"),
                }

        try:
            result = _run_racing_download_for_job(
                db_path=db_path,
                request_key=request_key,
                raw_payload=raw_payload,
                progress_cb=progress_cb,
            )
            with _JOBS_LOCK:
                job.status = "success" if result.get("ok") else "failed"
                if not result.get("ok"):
                    job.error = str(
                        (result.get("ingest") or {}).get("code")
                        or result.get("outcome")
                        or "failed"
                    )
                job.result = result
                job.finished_at = time.time()
                job.progress = {
                    **job.progress,
                    "phase": "done",
                    "message": result.get("message") or "完成",
                    "processed": result.get("processed", 0),
                    "total": result.get("total"),
                    "updated": result.get("updated", 0),
                    "skipped": result.get("skipped", 0),
                    "failed": result.get("failed", 0),
                    "race_date": result.get("race_date"),
                    "venue_code": result.get("venue_code"),
                }
        except RacingDownloadJobError as e:
            logger.warning("racing download job failed: %s %s", e.code, e.detail)
            with _JOBS_LOCK:
                job.status = "failed"
                job.error = e.code
                job.detail = e.detail
                job.finished_at = time.time()
                job.progress = {
                    **job.progress,
                    "phase": "failed",
                    "message": e.detail or e.code,
                }
        except Exception as e:
            # RacingDownloadError / others from runners.
            code = getattr(e, "code", None) or "read_failed"
            detail = getattr(e, "detail", None) or str(e)
            logger.exception("racing download job crashed")
            with _JOBS_LOCK:
                job.status = "failed"
                job.error = str(code)
                job.detail = str(detail)
                job.finished_at = time.time()
                job.progress = {
                    **job.progress,
                    "phase": "failed",
                    "message": str(detail),
                }

    threading.Thread(
        target=_runner, name=f"racing-dl-{job_id[:8]}", daemon=True
    ).start()
    return job


def _run_racing_download_for_job(
    *,
    db_path: Path,
    request_key: str,
    raw_payload: str,
    progress_cb: Any,
) -> dict[str, Any]:
    """Dispatch racing runners with progress (no soccer/web_download_shell import)."""
    from hppv.racing_download import (
        RacingDownloadError,
        run_daily_results_download_locked,
        run_horses_download_locked,
        run_odds_download_locked,
        run_racecard_download_locked,
        run_results_period_download_locked,
    )
    from hppv.racing_requests import (
        REQUEST_KEY_DAILY_RESULTS,
        REQUEST_KEY_HORSES,
        REQUEST_KEY_RESULTS_PERIOD,
        REQUEST_KEY_UPDATE_ODDS,
        RacingRequestPresetError,
        assert_payload_matches_whitelist,
        parse_racing_send_payload,
    )

    try:
        payload = parse_racing_send_payload(raw_payload)
        assert_payload_matches_whitelist(request_key, payload)
    except RacingRequestPresetError as e:
        raise RacingDownloadJobError(e.code, e.detail) from e

    variables = dict(payload.variables or {})
    race_date = variables.get("date")
    venue = variables.get("venueCode")
    race_date = (
        race_date.strip()
        if isinstance(race_date, str) and race_date.strip()
        else None
    )
    venue = venue.strip() if isinstance(venue, str) and venue.strip() else None

    t0 = time.perf_counter()
    try:
        if request_key == REQUEST_KEY_UPDATE_ODDS:
            result = run_odds_download_locked(
                db_path,
                race_date=race_date,
                venue_code=venue,
                progress_cb=progress_cb,
            )
        elif request_key == REQUEST_KEY_DAILY_RESULTS:
            result = run_daily_results_download_locked(
                db_path,
                race_date=race_date,
                venue_code=venue,
                progress_cb=progress_cb,
            )
        elif request_key == REQUEST_KEY_RESULTS_PERIOD:
            result = run_results_period_download_locked(
                db_path, variables=variables, progress_cb=progress_cb
            )
        elif request_key == REQUEST_KEY_HORSES:
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
        raise RacingDownloadJobError(e.code, e.detail) from e
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    detail = dict(result.detail or {})
    date_window = None
    if request_key == REQUEST_KEY_RESULTS_PERIOD:
        vars_out = detail.get("variables") or {}
        date_window = {
            "startDate": vars_out.get("startDate"),
            "endDate": vars_out.get("endDate"),
            "venueCodes": vars_out.get("venueCodes"),
        }
    ingest: dict[str, Any] = {
        "status": "success"
        if result.code in {"success", "partial_failure"}
        else "failed",
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
