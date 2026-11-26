"""Read-only racing daily list from existing racecard_starters (+ race_info join)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hppv.db import get_connection
from hppv.display_format import EMPTY_DISPLAY, format_date_display, format_field_display
from hppv.racing_history import (
    DEFAULT_HISTORY_SEARCH_OP,
    HISTORY_SEARCH_OP_APPROX,
    HISTORY_SEARCH_OP_CONTAINS,
    HISTORY_SEARCH_OP_ENDS,
    HISTORY_SEARCH_OP_EQ,
    HISTORY_SEARCH_OP_NE,
    HISTORY_SEARCH_OP_NOT_CONTAINS,
    HISTORY_SEARCH_OP_STARTS,
    normalize_history_search_op,
)
from hppv.racing_layout import (
    DEFAULT_RACING_DAILY_SEARCH_KEYS,
    RACING_DAILY_COLUMN_BY_KEY,
    RACING_DAILY_TEXT_SEARCH_WHITELIST,
    SORT_ASC,
    SORT_DESC,
    racing_daily_search_keys_from_layout,
    racing_daily_sort_specs_from_layout,
    racing_sort_param,
)

RACING_DAILY_PAGE_SIZE = 50

_RACECOURSE_DISPLAY = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}

# Display key → SQL expression (starters s LEFT JOIN race_info ri).
_DAILY_COL_SQL: dict[str, str] = {
    "id": "s.id",
    "race_date": "s.race_date",
    "racecourse": "s.racecourse",
    "racecourse_name": (
        "CASE upper(s.racecourse) "
        "WHEN 'ST' THEN '沙田' WHEN 'HV' THEN '跑馬地' WHEN 'FC' THEN '從化' "
        "ELSE ifnull(s.racecourse,'') END"
    ),
    "race_no": "s.race_no",
    "reserve": 's."後備"',
    "horse_id": "s.horse_id",
    "horse_no": 's."馬號"',
    "horse_name": 's."馬名"',
    "finish_pos": 's."名次"',
    "draw": 's."檔位"',
    "jockey": 's."騎師"',
    "trainer": 's."練馬師"',
    "handicap": 's."負磅"',
    "rating": 's."評分"',
    "horse_weight": 's."馬匹體重"',
    "gear": 's."配備"',
    "predicted_odds": 's."預測賠率"',
    "immediate_odds": 's."即時賠率"',
    "odds_diff": 's."賠率差異"',
    "weighted_win_rate": 's."加權總勝出率"',
    "same_distance_win_rate": 's."同程勝率"',
    "draw_win_rate": 's."檔位勝出率"',
    "draw_place_rate": 's."檔位上名率"',
    "event_name": "ri.event_name",
    "calendar_date": "ri.calendar_date",
    "weekday": "ri.weekday",
    "meeting_venue": "ri.meeting_venue",
    "post_time": "ri.post_time",
    "race_course_area": "ri.race_course_area",
    "track_name": "ri.track_name",
    "distance": "ri.distance",
    "prize_money": "ri.prize_money",
    "rating_range": "ri.rating_range",
    "race_class": "ri.race_class",
}

_DAILY_SORT_SQL: dict[str, str] = {
    "id": "s.id",
    "race_date": "s.race_date",
    "racecourse": "s.racecourse",
    "racecourse_name": _DAILY_COL_SQL["racecourse_name"],
    "race_no": "CAST(s.race_no AS INTEGER)",
    "reserve": 's."後備"',
    "horse_id": "s.horse_id",
    "horse_no": 'CAST(s."馬號" AS INTEGER)',
    "horse_name": 's."馬名"',
    "finish_pos": 'CAST(s."名次" AS INTEGER)',
    "draw": 'CAST(s."檔位" AS INTEGER)',
    "jockey": 's."騎師"',
    "trainer": 's."練馬師"',
    "handicap": 'CAST(s."負磅" AS INTEGER)',
    "rating": 'CAST(s."評分" AS INTEGER)',
    "horse_weight": 's."馬匹體重"',
    "gear": 's."配備"',
    "predicted_odds": 'CAST(s."預測賠率" AS REAL)',
    "immediate_odds": 'CAST(s."即時賠率" AS REAL)',
    "odds_diff": 'CAST(s."賠率差異" AS REAL)',
    "weighted_win_rate": 'CAST(s."加權總勝出率" AS REAL)',
    "same_distance_win_rate": 'CAST(s."同程勝率" AS REAL)',
    "draw_win_rate": 'CAST(s."檔位勝出率" AS REAL)',
    "draw_place_rate": 'CAST(s."檔位上名率" AS REAL)',
    "event_name": "ri.event_name",
    "calendar_date": "ri.calendar_date",
    "weekday": "ri.weekday",
    "meeting_venue": "ri.meeting_venue",
    "post_time": "ri.post_time",
    "race_course_area": "ri.race_course_area",
    "track_name": "ri.track_name",
    "distance": "ri.distance",
    "prize_money": "ri.prize_money",
    "rating_range": "ri.rating_range",
    "race_class": "ri.race_class",
}

_FROM_SQL = """
FROM racecard_starters AS s
LEFT JOIN racecard_race_info AS ri
  ON ri.race_date = s.race_date
 AND ri.racecourse = s.racecourse
 AND ri.race_no = s.race_no
