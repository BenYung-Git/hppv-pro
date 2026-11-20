"""Shared racing download runners (Web / Desktop / CLI / scheduler).

Racecard: whitelist ``raceMeetings`` only.
Odds: single path ``sp4e_racing_pmPool`` WIN ``oddsNodes`` only (no winOdds fallback).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hppv.db import get_connection, init_db
from hppv.racing_hkjc_graphql import (
    GRAPHQL_URL,
    ActiveMeeting,
    RacingGraphQLError,
    fetch_race_meetings,
    fetch_sp4e_pm_pool,
    extract_win_odds_from_sp4e_pools,
    pick_active_meeting,
    race_info_from_graphql,
    resolve_latest_meeting_from_graphql,
    starter_fields_from_graphql,
    to_db_date,
    to_graphql_date,
    update_immediate_win_odds,
    upsert_race_info_from_graphql,
    upsert_starter_from_graphql,
)
from hppv.schema import (
    RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS,
    ensure_racecard_starters_columns,
    racecard_base_schema_sql,
)

logger = logging.getLogger(__name__)

# Process-local lock shared by manual Web + scheduled racing jobs.
_RACING_JOB_LOCK = threading.RLock()

ProgressCb = Callable[[dict[str, Any]], None]


class RacingDownloadError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass
class RacingDownloadProgress:
    phase: str = "starting"
    total: int | None = None
    processed: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    message: str = ""
    race_date: str | None = None
    venue_code: str | None = None

    def to_public(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "total": self.total,
            "processed": self.processed,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "message": self.message,
            "race_date": self.race_date,
            "venue_code": self.venue_code,
            "unit": "record",
        }


@dataclass
class RacingDownloadResult:
    code: str
    progress: RacingDownloadProgress
    detail: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        out = {
            "code": self.code,
            "ok": self.code == "success",
            **self.progress.to_public(),
            "detail": self.detail,
        }
        return out


def racing_shared_job_lock() -> threading.RLock:
    return _RACING_JOB_LOCK


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_racecard_db(db_path: Path) -> None:
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        conn.executescript(racecard_base_schema_sql())
        ensure_racecard_starters_columns(
            conn=conn,
            starters_table="racecard_starters",
            display_fields=RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS,
            predicted_odds_col="預測賠率",
            immediate_odds_col="即時賠率",
        )
        conn.commit()
    finally:
        conn.close()


def _emit(cb: ProgressCb | None, prog: RacingDownloadProgress) -> None:
    if cb is None:
        return
    try:
        cb(prog.to_public())
    except Exception:
        logger.exception("racing download progress callback failed")


def _count_runners(meeting: dict[str, Any]) -> int:
    n = 0
    for race in meeting.get("races") or []:
        for runner in race.get("runners") or []:
            fields = starter_fields_from_graphql(runner)
            if fields.get("馬號") or fields.get("horse_id"):
                n += 1
    return n


def _write_meta(
    conn: Any,
    *,
    race_date: str,
    venue: str,
    race_nos: list[int],
) -> None:
    now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    race_nos_csv = ",".join(str(n) for n in race_nos)
    conn.execute(
        """
        INSERT INTO racecard_meta(key, value)
        VALUES('last_update_at', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """,
        (now_txt,),
    )
    conn.execute(
        """
        INSERT INTO racecard_meta(key, value) VALUES
          ('last_source_race_date', ?),
          ('last_source_racecourse_code', ?),
          ('last_source_race_nos', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """,
        (race_date, venue, race_nos_csv),
    )


def resolve_meeting_for_download(
    *,
    race_date: str | None = None,
    venue_code: str | None = None,
) -> ActiveMeeting:
    """Resolve meeting from GraphQL; date/venue optional (defaults to active)."""
    try:
        if race_date and venue_code:
            data = fetch_race_meetings(race_date, venue_code)
            active = pick_active_meeting({"raceMeetings": data.get("raceMeetings") or []})
            # Prefer requested meeting from raceMeetings list when present.
            meetings = data.get("raceMeetings") or []
            if meetings and meetings[0].get("date") and meetings[0].get("venueCode"):
                m = meetings[0]
                return ActiveMeeting(
                    date_iso=str(m["date"]),
                    venue_code=str(m["venueCode"]),
                    status=(str(m["status"]) if m.get("status") is not None else None),
                    total_number_of_race=(
                        int(m["totalNumberOfRace"])
                        if m.get("totalNumberOfRace") is not None
                        else len(m.get("races") or [])
                    ),
                )
            if active:
                return active
            raise RacingDownloadError("empty_meeting", "raceMeetings empty")
        active = resolve_latest_meeting_from_graphql(
            hint_date=race_date, hint_venue=venue_code
        )
        if not active:
            raise RacingDownloadError("empty_meeting", "no active meeting")
        return active
    except RacingGraphQLError as e:
        raise RacingDownloadError("graphql_error", str(e)) from e


def run_racecard_download(
    db_path: Path,
    *,
    race_date: str | None = None,
    venue_code: str | None = None,
    progress_cb: ProgressCb | None = None,
    fill_predicted: bool = True,
) -> RacingDownloadResult:
    """Download racecard via raceMeetings; count unit = horse record."""
    ensure_racecard_db(db_path)
    prog = RacingDownloadProgress(phase="fetching_total", message="正在取得總數")
    _emit(progress_cb, prog)
    started = utc_now_iso()

    try:
        meeting_meta = resolve_meeting_for_download(
            race_date=race_date, venue_code=venue_code
        )
        data = fetch_race_meetings(meeting_meta.date_iso, meeting_meta.venue_code)
    except RacingGraphQLError as e:
        raise RacingDownloadError("graphql_error", str(e)) from e

    meetings = data.get("raceMeetings") or []
    if not meetings:
        raise RacingDownloadError("empty_meeting", "raceMeetings empty")
    meeting = meetings[0]
    db_date = to_db_date(str(meeting.get("date") or meeting_meta.date_iso))
    venue = str(meeting.get("venueCode") or meeting_meta.venue_code)
    prog.race_date = db_date
    prog.venue_code = venue
    prog.total = _count_runners(meeting)
    prog.phase = "processing"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    race_nos: list[int] = []
    conn = get_connection(db_path)
    try:
        for race in meeting.get("races") or []:
            race_no = race.get("no")
            if race_no is None:
                continue
            rn = int(race_no)
            race_nos.append(rn)
            info = race_info_from_graphql(meeting, race)
            try:
                upsert_race_info_from_graphql(
                    conn,
                    race_date=db_date,
                    racecourse=venue,
                    race_no=rn,
                    info=info,
                )
            except Exception as e:
                logger.exception("race_info upsert failed race=%s", rn)
                prog.failed += 1
                prog.processed += 1
                prog.message = f"已處理 {prog.processed}/{prog.total} record"
                _emit(progress_cb, prog)
                continue

            for runner in race.get("runners") or []:
                fields = starter_fields_from_graphql(runner)
                if not fields.get("馬號") and not fields.get("horse_id"):
                    continue
                try:
                    before = conn.execute(
                        """
                        SELECT id, "馬名" AS name FROM racecard_starters
                         WHERE race_date=? AND racecourse=? AND race_no=?
                           AND 後備=? AND (
                             (horse_id IS NOT NULL AND horse_id=?)
                             OR ("馬號" IS NOT NULL AND "馬號"=?)
                           )
                         LIMIT 1;
                        """,
                        (
                            db_date,
                            venue,
                            rn,
                            fields.get("後備") or "否",
                            fields.get("horse_id"),
                            fields.get("馬號"),
                        ),
                    ).fetchone()
                    upsert_starter_from_graphql(
                        conn,
                        race_date=db_date,
                        racecourse=venue,
                        race_no=rn,
                        fields=fields,
                    )
                    if before is None:
                        prog.updated += 1
                    else:
                        # Treat successful upsert as updated when any mapped field present.
                        prog.updated += 1
                except Exception as e:
                    logger.exception("starter upsert failed")
                    prog.failed += 1
                prog.processed += 1
                prog.message = f"已處理 {prog.processed}/{prog.total} record"
                _emit(progress_cb, prog)

        if fill_predicted:
            # Lazy import: desktop helper pulls Qt; keep Web/CLI free of UI deps.
            from hppv.ui.racecard_rank_viewer import (
                _fill_predicted_win_odds_for_meeting,
            )

            _fill_predicted_win_odds_for_meeting(conn, db_date, venue)
        _write_meta(conn, race_date=db_date, venue=venue, race_nos=sorted(set(race_nos)))
        conn.commit()
    finally:
        conn.close()

    prog.phase = "done"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)
    detail = {
        "started_at": started,
        "finished_at": utc_now_iso(),
        "operation": "raceMeetings",
        "endpoint": GRAPHQL_URL,
        "variables": {
            "date": meeting_meta.date_iso,
            "venueCode": meeting_meta.venue_code,
        },
        "race_nos": sorted(set(race_nos)),
        "ingest_status": "success",
        "processed": prog.processed,
        "updated": prog.updated,
        "skipped": prog.skipped,
        "failed": prog.failed,
        "total": prog.total,
    }
    code = "success" if prog.failed == 0 else "partial_failure"
    return RacingDownloadResult(code=code, progress=prog, detail=detail)


def run_odds_download(
    db_path: Path,
    *,
    race_date: str | None = None,
    venue_code: str | None = None,
    progress_cb: ProgressCb | None = None,
) -> RacingDownloadResult:
    """Update 即時賠率 via sp4e WIN oddsNodes only (single path)."""
    ensure_racecard_db(db_path)
    prog = RacingDownloadProgress(phase="fetching_total", message="正在取得總數")
    _emit(progress_cb, prog)
    started = utc_now_iso()

    try:
        meeting_meta = resolve_meeting_for_download(
            race_date=race_date, venue_code=venue_code
        )
        # Need race list / declared horses from raceMeetings (structure), odds from sp4e.
        data = fetch_race_meetings(meeting_meta.date_iso, meeting_meta.venue_code)
    except RacingGraphQLError as e:
        raise RacingDownloadError("graphql_error", str(e)) from e

    meetings = data.get("raceMeetings") or []
    if not meetings:
        raise RacingDownloadError("empty_meeting", "raceMeetings empty")
    meeting = meetings[0]
    db_date = to_db_date(str(meeting.get("date") or meeting_meta.date_iso))
    venue = str(meeting.get("venueCode") or meeting_meta.venue_code)
    prog.race_date = db_date
    prog.venue_code = venue

    # Total = non-standby runners with a horse number (odds unit).
    targets: list[tuple[int, str]] = []
    race_nos: list[int] = []
    for race in meeting.get("races") or []:
        if race.get("no") is None:
            continue
        rn = int(race["no"])
        race_nos.append(rn)
        for runner in race.get("runners") or []:
            fields = starter_fields_from_graphql(runner)
            if fields.get("後備") == "是":
                continue
            horse_no = fields.get("馬號")
            if not horse_no:
                continue
            targets.append((rn, str(horse_no)))

    prog.total = len(targets)
    prog.phase = "processing"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    # Fetch WIN pool per race (single odds path).
    odds_map: dict[tuple[int, str], str] = {}
    fetch_errors: list[dict[str, Any]] = []
    for rn in sorted(set(race_nos)):
        try:
            pool_data = fetch_sp4e_pm_pool(
                meeting_meta.date_iso,
                venue,
                odds_types=("WIN",),
                race_no=rn,
            )
            odds_map.update(extract_win_odds_from_sp4e_pools(pool_data))
        except RacingGraphQLError as e:
            fetch_errors.append({"race_no": rn, "error": str(e)})
            logger.warning("sp4e WIN fetch failed race=%s: %s", rn, e)

    conn = get_connection(db_path)
    try:
        for rn, horse_no in targets:
            key = (rn, str(int(horse_no)) if str(horse_no).isdigit() else horse_no)
            odds = odds_map.get(key) or odds_map.get((rn, horse_no))
            if odds is None:
                # Missing source value → skip, keep existing; still counts as processed.
                prog.skipped += 1
                prog.processed += 1
                prog.message = f"已處理 {prog.processed}/{prog.total} record"
                _emit(progress_cb, prog)
                continue
            try:
                n = update_immediate_win_odds(
                    conn,
                    race_date=db_date,
                    racecourse=venue,
                    race_no=rn,
                    horse_no=str(int(horse_no)) if str(horse_no).isdigit() else horse_no,
                    win_odds=odds,
                )
                if n:
                    prog.updated += 1
                else:
                    # No matching starter row — report as skipped (not success cover-up).
                    prog.skipped += 1
            except Exception:
                logger.exception("odds update failed")
                prog.failed += 1
            prog.processed += 1
            prog.message = f"已處理 {prog.processed}/{prog.total} record"
            _emit(progress_cb, prog)
        conn.commit()
    finally:
        conn.close()

    prog.phase = "done"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    detail = {
        "started_at": started,
        "finished_at": utc_now_iso(),
        "operation": "sp4e_racing_pmPool",
        "endpoint": GRAPHQL_URL,
        "odds_path": "WIN.oddsNodes",
        "variables_template": {
            "date": meeting_meta.date_iso,
            "venueCode": venue,
            "oddsTypes": ["WIN"],
            "raceNo": "<per-race>",
        },
        "race_nos": sorted(set(race_nos)),
        "fetch_errors": fetch_errors,
        "ingest_status": "success" if not fetch_errors or prog.updated > 0 else "failed",
        "processed": prog.processed,
        "updated": prog.updated,
        "skipped": prog.skipped,
        "failed": prog.failed,
        "total": prog.total,
    }
    if fetch_errors and prog.updated == 0 and prog.processed > 0 and prog.skipped == prog.processed:
        # All skipped due to fetch failure / missing odds — honest failure if every race fetch failed.
        if len(fetch_errors) >= len(set(race_nos)):
            raise RacingDownloadError(
                "odds_fetch_failed",
                f"sp4e WIN failed for all races: {fetch_errors[0].get('error')}",
            )
    code = "success" if prog.failed == 0 else "partial_failure"
    return RacingDownloadResult(code=code, progress=prog, detail=detail)


def run_racecard_download_locked(
    db_path: Path, **kwargs: Any
) -> RacingDownloadResult:
    with _RACING_JOB_LOCK:
        return run_racecard_download(db_path, **kwargs)


def run_odds_download_locked(db_path: Path, **kwargs: Any) -> RacingDownloadResult:
    with _RACING_JOB_LOCK:
        return run_odds_download(db_path, **kwargs)
