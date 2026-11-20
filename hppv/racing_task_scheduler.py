"""Racing web task scheduler — reuses soccer timer/settings machinery.

Task rows live in ``soccer_task_settings`` (same schema; distinct task_key values).
Downloads go through ``execute_download_from_preset`` (same entry as Web send).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hppv.racing_requests import REQUEST_KEY_RACECARD, REQUEST_KEY_UPDATE_ODDS
from hppv.soccer_task_scheduler import (
    SoccerTaskError,
    SoccerTaskState,
    _TaskSchedulerBase,
    get_task_state,
)
from hppv.web_download_shell import (
    SPORT_RACING,
    DownloadShellError,
    execute_download_from_preset,
)

logger = logging.getLogger(__name__)

TASK_KEY_UPDATE_RACECARD = "racing_update_racecard"
TASK_KEY_UPDATE_ODDS = "racing_update_odds"

UPDATE_RACECARD_SCOPE = {
    "operation": "raceMeetings",
    "endpoint": "https://info.cld.hkjc.com/graphql/base-get/",
    "unit": "record",
    "note": (
        "Downloads racecard via whitelist raceMeetings; updates mapped columns only; "
        "does not delete starters; does not write results."
    ),
    "multi_worker_safe": False,
}

UPDATE_ODDS_SCOPE = {
    "operation": "sp4e_racing_pmPool",
    "odds_path": "WIN.oddsNodes",
    "endpoint": "https://info.cld.hkjc.com/graphql/base-get/",
    "unit": "record",
    "note": (
        "Single odds path: sp4e_racing_pmPool WIN oddsNodes → 即時賠率. "
        "No winOdds fallback. Missing values skip without clearing."
    ),
    "multi_worker_safe": False,
}


def scope_for_racing_task(task_key: str) -> dict[str, Any]:
    if task_key == TASK_KEY_UPDATE_ODDS:
        return UPDATE_ODDS_SCOPE
    return UPDATE_RACECARD_SCOPE


def _attach_scope(state: SoccerTaskState) -> dict[str, Any]:
    pub = state.to_public_dict()
    pub["scope"] = scope_for_racing_task(state.task_key)
    return pub


def _detail_from_send_body(body: dict[str, Any]) -> dict[str, Any]:
    detail = dict(body.get("detail") or {})
    detail["ingest_status"] = (
        "success" if body.get("ok") and body.get("outcome") == "success" else "failed"
    )
    code = (body.get("ingest") or {}).get("code") or body.get("outcome")
    if code == "partial_failure":
        detail["ingest_status"] = "success"
        detail["partial"] = True
    detail["processed"] = body.get("processed")
    detail["total"] = body.get("total")
    detail["unit"] = body.get("unit") or "record"
    return detail


def run_update_racecard_once(db_path: Path, requests_dir: Path) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_RACECARD,
        )
    except DownloadShellError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    return _detail_from_send_body(body)


def run_update_racing_odds_once(db_path: Path, requests_dir: Path) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_UPDATE_ODDS,
        )
    except DownloadShellError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    return _detail_from_send_body(body)


class _RacingSchedulerBase(_TaskSchedulerBase):
    """Settings DB may differ from racing data DB; requests_dir holds whitelist presets."""

    def __init__(
        self, settings_db: Path, racing_db: Path, requests_dir: Path
    ) -> None:
        super().__init__(
            racing_db, requests_dir, settings_db=settings_db
        )
        self.racing_db = Path(racing_db)


class UpdateRacecardScheduler(_RacingSchedulerBase):
    task_key = TASK_KEY_UPDATE_RACECARD
    _run_once = staticmethod(run_update_racecard_once)


class UpdateRacingOddsScheduler(_RacingSchedulerBase):
    task_key = TASK_KEY_UPDATE_ODDS
    _run_once = staticmethod(run_update_racing_odds_once)


_racing_racecard_scheduler: UpdateRacecardScheduler | None = None
_racing_odds_scheduler: UpdateRacingOddsScheduler | None = None


def get_update_racecard_scheduler(
    settings_db: Path, racing_db: Path, requests_dir: Path
) -> UpdateRacecardScheduler:
    global _racing_racecard_scheduler
    if _racing_racecard_scheduler is None:
        _racing_racecard_scheduler = UpdateRacecardScheduler(
            settings_db, racing_db, requests_dir
        )
    return _racing_racecard_scheduler


def get_update_racing_odds_scheduler(
    settings_db: Path, racing_db: Path, requests_dir: Path
) -> UpdateRacingOddsScheduler:
    global _racing_odds_scheduler
    if _racing_odds_scheduler is None:
        _racing_odds_scheduler = UpdateRacingOddsScheduler(
            settings_db, racing_db, requests_dir
        )
    return _racing_odds_scheduler


def get_racing_task_public_state(
    settings_db: Path, task_key: str, *, running: bool = False
) -> dict[str, Any]:
    state = get_task_state(settings_db, task_key, running=running)
    return _attach_scope(state)