"""


class RacingDailyError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class RacingDailySearchError(RacingDailyError):
    pass


@dataclass(frozen=True, slots=True)
class RacingDailyListResult:
    rows: list[dict[str, str]]
    n: int
    total_count: int
    total_pages: int
    page_size: int
    q: str
    all_matches: bool
    sort: str
    q_op: str = DEFAULT_HISTORY_SEARCH_OP
    field_keys: tuple[str, ...] = ()
    search_keys: tuple[str, ...] = ()
    race_date: str | None = None
    racecourse: str | None = None
    race_no: int | None = None


@dataclass(frozen=True, slots=True)
class RacingDailyMeeting:
    race_date: str
    racecourse: str
    race_date_iso: str
    racecourse_name: str
    source: str  # "meta" | "latest_fallback"


@dataclass(frozen=True, slots=True)
class RacingDailyRaceCard:
    race_no: int
    horse_count: int


@dataclass(frozen=True, slots=True)
class RacingDailyRaceView:
    meeting: RacingDailyMeeting
    races: list[RacingDailyRaceCard]
    selected_race_no: int
    summary: dict[str, str]
    rows: list[dict[str, str]]
    total_count: int
    sort: str
    field_keys: tuple[str, ...] = ()


def iso_to_racecard_date(raw: str | None) -> str | None:
    """Convert HTML date (YYYY-MM-DD) or racecard date (YYYY/MM/DD) to DB form."""
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "/")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", text):
        return text
    return None


def racecard_date_to_iso(raw: str | None) -> str:
    text = str(raw or "").strip()
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", text):
        return text.replace("/", "-")
    return text


def parse_racing_daily_sort_param(raw: str | None) -> list[tuple[str, str]] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    specs: list[tuple[str, str]] = []
    for part in text.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, direction = part.split(":", 1)
        key = key.strip()
        direction = direction.strip().lower()
        if key not in _DAILY_SORT_SQL:
            continue
        if direction not in ("asc", "desc"):
            continue
        specs.append((key, direction))
    return specs or None


def _normalize_page(n: int | None) -> int:
    try:
        page = int(n) if n is not None else 1
    except (TypeError, ValueError):
        page = 1
    return max(1, page)


def _build_q_clause(q: str, q_op: str, search_keys: list[str]) -> tuple[str, list[Any]]:
    q = (q or "").strip()
    if not q:
        return "", []
    keys = [
        k
        for k in search_keys
        if k in RACING_DAILY_TEXT_SEARCH_WHITELIST and k in _DAILY_COL_SQL
    ]
    if not keys:
        raise RacingDailySearchError("no_searchable_fields")

    exprs = [_DAILY_COL_SQL[k] for k in keys]
    op = normalize_history_search_op(q_op)
    params: list[Any] = []

    def lower_expr(e: str) -> str:
        return f"lower(CAST({e} AS TEXT))"

    if op == HISTORY_SEARCH_OP_APPROX:
        parts = [f"instr({lower_expr(e)}, ?) > 0" for e in exprs]
        params.extend([q.lower()] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_EQ:
        parts = [f"CAST({e} AS TEXT) = ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NE:
        parts = [f"ifnull(CAST({e} AS TEXT), '') != ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_CONTAINS:
        parts = [f"instr(CAST({e} AS TEXT), ?) > 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NOT_CONTAINS:
        parts = [f"instr(ifnull(CAST({e} AS TEXT), ''), ?) = 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_STARTS:
        parts = [f"CAST({e} AS TEXT) LIKE ?" for e in exprs]
        params.extend([q + "%"] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_ENDS:
        parts = [f"CAST({e} AS TEXT) LIKE ?" for e in exprs]
        params.extend(["%" + q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    return "", []


def _format_cell(key: str, value: Any) -> str:
    return format_field_display(key, value)


def _row_to_view(row: Any) -> dict[str, str]:
    keys = list(row.keys()) if hasattr(row, "keys") else []
    out: dict[str, str] = {}
    for key in _DAILY_COL_SQL:
        out[key] = _format_cell(key, row[key] if key in keys else None)
    return out


def list_available_race_dates(db_path: Path) -> list[dict[str, str]]:
    """Distinct meetings for date picker (iso + display)."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT race_date, racecourse, COUNT(*) AS c
            FROM racecard_starters
            GROUP BY race_date, racecourse
            ORDER BY race_date DESC, racecourse ASC
            """
        ).fetchall()
    out: list[dict[str, str]] = []
    for r in rows:
        rd = str(r["race_date"] or "")
        code = str(r["racecourse"] or "")
        out.append(
            {
                "race_date": rd,
                "race_date_iso": racecard_date_to_iso(rd),
                "racecourse": code,
                "racecourse_name": _RACECOURSE_DISPLAY.get(code.upper(), code or "—"),
                "count": str(r["c"]),
            }
        )
    return out


def _meeting_exists(conn: Any, race_date: str, racecourse: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 AS ok
        FROM racecard_starters
        WHERE race_date = ? AND racecourse = ?
        LIMIT 1
        """,
        (race_date, racecourse),
    ).fetchone()
    return bool(row)


