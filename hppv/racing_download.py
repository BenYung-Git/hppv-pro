"""Shared racing download runners (Web / Desktop / CLI / scheduler).

Racecard: whitelist ``raceMeetings`` only.
Odds: single path ``sp4e_racing_pmPool`` WIN ``oddsNodes`` only (no winOdds fallback).
Daily results: same ``raceMeetings`` whitelist → ``race_records`` via INSERT OR IGNORE;
also insert/fill ``horses`` names from runners (incl. WV).
Results period: verified localresults HTML (startDate/endDate × venueCodes);
also insert/fill ``horses`` from 馬名 cells.
Horses: verified HKJC rating-list HTML → ``horses`` via insert_or_fill.
Standard times: verified yearly PDF → ``racing_standard_times`` upsert.
Draw statistics: verified drawadvsearch HTML → ``racing_draw_statistics`` upsert.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hppv.config import HKJC_MCS_HORSES_CLAS_URL_EN
from hppv.db import (
    get_connection,
    init_db,
    insert_or_fill_horse,
    is_acceptable_horse_id,
    upsert_race_records,
)
from hppv.racing_requests import HORSES_OPERATION, HORSES_QUERY
from hppv.scraper import fetch_all_horses_basic_info
from hppv.racing_hkjc_graphql import (
    GRAPHQL_URL,
    ActiveMeeting,
    RacingGraphQLError,
    fetch_race_meetings,
    fetch_sp4e_pm_pool,
    extract_win_odds_from_sp4e_pools,
    pick_active_meeting,
    race_info_from_graphql,
    horse_rows_from_meeting,
    race_records_from_meeting,
    resolve_latest_meeting_from_graphql,
    starter_fields_from_graphql,
    to_db_date,
    to_graphql_date,
    update_immediate_win_odds,
    upsert_race_info_from_graphql,
    upsert_starter_from_graphql,
)
from hppv.racing_local_results import (
    LOCAL_RESULTS_URL,
    RacingLocalResultsError,
    collect_period_race_records,
    iter_dates_inclusive,
    parse_results_period_variables,
    to_localresults_date,
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


def run_daily_results_download(
    db_path: Path,
    *,
    race_date: str | None = None,
    venue_code: str | None = None,
    progress_cb: ProgressCb | None = None,
    overwrite: bool = False,
) -> RacingDownloadResult:
    """Write finished raceMeetings runners into race_records (ignore or overwrite)."""
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

    records = race_records_from_meeting(meeting)
    horse_rows = horse_rows_from_meeting(meeting)
    horses_filled = 0
    for hrow in horse_rows:
        try:
            if insert_or_fill_horse(db_path, hrow, overwrite=overwrite) in (
                "inserted",
                "filled",
            ):
                horses_filled += 1
        except Exception:
            logger.exception(
                "horses insert/fill from results failed horse=%s",
                hrow.get("horse_id"),
            )

    prog.total = len(records)
    prog.phase = "processing"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    # Insert one-by-one so updated/skipped counts stay honest under OR IGNORE.
    for rec in records:
        try:
            n = upsert_race_records(db_path, [rec], overwrite=overwrite)
            if n:
                prog.updated += 1
            else:
                prog.skipped += 1
        except Exception:
            logger.exception("race_records insert failed horse=%s", rec.horse_id)
            prog.failed += 1
        prog.processed += 1
        prog.message = f"已處理 {prog.processed}/{prog.total} record"
        _emit(progress_cb, prog)

    prog.phase = "done"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)
    detail = {
        "started_at": started,
        "finished_at": utc_now_iso(),
        "operation": "raceMeetings",
        "endpoint": GRAPHQL_URL,
        "target": "race_records",
        "write_mode": "overwrite" if overwrite else "insert_or_ignore",
        "overwrite": bool(overwrite),
        "horses_write_mode": (
            "overwrite" if overwrite else "insert_or_fill_blanks"
        ),
        "horses_filled": horses_filled,
        "variables": {
            "date": meeting_meta.date_iso,
            "venueCode": meeting_meta.venue_code,
        },
        "ingest_status": "success",
        "processed": prog.processed,
        "updated": prog.updated,
        "skipped": prog.skipped,
        "failed": prog.failed,
        "total": prog.total,
    }
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


def run_daily_results_download_locked(
    db_path: Path, **kwargs: Any
) -> RacingDownloadResult:
    with _RACING_JOB_LOCK:
        return run_daily_results_download(db_path, **kwargs)


def run_results_period_download(
    db_path: Path,
    *,
    variables: dict[str, Any] | None = None,
    progress_cb: ProgressCb | None = None,
    overwrite: bool = False,
) -> RacingDownloadResult:
    """Write localresults HTML runners for a date window into race_records."""
    ensure_racecard_db(db_path)
    prog = RacingDownloadProgress(phase="fetching_total", message="正在取得總數")
    _emit(progress_cb, prog)
    started = utc_now_iso()

    try:
        start, end, venues = parse_results_period_variables(variables)
    except RacingLocalResultsError as e:
        raise RacingDownloadError(e.code, e.detail) from e

    prog.race_date = f"{to_localresults_date(start)}..{to_localresults_date(end)}"
    prog.venue_code = ",".join(venues)

    meetings_planned = len(iter_dates_inclusive(start, end)) * max(len(venues), 1)
    prog.total = meetings_planned
    prog.processed = 0
    prog.phase = "fetching"
    prog.message = f"掃描賽日 0/{meetings_planned}"
    _emit(
        progress_cb,
        prog,
    )
    # Attach extras for job polling (formatters read via to_public keys only;
    # pass through a side dict on the callback payload).
    def _emit_fetch(extra: dict[str, Any] | None = None) -> None:
        pub = prog.to_public()
        pub["operation"] = "localResultsPeriod"
        pub["meetings_planned"] = meetings_planned
        pub["meetings_scanned"] = prog.processed
        pub["variables"] = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "venueCodes": list(venues),
        }
        if extra:
            pub.update(extra)
        if progress_cb is not None:
            try:
                progress_cb(pub)
            except Exception:
                logger.exception("racing period progress callback failed")

    _emit_fetch()

    try:
        def _on_meeting(meta: dict[str, Any]) -> None:
            prog.processed += 1
            prog.phase = "fetching"
            day = meta.get("race_date") or ""
            venue = meta.get("venue_code") or ""
            found = int(meta.get("record_count") or 0)
            prog.message = (
                f"掃描賽日 {prog.processed}/{meetings_planned}"
                f"：{day} {venue}（+{found}）"
            )
            _emit_fetch(
                {
                    "current_meeting": f"{day} {venue}",
                    "meetings_scanned": prog.processed,
                }
            )

        records, horse_rows, meeting_metas = collect_period_race_records(
            start=start,
            end=end,
            venue_codes=venues,
            on_meeting=_on_meeting,
        )
    except RacingLocalResultsError as e:
        raise RacingDownloadError(e.code, e.detail) from e
    except Exception as e:
        raise RacingDownloadError("localresults_fetch_failed", str(e)) from e

    horses_filled = 0
    for hrow in horse_rows:
        try:
            if insert_or_fill_horse(db_path, hrow, overwrite=overwrite) in (
                "inserted",
                "filled",
            ):
                horses_filled += 1
        except Exception:
            logger.exception(
                "horses insert/fill from results failed horse=%s",
                hrow.get("horse_id"),
            )

    prog.total = len(records)
    prog.processed = 0
    prog.updated = 0
    prog.skipped = 0
    prog.failed = 0
    prog.phase = "processing"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    for rec in records:
        try:
            n = upsert_race_records(db_path, [rec], overwrite=overwrite)
            if n:
                prog.updated += 1
            else:
                prog.skipped += 1
        except Exception:
            logger.exception("race_records insert failed horse=%s", rec.horse_id)
            prog.failed += 1
        prog.processed += 1
        prog.message = f"已處理 {prog.processed}/{prog.total} record"
        _emit(progress_cb, prog)

    prog.phase = "done"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)
    meetings_with_results = sum(
        1 for m in meeting_metas if int(m.get("races_with_results") or 0) > 0
    )
    detail = {
        "started_at": started,
        "finished_at": utc_now_iso(),
        "operation": "localResultsPeriod",
        "endpoint": LOCAL_RESULTS_URL,
        "target": "race_records",
        "write_mode": "overwrite" if overwrite else "insert_or_ignore",
        "overwrite": bool(overwrite),
        "horses_write_mode": (
            "overwrite" if overwrite else "insert_or_fill_blanks"
        ),
        "horses_filled": horses_filled,
        "variables": {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "venueCodes": list(venues),
        },
        "meetings_scanned": len(meeting_metas),
        "meetings_with_results": meetings_with_results,
        "meeting_metas": meeting_metas,
        "ingest_status": "success",
        "processed": prog.processed,
        "updated": prog.updated,
        "skipped": prog.skipped,
        "failed": prog.failed,
        "total": prog.total,
    }
    code = "success" if prog.failed == 0 else "partial_failure"
    return RacingDownloadResult(code=code, progress=prog, detail=detail)


def run_results_period_download_locked(
    db_path: Path, **kwargs: Any
) -> RacingDownloadResult:
    with _RACING_JOB_LOCK:
        return run_results_period_download(db_path, **kwargs)


def run_horses_download(
    db_path: Path,
    *,
    progress_cb: ProgressCb | None = None,
    overwrite: bool = False,
) -> RacingDownloadResult:
    """Fetch HKJC horse rating list; insert new or fill blank/placeholder names."""
    ensure_racecard_db(db_path)
    prog = RacingDownloadProgress(phase="fetching_total", message="正在取得總數")
    _emit(progress_cb, prog)
    started = utc_now_iso()

    try:
        basics = fetch_all_horses_basic_info()
    except Exception as e:
        raise RacingDownloadError("horses_fetch_failed", str(e)) from e

    rows = [
        {
            "horse_id": h.horse_id,
            "brand_no": h.brand_no,
            "name_zh": h.name_zh,
            "name_en": h.name_en,
            "status": h.status,
        }
        for h in basics
        if is_acceptable_horse_id(h.horse_id)
    ]
    if not rows:
        raise RacingDownloadError("empty_horses", "horse rating list empty")

    prog.total = len(rows)
    prog.phase = "processing"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    inserted = 0
    filled = 0
    for row in rows:
        try:
            action = insert_or_fill_horse(db_path, row, overwrite=overwrite)
            if action == "inserted":
                inserted += 1
                prog.updated += 1
            elif action == "filled":
                filled += 1
                prog.updated += 1
            else:
                prog.skipped += 1
        except Exception:
            logger.exception("horses insert/fill failed horse=%s", row.get("horse_id"))
            prog.failed += 1
        prog.processed += 1
        prog.message = f"已處理 {prog.processed}/{prog.total} record"
        _emit(progress_cb, prog)

    prog.phase = "done"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)
    detail = {
        "started_at": started,
        "finished_at": utc_now_iso(),
        "operation": HORSES_OPERATION,
        "query": HORSES_QUERY,
        "endpoint": HKJC_MCS_HORSES_CLAS_URL_EN,
        "target": "horses",
        "write_mode": "overwrite" if overwrite else "insert_or_fill_blanks",
        "overwrite": bool(overwrite),
        "variables": {},
        "ingest_status": "success",
        "processed": prog.processed,
        "updated": prog.updated,
        "inserted": inserted,
        "filled": filled,
        "skipped": prog.skipped,
        "failed": prog.failed,
        "total": prog.total,
    }
    code = "success" if prog.failed == 0 else "partial_failure"
    return RacingDownloadResult(code=code, progress=prog, detail=detail)


def run_horses_download_locked(
    db_path: Path, **kwargs: Any
) -> RacingDownloadResult:
    with _RACING_JOB_LOCK:
        return run_horses_download(db_path, **kwargs)


def run_standard_times_download(
    db_path: Path,
    *,
    years: Any = None,
    progress_cb: ProgressCb | None = None,
    overwrite: bool = False,
) -> RacingDownloadResult:
    """Fetch registered HKJC standard-times PDFs and upsert racing_standard_times."""
    from hppv.racing_standard_times import (
        STANDARD_TIMES_OPERATION,
        STANDARD_TIMES_QUERY,
        StandardTimesError,
        ensure_standard_times_table,
        fetch_standard_times_pdf,
        parse_standard_times_pdf_bytes,
        pdf_url_for_year,
        resolve_download_years,
        upsert_standard_times_rows,
    )

    ensure_racecard_db(db_path)
    ensure_standard_times_table(db_path)
    prog = RacingDownloadProgress(phase="fetching_total", message="正在取得總數")
    _emit(progress_cb, prog)
    started = utc_now_iso()

    try:
        year_list = resolve_download_years(years)
    except StandardTimesError as e:
        raise RacingDownloadError(e.code, e.detail) from e

    prog.total = len(year_list)
    prog.phase = "processing"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    inserted = 0
    updated = 0
    skipped = 0
    year_details: list[dict[str, Any]] = []
    for year in year_list:
        try:
            pdf_bytes = fetch_standard_times_pdf(year)
            published_on, rows = parse_standard_times_pdf_bytes(
                pdf_bytes, source_year=year
            )
            counts = upsert_standard_times_rows(
                db_path, rows, overwrite=bool(overwrite)
            )
            inserted += int(counts.get("inserted") or 0)
            updated += int(counts.get("updated") or 0)
            skipped += int(counts.get("skipped") or 0)
            prog.updated += int(counts.get("inserted") or 0) + int(
                counts.get("updated") or 0
            )
            prog.skipped += int(counts.get("skipped") or 0)
            year_details.append(
                {
                    "year": year,
                    "published_on": published_on,
                    "url": pdf_url_for_year(year),
                    "rows": len(rows),
                    "inserted": counts.get("inserted"),
                    "updated": counts.get("updated"),
                    "skipped": counts.get("skipped"),
                }
            )
        except StandardTimesError as e:
            logger.exception("standard times year failed year=%s code=%s", year, e.code)
            prog.failed += 1
            year_details.append(
                {"year": year, "error": e.code, "detail": e.detail}
            )
        except Exception as e:
            logger.exception("standard times year failed year=%s", year)
            prog.failed += 1
            year_details.append({"year": year, "error": "year_failed", "detail": str(e)})
        prog.processed += 1
        prog.message = f"已處理 {prog.processed}/{prog.total} record"
        _emit(progress_cb, prog)

    prog.phase = "done"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)
    detail = {
        "started_at": started,
        "finished_at": utc_now_iso(),
        "operation": STANDARD_TIMES_OPERATION,
        "query": STANDARD_TIMES_QUERY,
        "target": "racing_standard_times",
        "write_mode": "overwrite" if overwrite else "insert_or_ignore",
        "overwrite": bool(overwrite),
        "variables": {"years": year_list},
        "years": year_details,
        "ingest_status": "success" if prog.failed == 0 else "failed",
        "processed": prog.processed,
        "updated": prog.updated,
        "inserted": inserted,
        "row_updated": updated,
        "skipped": skipped,
        "failed": prog.failed,
        "total": prog.total,
    }
    if prog.failed and (inserted or updated):
        code = "partial_failure"
        detail["ingest_status"] = "success"
    elif prog.failed:
        code = "failed"
    else:
        code = "success"
    return RacingDownloadResult(code=code, progress=prog, detail=detail)


def run_standard_times_download_locked(
    db_path: Path, **kwargs: Any
) -> RacingDownloadResult:
    with _RACING_JOB_LOCK:
        return run_standard_times_download(db_path, **kwargs)


def run_draw_statistics_download(
    db_path: Path,
    *,
    progress_cb: ProgressCb | None = None,
    overwrite: bool = False,
) -> RacingDownloadResult:
    """Fetch HKJC drawadvsearch matrix; upsert racing_draw_statistics by unique key."""
    from hppv.racing_draw_statistics import (
        DRAW_STATISTICS_OPERATION,
        DRAW_STATISTICS_QUERY,
        DrawStatisticsError,
        download_all_draw_statistics,
        ensure_draw_statistics_table,
        upsert_draw_statistics_rows,
    )

    ensure_racecard_db(db_path)
    ensure_draw_statistics_table(db_path)
    prog = RacingDownloadProgress(phase="fetching_total", message="正在取得總數")
    _emit(progress_cb, prog)
    started = utc_now_iso()

    def _progress_bridge(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("total") is not None:
            try:
                prog.total = int(payload["total"])
            except (TypeError, ValueError):
                pass
        if payload.get("processed") is not None:
            try:
                prog.processed = int(payload["processed"])
            except (TypeError, ValueError):
                pass
        prog.phase = str(payload.get("phase") or prog.phase)
        prog.message = str(payload.get("message") or prog.message)
        _emit(progress_cb, prog)

    try:
        rows, fetch_detail = download_all_draw_statistics(
            progress_cb=_progress_bridge
        )
    except DrawStatisticsError as e:
        raise RacingDownloadError(e.code, e.detail) from e

    prog.total = len(rows)
    prog.processed = 0
    prog.phase = "processing"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    try:
        counts = upsert_draw_statistics_rows(
            db_path, rows, overwrite=bool(overwrite)
        )
    except Exception as e:
        raise RacingDownloadError("draw_upsert_failed", str(e)) from e

    prog.processed = len(rows)
    prog.updated = int(counts.get("inserted") or 0) + int(counts.get("updated") or 0)
    prog.skipped = int(counts.get("skipped") or 0) + int(counts.get("filled") or 0)
    prog.phase = "done"
    prog.message = f"已處理 {prog.processed}/{prog.total} record"
    _emit(progress_cb, prog)

    detail = {
        "started_at": started,
        "finished_at": utc_now_iso(),
        "operation": DRAW_STATISTICS_OPERATION,
        "query": DRAW_STATISTICS_QUERY,
        "target": "racing_draw_statistics",
        "write_mode": "overwrite" if overwrite else "insert_or_fill_meta",
        "overwrite": bool(overwrite),
        "variables": {"going": "ALL"},
        "fetch": fetch_detail,
        "ingest_status": "success",
        "processed": prog.processed,
        "updated": prog.updated,
        "inserted": counts.get("inserted"),
        "row_updated": counts.get("updated"),
        "filled": counts.get("filled"),
        "skipped": counts.get("skipped"),
        "failed": prog.failed,
        "total": prog.total,
    }
    return RacingDownloadResult(code="success", progress=prog, detail=detail)


def run_draw_statistics_download_locked(
    db_path: Path, **kwargs: Any
) -> RacingDownloadResult:
    with _RACING_JOB_LOCK:
        return run_draw_statistics_download(db_path, **kwargs)
