"""Racing web task scheduler — reuses soccer timer/settings machinery.

Task rows live in ``soccer_task_settings`` (same schema; distinct task_key values).
Downloads go through ``execute_download_from_preset`` (same entry as Web send).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hppv.config import HKJC_MCS_HORSES_CLAS_URL_EN
from datetime import date

from hppv.racing_requests import (
    REQUEST_KEY_DAILY_RESULTS,
    REQUEST_KEY_DRAW_STATISTICS,
    REQUEST_KEY_HORSES,
    REQUEST_KEY_RACECARD,
    REQUEST_KEY_STANDARD_TIMES,
    REQUEST_KEY_UPDATE_ODDS,
)
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
TASK_KEY_UPDATE_DAILY_RESULTS = "racing_update_daily_results"
TASK_KEY_UPDATE_HORSES = "racing_update_horses"
TASK_KEY_UPDATE_STANDARD_TIMES = "racing_update_standard_times"
TASK_KEY_UPDATE_DRAW_STATISTICS = "racing_update_draw_statistics"

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

UPDATE_DAILY_RESULTS_SCOPE = {
    "operation": "raceMeetings",
    "endpoint": "https://info.cld.hkjc.com/graphql/base-get/",
    "target": "race_records",
    "write_mode": "insert_or_ignore",
    "unit": "record",
    "note": (
        "Same-day results via whitelist raceMeetings finalPosition → race_records "
        "(DD/MM/YY); skips when a complete localresults row exists for that horse/day; "
        "does not rewrite racecard."
    ),
    "multi_worker_safe": False,
}

UPDATE_HORSES_SCOPE = {
    "operation": "horseRatingList",
    "endpoint": HKJC_MCS_HORSES_CLAS_URL_EN,
    "target": "horses",
    "write_mode": "insert_or_fill_blanks",
    "unit": "record",
    "note": (
        "Horse basics from HKJC rating-list HTML → horses; "
        "inserts new rows and fills blank/placeholder names only; "
        "does not overwrite existing real names."
    ),
    "multi_worker_safe": False,
}

UPDATE_STANDARD_TIMES_SCOPE = {
    "operation": "standardTimesPdf",
    "endpoint": "res.hkjc.com",
    "target": "racing_standard_times",
    "write_mode": "insert_or_ignore",
    "unit": "year",
    "note": (
        "Yearly HKJC standard-times PDF for the current calendar year → "
        "racing_standard_times (published_on version + sectionals); "
        "unknown year URL fails without inventing paths."
    ),
    "multi_worker_safe": False,
}

UPDATE_DRAW_STATISTICS_SCOPE = {
    "operation": "drawStatisticsAdvSearch",
    "endpoint": "racing.hkjc.com/zh-hk/local/information/drawadvsearch",
    "target": "racing_draw_statistics",
    "write_mode": "insert_or_fill_meta",
    "unit": "record",
    "note": (
        "HKJC drawadvsearch Going=ALL for latest season (STT/STA/HVT) → "
        "racing_draw_statistics; unique key venue/track/rail/distance/draw."
    ),
    "multi_worker_safe": False,
}


def scope_for_racing_task(task_key: str) -> dict[str, Any]:
    if task_key == TASK_KEY_UPDATE_ODDS:
        return UPDATE_ODDS_SCOPE
    if task_key == TASK_KEY_UPDATE_DAILY_RESULTS:
        return UPDATE_DAILY_RESULTS_SCOPE
    if task_key == TASK_KEY_UPDATE_HORSES:
        return UPDATE_HORSES_SCOPE
    if task_key == TASK_KEY_UPDATE_STANDARD_TIMES:
        return UPDATE_STANDARD_TIMES_SCOPE
    if task_key == TASK_KEY_UPDATE_DRAW_STATISTICS:
        return UPDATE_DRAW_STATISTICS_SCOPE
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


def run_update_racecard_once(
    db_path: Path, requests_dir: Path, *, overwrite: bool = False
) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_RACECARD,
            overwrite=overwrite,
        )
    except DownloadShellError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    return _detail_from_send_body(body)


def run_update_racing_odds_once(
    db_path: Path, requests_dir: Path, *, overwrite: bool = False
) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_UPDATE_ODDS,
            overwrite=overwrite,
        )
    except DownloadShellError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    return _detail_from_send_body(body)


def run_update_daily_results_once(
    db_path: Path, requests_dir: Path, *, overwrite: bool = False
) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_DAILY_RESULTS,
            overwrite=overwrite,
        )
    except DownloadShellError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    return _detail_from_send_body(body)


def run_update_horses_once(
    db_path: Path, requests_dir: Path, *, overwrite: bool = False
) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_HORSES,
            overwrite=overwrite,
        )
    except DownloadShellError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    return _detail_from_send_body(body)


def run_update_standard_times_once(
    db_path: Path, requests_dir: Path, *, overwrite: bool = False
) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_STANDARD_TIMES,
            variables_override={"years": [date.today().year]},
            overwrite=overwrite,
        )
    except DownloadShellError as e:
        raise SoccerTaskError(e.code, e.detail) from e
    return _detail_from_send_body(body)


def run_update_draw_statistics_once(
    db_path: Path, requests_dir: Path, *, overwrite: bool = False
) -> dict[str, Any]:
    try:
        body = execute_download_from_preset(
            sport=SPORT_RACING,
            db_path=db_path,
            requests_dir=requests_dir,
            request_key=REQUEST_KEY_DRAW_STATISTICS,
            overwrite=overwrite,
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


class UpdateDailyResultsScheduler(_RacingSchedulerBase):
    task_key = TASK_KEY_UPDATE_DAILY_RESULTS
    _run_once = staticmethod(run_update_daily_results_once)


class UpdateHorsesScheduler(_RacingSchedulerBase):
    task_key = TASK_KEY_UPDATE_HORSES
    _run_once = staticmethod(run_update_horses_once)


class UpdateStandardTimesScheduler(_RacingSchedulerBase):
    task_key = TASK_KEY_UPDATE_STANDARD_TIMES
    _run_once = staticmethod(run_update_standard_times_once)


class UpdateDrawStatisticsScheduler(_RacingSchedulerBase):
    task_key = TASK_KEY_UPDATE_DRAW_STATISTICS
    _run_once = staticmethod(run_update_draw_statistics_once)


_racing_racecard_scheduler: UpdateRacecardScheduler | None = None
_racing_odds_scheduler: UpdateRacingOddsScheduler | None = None
_racing_daily_results_scheduler: UpdateDailyResultsScheduler | None = None
_racing_horses_scheduler: UpdateHorsesScheduler | None = None
_racing_standard_times_scheduler: UpdateStandardTimesScheduler | None = None
_racing_draw_statistics_scheduler: UpdateDrawStatisticsScheduler | None = None


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


def get_update_daily_results_scheduler(
    settings_db: Path, racing_db: Path, requests_dir: Path
) -> UpdateDailyResultsScheduler:
    global _racing_daily_results_scheduler
    if _racing_daily_results_scheduler is None:
        _racing_daily_results_scheduler = UpdateDailyResultsScheduler(
            settings_db, racing_db, requests_dir
        )
    return _racing_daily_results_scheduler


def get_update_horses_scheduler(
    settings_db: Path, racing_db: Path, requests_dir: Path
) -> UpdateHorsesScheduler:
    global _racing_horses_scheduler
    if _racing_horses_scheduler is None:
        _racing_horses_scheduler = UpdateHorsesScheduler(
            settings_db, racing_db, requests_dir
        )
    return _racing_horses_scheduler


def get_update_standard_times_scheduler(
    settings_db: Path, racing_db: Path, requests_dir: Path
) -> UpdateStandardTimesScheduler:
    global _racing_standard_times_scheduler
    if _racing_standard_times_scheduler is None:
        _racing_standard_times_scheduler = UpdateStandardTimesScheduler(
            settings_db, racing_db, requests_dir
        )
    return _racing_standard_times_scheduler


def get_update_draw_statistics_scheduler(
    settings_db: Path, racing_db: Path, requests_dir: Path
) -> UpdateDrawStatisticsScheduler:
    global _racing_draw_statistics_scheduler
    if _racing_draw_statistics_scheduler is None:
        _racing_draw_statistics_scheduler = UpdateDrawStatisticsScheduler(
            settings_db, racing_db, requests_dir
        )
    return _racing_draw_statistics_scheduler


def get_racing_task_public_state(
    settings_db: Path, task_key: str, *, running: bool = False
) -> dict[str, Any]:
    state = get_task_state(settings_db, task_key, running=running)
    return _attach_scope(state)