def resolve_active_meeting(db_path: Path) -> RacingDailyMeeting | None:
    """
    Active meeting batch for daily UI.

    Existing project rule (dashboard / download meta):
      racecard_meta.last_source_race_date + last_source_racecourse_code
    Fallback (desktop RacecardRankViewer / daily when meta missing):
      latest (race_date DESC, racecourse ASC) that has starters.
    """
    with get_connection(db_path) as conn:
        meta_rows = conn.execute(
            """
            SELECT key, value FROM racecard_meta
            WHERE key IN ('last_source_race_date', 'last_source_racecourse_code')
            """
        ).fetchall()
        meta = {str(r["key"]): str(r["value"] or "").strip() for r in meta_rows}
        meta_date = meta.get("last_source_race_date") or ""
        meta_course = meta.get("last_source_racecourse_code") or ""
        if meta_date and meta_course and _meeting_exists(conn, meta_date, meta_course):
            return RacingDailyMeeting(
                race_date=meta_date,
                racecourse=meta_course,
                race_date_iso=racecard_date_to_iso(meta_date),
                racecourse_name=_RACECOURSE_DISPLAY.get(
                    meta_course.upper(), meta_course or "—"
                ),
                source="meta",
            )
        row = conn.execute(
            """
            SELECT race_date, racecourse
            FROM racecard_starters
            GROUP BY race_date, racecourse
            ORDER BY race_date DESC, racecourse ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        rd = str(row["race_date"] or "")
        code = str(row["racecourse"] or "")
        return RacingDailyMeeting(
            race_date=rd,
            racecourse=code,
            race_date_iso=racecard_date_to_iso(rd),
            racecourse_name=_RACECOURSE_DISPLAY.get(code.upper(), code or "—"),
            source="latest_fallback",
        )


def list_meeting_races(
    db_path: Path, *, race_date: str, racecourse: str
) -> list[RacingDailyRaceCard]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT race_no, COUNT(*) AS c
            FROM racecard_starters
            WHERE race_date = ? AND racecourse = ?
            GROUP BY race_no
            ORDER BY CAST(race_no AS INTEGER) ASC, race_no ASC
            """,
            (race_date, racecourse),
        ).fetchall()
    out: list[RacingDailyRaceCard] = []
    for r in rows:
        try:
            rn = int(r["race_no"])
        except (TypeError, ValueError):
            continue
        out.append(RacingDailyRaceCard(race_no=rn, horse_count=int(r["c"] or 0)))
    return out


def _build_race_summary(row: Any | None, meeting: RacingDailyMeeting, race_no: int) -> dict[str, str]:
    """Compact race header fields from race_info (+ meeting). Going not in schema → —."""
    keys = (
        "event_name",
        "calendar_date",
        "weekday",
        "meeting_venue",
        "post_time",
        "race_course_area",
        "track_name",
        "distance",
        "race_class",
        "rating_range",
        "prize_money",
    )
    out: dict[str, str] = {
        "race_no": str(race_no),
        "race_date": format_date_display(meeting.race_date),
        "race_date_iso": meeting.race_date_iso,
        "racecourse": meeting.racecourse,
        "racecourse_name": meeting.racecourse_name,
        "going": EMPTY_DISPLAY,  # not stored in racecard_race_info
    }
    if row is None:
        for k in keys:
            out[k] = EMPTY_DISPLAY
        return out
    row_keys = set(row.keys()) if hasattr(row, "keys") else set()
    for k in keys:
        out[k] = _format_cell(k, row[k] if k in row_keys else None)
    track = out.get("track_name") or ""
    if track and track != EMPTY_DISPLAY and "賽道" not in track:
        out["track_name"] = f"{track} 賽道"
    return out


def fetch_racing_daily_race(
    db_path: Path,
    *,
    race_no: int | None = None,
    race_date: str | None = None,
    racecourse: str | None = None,
    sort_specs: list[tuple[str, str]] | None = None,
) -> RacingDailyRaceView:
    """One meeting + one race: all starters (no search/pager)."""
    # Ensure starters columns exist before SELECT (new derived cols via ALTER).
    from hppv.racing_download import ensure_racecard_db

    ensure_racecard_db(db_path)
    meeting: RacingDailyMeeting | None = None
    db_date = iso_to_racecard_date(race_date) if race_date else None
    course = str(racecourse or "").strip().upper() or None
    if db_date and course:
        with get_connection(db_path) as conn:
            if _meeting_exists(conn, db_date, course):
                meeting = RacingDailyMeeting(
                    race_date=db_date,
                    racecourse=course,
                    race_date_iso=racecard_date_to_iso(db_date),
                    racecourse_name=_RACECOURSE_DISPLAY.get(course, course),
                    source="request",
                )
    if meeting is None:
        meeting = resolve_active_meeting(db_path)
    if meeting is None:
        raise RacingDailyError("no_meeting")

    races = list_meeting_races(
        db_path, race_date=meeting.race_date, racecourse=meeting.racecourse
    )
    if not races:
        raise RacingDailyError("no_races")

    race_nos = [r.race_no for r in races]
    selected = race_no if race_no in race_nos else race_nos[0]

    specs = sort_specs or [("horse_no", SORT_ASC), ("id", SORT_ASC)]
    order_parts: list[str] = []
    for key, direction in specs:
        expr = _DAILY_SORT_SQL.get(key)
        meta = RACING_DAILY_COLUMN_BY_KEY.get(key)
        if not expr or not meta or not meta.get("sortable"):
            continue
        order_parts.append(f"{expr} {'ASC' if direction == SORT_ASC else 'DESC'}")
    if not order_parts:
        order_parts = ['CAST(s."馬號" AS INTEGER) ASC', "s.id ASC"]
    order_sql = " ORDER BY " + ", ".join(order_parts)
    select_cols = ", ".join(f"{expr} AS {key}" for key, expr in _DAILY_COL_SQL.items())
    where_sql = " WHERE s.race_date = ? AND s.racecourse = ? AND s.race_no = ?"
    params: list[Any] = [meeting.race_date, meeting.racecourse, selected]

    with get_connection(db_path) as conn:
        info = conn.execute(
            """
            SELECT event_name, calendar_date, weekday, meeting_venue, post_time,
                   race_course_area, track_name, distance, race_class,
                   rating_range, prize_money
            FROM racecard_race_info
            WHERE race_date = ? AND racecourse = ? AND race_no = ?
            LIMIT 1
            """,
            (meeting.race_date, meeting.racecourse, selected),
        ).fetchone()
        rows_raw = conn.execute(
            f"SELECT {select_cols} {_FROM_SQL}{where_sql}{order_sql}",
            params,
        ).fetchall()

    rows = [_row_to_view(r) for r in rows_raw]
    return RacingDailyRaceView(
        meeting=meeting,
        races=races,
        selected_race_no=selected,
        summary=_build_race_summary(info, meeting, selected),
        rows=rows,
        total_count=len(rows),
        sort=racing_sort_param(specs),
        field_keys=tuple(_DAILY_COL_SQL.keys()),
    )


def fetch_racing_daily_filtered(
    db_path: Path,
    *,
    all_matches: bool = True,
    race_date: str | None = None,
    racecourse: str | None = None,
    race_no: int | None = None,
    n: int | None = 1,
    q: str = "",
    q_op: str = DEFAULT_HISTORY_SEARCH_OP,
    sort_specs: list[tuple[str, str]] | None = None,
    search_keys: list[str] | None = None,
    page_size: int = RACING_DAILY_PAGE_SIZE,
) -> RacingDailyListResult:
    page = _normalize_page(n)
    size = max(1, int(page_size or RACING_DAILY_PAGE_SIZE))
    keys = search_keys or list(DEFAULT_RACING_DAILY_SEARCH_KEYS)
    specs = sort_specs or [("race_date", SORT_DESC), ("race_no", SORT_ASC), ("horse_no", SORT_ASC)]
    db_date = iso_to_racecard_date(race_date) if race_date else None
    course = str(racecourse or "").strip().upper() or None
    rn: int | None = None
    if race_no is not None:
        try:
            rn = int(race_no)
        except (TypeError, ValueError):
            raise RacingDailyError("invalid_race_no") from None

    where: list[str] = []
    params: list[Any] = []
    if not all_matches or db_date or course or rn is not None:
        if db_date:
            where.append("s.race_date = ?")
            params.append(db_date)
        elif not all_matches:
            raise RacingDailyError("invalid_date")
        if course:
            where.append("s.racecourse = ?")
            params.append(course)
        if rn is not None:
            where.append("s.race_no = ?")
            params.append(rn)

    q_clause, q_params = _build_q_clause(q, q_op, keys)
    if q_clause:
        where.append(q_clause)
        params.extend(q_params)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    order_parts: list[str] = []
    for key, direction in specs:
        expr = _DAILY_SORT_SQL.get(key)
        meta = RACING_DAILY_COLUMN_BY_KEY.get(key)
        if not expr or not meta or not meta.get("sortable"):
            continue
        order_parts.append(f"{expr} {'ASC' if direction == SORT_ASC else 'DESC'}")
    if not order_parts:
        order_parts = [
            "s.race_date DESC",
            "CAST(s.race_no AS INTEGER) ASC",
            'CAST(s."馬號" AS INTEGER) ASC',
        ]
    order_sql = " ORDER BY " + ", ".join(order_parts)

    select_cols = ", ".join(f"{expr} AS {key}" for key, expr in _DAILY_COL_SQL.items())
    with get_connection(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c {_FROM_SQL}{where_sql}",
            params,
        ).fetchone()["c"]
        total_pages = max(1, int(math.ceil(total / size))) if total else 0
        if total_pages and page > total_pages:
            page = total_pages
        offset = (page - 1) * size if total else 0
        rows_raw = conn.execute(
            f"SELECT {select_cols} {_FROM_SQL}{where_sql}{order_sql} LIMIT ? OFFSET ?",
            [*params, size, offset],
        ).fetchall()

    rows = [_row_to_view(r) for r in rows_raw]
    return RacingDailyListResult(
        rows=rows,
        n=page if total else 0,
        total_count=int(total),
        total_pages=int(total_pages),
        page_size=size,
        q=(q or "").strip(),
        all_matches=bool(all_matches) and db_date is None and course is None and rn is None,
        sort=racing_sort_param(specs),
        q_op=normalize_history_search_op(q_op),
        field_keys=tuple(_DAILY_COL_SQL.keys()),
        search_keys=tuple(keys),
        race_date=db_date,
        racecourse=course,
        race_no=rn,
    )
