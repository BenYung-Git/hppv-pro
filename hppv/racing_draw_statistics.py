"""HKJC draw statistics (檔位統計) → racing_draw_statistics."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from hppv.db import get_connection, init_db
from hppv.display_format import format_field_display

DRAW_STATISTICS_QUERY = "drawStatisticsAdvSearch"
DRAW_STATISTICS_OPERATION = "drawStatisticsAdvSearch"

DRAW_ADVSEARCH_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/drawadvsearch"
)

# Local HK venues only (ST / HV). Conghua (CHT) excluded — not in product venue_code set.
RACECOURSE_MAP: dict[str, tuple[str, str]] = {
    "STT": ("ST", "TURF"),
    "STA": ("ST", "AWT"),
    "HVT": ("HV", "TURF"),
}

_HTTP_HEADERS = {
    "User-Agent": "HPPV/0.1 (+requests; draw statistics download)",
    "Accept-Language": "zh-HK",
}

DRAW_STATISTICS_PAGE_SIZE = 50

HISTORY_SEARCH_OP_APPROX = "approx"
HISTORY_SEARCH_OP_EQ = "eq"
HISTORY_SEARCH_OP_NE = "ne"
HISTORY_SEARCH_OP_CONTAINS = "contains"
HISTORY_SEARCH_OP_NOT_CONTAINS = "not_contains"
HISTORY_SEARCH_OP_STARTS = "starts"
HISTORY_SEARCH_OP_ENDS = "ends"
DEFAULT_HISTORY_SEARCH_OP = HISTORY_SEARCH_OP_APPROX
HISTORY_SEARCH_OPS_ALL = frozenset(
    {
        HISTORY_SEARCH_OP_APPROX,
        HISTORY_SEARCH_OP_EQ,
        HISTORY_SEARCH_OP_NE,
        HISTORY_SEARCH_OP_CONTAINS,
        HISTORY_SEARCH_OP_NOT_CONTAINS,
        HISTORY_SEARCH_OP_STARTS,
        HISTORY_SEARCH_OP_ENDS,
    }
)

_COL_SQL: dict[str, str] = {
    "id": "ds.id",
    "venue_code": "ds.venue_code",
    "track_type": "ds.track_type",
    "rail_position": "ds.rail_position",
    "distance_m": "ds.distance_m",
    "draw_no": "ds.draw_no",
    "starts": "ds.starts",
    "wins": "ds.wins",
    "seconds": "ds.seconds",
    "thirds": "ds.thirds",
    "fourths": "ds.fourths",
    "win_pct": "ds.win_pct",
    "quinella_pct": "ds.quinella_pct",
    "place_pct": "ds.place_pct",
    "top4_pct": "ds.top4_pct",
    "win_count": "ds.win_count",
    "in_place_count": "ds.in_place_count",
    "lose_count": "ds.lose_count",
    "source": "ds.source",
    "source_url": "ds.source_url",
    "fetched_at": "ds.fetched_at",
    "updated_at": "ds.updated_at",
}

_SORT_SQL: dict[str, str] = dict(_COL_SQL)


class DrawStatisticsError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class DrawStatisticsSearchError(DrawStatisticsError):
    pass


@dataclass(frozen=True, slots=True)
class DrawStatisticsRow:
    venue_code: str
    track_type: str
    rail_position: str
    distance_m: int
    draw_no: int
    starts: int
    wins: int
    seconds: int
    thirds: int
    fourths: int
    win_pct: str
    quinella_pct: str
    place_pct: str
    top4_pct: str
    source: str
    source_url: str
    fetched_at: str

    @property
    def win_count(self) -> int:
        return self.wins

    @property
    def in_place_count(self) -> int:
        return self.seconds + self.thirds + self.fourths

    @property
    def lose_count(self) -> int:
        return self.starts - self.wins - self.seconds - self.thirds - self.fourths


@dataclass(frozen=True, slots=True)
class DrawStatisticsListResult:
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
    filter_options: dict[str, list[str]] | None = None
    venue_code: str | None = None
    rail_position: str | None = None
    distance_m: str | None = None
    draw_no: str | None = None


def draw_statistics_schema_sql() -> str:
    return """
        CREATE TABLE IF NOT EXISTS racing_draw_statistics (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          venue_code TEXT NOT NULL,
          track_type TEXT NOT NULL,
          rail_position TEXT NOT NULL DEFAULT '',
          distance_m INTEGER NOT NULL,
          draw_no INTEGER NOT NULL,
          starts INTEGER NOT NULL,
          wins INTEGER NOT NULL,
          seconds INTEGER NOT NULL,
          thirds INTEGER NOT NULL,
          fourths INTEGER NOT NULL,
          win_pct TEXT NOT NULL DEFAULT '',
          quinella_pct TEXT NOT NULL DEFAULT '',
          place_pct TEXT NOT NULL DEFAULT '',
          top4_pct TEXT NOT NULL DEFAULT '',
          win_count INTEGER NOT NULL,
          in_place_count INTEGER NOT NULL,
          lose_count INTEGER NOT NULL,
          source TEXT NOT NULL DEFAULT '',
          source_url TEXT NOT NULL DEFAULT '',
          fetched_at TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          UNIQUE(venue_code, track_type, rail_position, distance_m, draw_no)
        );
        CREATE INDEX IF NOT EXISTS idx_racing_draw_stats_venue_rail
          ON racing_draw_statistics(venue_code, rail_position, distance_m);
    """


def ensure_draw_statistics_table(db_path: Path) -> None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.executescript(draw_statistics_schema_sql())
        conn.commit()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_int(raw: str, *, field: str) -> int:
    text = str(raw or "").strip().replace(",", "")
    if not re.fullmatch(r"-?\d+", text):
        raise DrawStatisticsError("invalid_int", f"{field}={raw!r}")
    return int(text)


def _parse_pct(raw: str, *, field: str) -> str:
    text = str(raw or "").strip().replace("%", "")
    if text == "":
        raise DrawStatisticsError("missing_pct", f"{field} empty")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise DrawStatisticsError("invalid_pct", f"{field}={raw!r}")
    return text


# race_course_area (racecard) → racing_draw_statistics.track_type
_RACE_COURSE_AREA_TO_TRACK_TYPE: dict[str, str] = {
    "草地": "TURF",
    "全天候": "AWT",
    "TURF": "TURF",
    "AWT": "AWT",
}


def track_type_from_race_course_area(area: Any) -> str | None:
    """Map racecard race_course_area to draw-stats track_type. Unknown → None."""
    text = str(area or "").strip()
    if not text:
        return None
    return _RACE_COURSE_AREA_TO_TRACK_TYPE.get(text)


def rail_position_for_draw_lookup(*, track_type: str, track_name: Any) -> str | None:
    """AWT stats use empty rail; TURF uses racecard track_name (A/B/C+3…)."""
    tt = str(track_type or "").strip().upper()
    if tt == "AWT":
        return ""
    if tt != "TURF":
        return None
    text = str(track_name or "").strip()
    if not text:
        return None
    return text


def lookup_draw_win_place_pct(
    conn: Any,
    *,
    venue_code: str,
    track_type: str,
    rail_position: str,
    distance_m: int,
    draw_no: int,
) -> tuple[str | None, str | None]:
    """Read win_pct/place_pct by full unique key. 0 kept; missing → (None, None).

    Multiple rows for the key → DrawStatisticsError (do not pick/merge).
    """
    rows = conn.execute(
        """
        SELECT win_pct, place_pct
          FROM racing_draw_statistics
         WHERE venue_code = ?
           AND track_type = ?
           AND rail_position = ?
           AND distance_m = ?
           AND draw_no = ?
        """,
        (
            str(venue_code).strip().upper(),
            str(track_type).strip().upper(),
            str(rail_position),
            int(distance_m),
            int(draw_no),
        ),
    ).fetchall()
    if not rows:
        return None, None
    if len(rows) > 1:
        raise DrawStatisticsError(
            "draw_lookup_ambiguous",
            (
                f"multiple draw stats for "
                f"{venue_code}/{track_type}/{rail_position!r}/{distance_m}/{draw_no}"
            ),
        )
    win_pct = rows[0]["win_pct"]
    place_pct = rows[0]["place_pct"]
    # Preserve stored "0"; only treat SQL NULL as missing.
    win_out = None if win_pct is None else str(win_pct)
    place_out = None if place_pct is None else str(place_pct)
    return win_out, place_out


def update_draw_rates_for_meeting(
    conn: Any,
    *,
    race_date: str,
    racecourse: str,
) -> int:
    """Write 檔位勝出率／檔位上名率 for starters of one meeting (lookup only)."""
    from hppv.racing_horse_form import normalize_distance
    from hppv.schema import (
        RACECARD_DRAW_PLACE_RATE_COLUMN,
        RACECARD_DRAW_WIN_RATE_COLUMN,
        qident,
    )

    venue_code = str(racecourse or "").strip().upper()
    if not venue_code:
        return 0
    # Table may be absent before first draw-stats download.
    exists = conn.execute(
        """
        SELECT 1 FROM sqlite_master
         WHERE type = 'table' AND name = 'racing_draw_statistics'
         LIMIT 1
        """
    ).fetchone()
    win_col = qident(RACECARD_DRAW_WIN_RATE_COLUMN)
    place_col = qident(RACECARD_DRAW_PLACE_RATE_COLUMN)
    starters = conn.execute(
        """
        SELECT s.id AS starter_id,
               s."檔位" AS draw,
               ri.distance AS distance,
               ri.track_name AS track_name,
               ri.race_course_area AS race_course_area
          FROM racecard_starters AS s
          JOIN racecard_race_info AS ri
            ON ri.race_date = s.race_date
           AND ri.racecourse = s.racecourse
           AND ri.race_no = s.race_no
         WHERE s.race_date = ?
           AND s.racecourse = ?
           AND TRIM(COALESCE(s."檔位", '')) != ''
        """,
        (race_date, racecourse),
    ).fetchall()
    updated = 0
    for row in starters:
        starter_id = int(row["starter_id"])
        win_val: str | None = None
        place_val: str | None = None
        if exists:
            track_type = track_type_from_race_course_area(row["race_course_area"])
            dist_txt = normalize_distance(row["distance"])
            draw_txt = str(row["draw"] or "").strip()
            rail = (
                rail_position_for_draw_lookup(
                    track_type=track_type or "",
                    track_name=row["track_name"],
                )
                if track_type
                else None
            )
            if (
                track_type
                and rail is not None
                and dist_txt.isdigit()
                and draw_txt.isdigit()
            ):
                win_val, place_val = lookup_draw_win_place_pct(
                    conn,
                    venue_code=venue_code,
                    track_type=track_type,
                    rail_position=rail,
                    distance_m=int(dist_txt),
                    draw_no=int(draw_txt),
                )
        # Always write (NULL clears stale values when lookup misses).
        cur = conn.execute(
            f"""
            UPDATE racecard_starters
               SET {win_col} = ?,
                   {place_col} = ?
             WHERE id = ?;
            """,
            (win_val, place_val, starter_id),
        )
        updated += int(cur.rowcount or 0)
    return updated


def build_drawadvsearch_url(
    *,
    year: int,
    racecourse: str,
    course: str,
    distance: int,
    going: str = "ALL",
) -> str:
    parts = [
        f"Year={int(year)}",
        f"Racecourse={quote(str(racecourse), safe='')}",
        f"Distance={int(distance)}",
        f"Going={quote(str(going), safe='')}",
    ]
    course_n = str(course or "").strip()
    if course_n:
        parts.append(f"Course={quote(course_n, safe='')}")
    return f"{DRAW_ADVSEARCH_URL}?{'&'.join(parts)}"


def fetch_drawadvsearch_html(url: str, *, timeout: float = 60.0) -> str:
    try:
        resp = requests.get(url, timeout=timeout, headers=_HTTP_HEADERS)
    except requests.RequestException as e:
        raise DrawStatisticsError("draw_fetch_failed", str(e)) from e
    if resp.status_code != 200:
        raise DrawStatisticsError(
            "draw_http_error", f"status={resp.status_code} url={url}"
        )
    text = resp.text or ""
    if not text.strip():
        raise DrawStatisticsError("draw_empty_html", url)
    return text


def discover_draw_search_matrix(html: str) -> tuple[int, list[dict[str, Any]]]:
    """Parse season + local racecourse/course/distance options from advsearch page."""
    soup = BeautifulSoup(html, "html.parser")
    season_sel = soup.find("select", id="seasonId")
    if season_sel is None:
        raise DrawStatisticsError("season_select_missing", "seasonId not found")
    years: list[int] = []
    for opt in season_sel.find_all("option"):
        val = str(opt.get("value") or "").strip()
        if re.fullmatch(r"\d{4}", val):
            years.append(int(val))
    if not years:
        raise DrawStatisticsError("season_options_empty", "no seasonId years")
    year = max(years)

    rc_sel = soup.find("select", id="racecourseId")
    if rc_sel is None:
        raise DrawStatisticsError("racecourse_select_missing", "racecourseId not found")
    racecourses = [
        str(o.get("value") or "").strip()
        for o in rc_sel.find_all("option")
        if str(o.get("value") or "").strip() in RACECOURSE_MAP
    ]
    if not racecourses:
        raise DrawStatisticsError(
            "racecourse_options_empty", "no local STT/STA/HVT options"
        )

    combos: list[dict[str, Any]] = []
    for rc in racecourses:
        course_sel = soup.find("select", id=f"courseId_{year}{rc}")
        dist_sel = soup.find("select", id=f"distanceId_{year}{rc}")
        if dist_sel is None:
            raise DrawStatisticsError(
                "distance_select_missing", f"distanceId_{year}{rc}"
            )
        distances = [
            int(str(o.get("value") or "").strip())
            for o in dist_sel.find_all("option")
            if re.fullmatch(r"\d{3,4}", str(o.get("value") or "").strip())
        ]
        if not distances:
            raise DrawStatisticsError(
                "distance_options_empty", f"distanceId_{year}{rc}"
            )
        courses: list[str] = []
        if course_sel is not None:
            for o in course_sel.find_all("option"):
                val = str(o.get("value") or "").strip()
                # AWT uses empty value = not applicable.
                courses.append(val)
        if not courses:
            courses = [""]
        for course in courses:
            for distance in distances:
                combos.append(
                    {
                        "year": year,
                        "racecourse": rc,
                        "course": course,
                        "distance": distance,
                        "going": "ALL",
                    }
                )
    if not combos:
        raise DrawStatisticsError("combo_matrix_empty", "no download combinations")
    return year, combos


def parse_drawadvsearch_result_html(
    html: str,
    *,
    venue_code: str,
    track_type: str,
    rail_position: str,
    distance_m: int,
    source_url: str,
    fetched_at: str,
) -> list[DrawStatisticsRow]:
    soup = BeautifulSoup(html, "html.parser")
    # HKJC empty answer for a course/distance with no races in the window.
    page_text = soup.get_text(" ", strip=True)
    if "找不到相關資料" in page_text and "檔位" not in page_text:
        return []
    target = None
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True)
        if "檔位" in text and "出賽" in text and "冠" in text:
            target = table
            break
    if target is None:
        if "找不到相關資料" in page_text:
            return []
        raise DrawStatisticsError("draw_table_missing", source_url)

    rows_out: list[DrawStatisticsRow] = []
    for tr in target.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        if cells[0] in {"檔位", "Draw"}:
            continue
        if "大熱門" in cells[0]:
            continue
        if not re.fullmatch(r"\d{1,2}", cells[0]):
            continue
        if len(cells) < 10:
            raise DrawStatisticsError(
                "draw_row_short", f"cells={cells!r} url={source_url}"
            )
        draw_no = _require_int(cells[0], field="draw_no")
        starts = _require_int(cells[1], field="starts")
        wins = _require_int(cells[2], field="wins")
        seconds = _require_int(cells[3], field="seconds")
        thirds = _require_int(cells[4], field="thirds")
        fourths = _require_int(cells[5], field="fourths")
        win_pct = _parse_pct(cells[6], field="win_pct")
        quinella_pct = _parse_pct(cells[7], field="quinella_pct")
        place_pct = _parse_pct(cells[8], field="place_pct")
        top4_pct = _parse_pct(cells[9], field="top4_pct")
        row = DrawStatisticsRow(
            venue_code=venue_code,
            track_type=track_type,
            rail_position=rail_position,
            distance_m=int(distance_m),
            draw_no=draw_no,
            starts=starts,
            wins=wins,
            seconds=seconds,
            thirds=thirds,
            fourths=fourths,
            win_pct=win_pct,
            quinella_pct=quinella_pct,
            place_pct=place_pct,
            top4_pct=top4_pct,
            source=DRAW_STATISTICS_OPERATION,
            source_url=source_url,
            fetched_at=fetched_at,
        )
        if row.lose_count < 0:
            raise DrawStatisticsError(
                "invalid_lose_count",
                f"starts={starts} wins={wins} 2={seconds} 3={thirds} 4={fourths}",
            )
        rows_out.append(row)
    return rows_out


def upsert_draw_statistics_rows(
    db_path: Path,
    rows: list[DrawStatisticsRow],
    *,
    overwrite: bool = False,
) -> dict[str, int]:
    ensure_draw_statistics_table(db_path)
    stamp = utc_now_iso()
    inserted = 0
    updated = 0
    skipped = 0
    filled = 0
    sql_lookup = """
        SELECT id, starts, wins, seconds, thirds, fourths,
               win_pct, quinella_pct, place_pct, top4_pct,
               source, source_url, fetched_at
        FROM racing_draw_statistics
        WHERE venue_code = ? AND track_type = ? AND rail_position = ?
          AND distance_m = ? AND draw_no = ?
        LIMIT 1
    """
    sql_insert = """
        INSERT INTO racing_draw_statistics (
          venue_code, track_type, rail_position, distance_m, draw_no,
          starts, wins, seconds, thirds, fourths,
          win_pct, quinella_pct, place_pct, top4_pct,
          win_count, in_place_count, lose_count,
          source, source_url, fetched_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    sql_update_all = """
        UPDATE racing_draw_statistics
        SET starts = ?, wins = ?, seconds = ?, thirds = ?, fourths = ?,
            win_pct = ?, quinella_pct = ?, place_pct = ?, top4_pct = ?,
            win_count = ?, in_place_count = ?, lose_count = ?,
            source = ?, source_url = ?, fetched_at = ?, updated_at = ?
        WHERE id = ?
    """

    def params_for(row: DrawStatisticsRow) -> tuple[Any, ...]:
        return (
            row.venue_code,
            row.track_type,
            row.rail_position,
            row.distance_m,
            row.draw_no,
            row.starts,
            row.wins,
            row.seconds,
            row.thirds,
            row.fourths,
            row.win_pct,
            row.quinella_pct,
            row.place_pct,
            row.top4_pct,
            row.win_count,
            row.in_place_count,
            row.lose_count,
            row.source,
            row.source_url,
            row.fetched_at,
            stamp,
        )

    with get_connection(db_path) as conn:
        for row in rows:
            existing = conn.execute(
                sql_lookup,
                (
                    row.venue_code,
                    row.track_type,
                    row.rail_position,
                    row.distance_m,
                    row.draw_no,
                ),
            ).fetchone()
            if existing is None:
                conn.execute(sql_insert, params_for(row))
                inserted += 1
                continue
            if overwrite:
                conn.execute(
                    sql_update_all,
                    (
                        row.starts,
                        row.wins,
                        row.seconds,
                        row.thirds,
                        row.fourths,
                        row.win_pct,
                        row.quinella_pct,
                        row.place_pct,
                        row.top4_pct,
                        row.win_count,
                        row.in_place_count,
                        row.lose_count,
                        row.source,
                        row.source_url,
                        row.fetched_at,
                        stamp,
                        int(existing["id"]),
                    ),
                )
                updated += 1
                continue
            # overwrite=false: fill only empty text meta fields; never overwrite stats.
            patches: list[str] = []
            vals: list[Any] = []
            for col in ("source", "source_url", "fetched_at"):
                cur = str(existing[col] or "").strip()
                new = str(getattr(row, col) or "").strip()
                if cur == "" and new != "":
                    patches.append(f"{col} = ?")
                    vals.append(new)
            if patches:
                vals.append(stamp)
                vals.append(int(existing["id"]))
                conn.execute(
                    f"UPDATE racing_draw_statistics SET {', '.join(patches)}, "
                    f"updated_at = ? WHERE id = ?",
                    vals,
                )
                filled += 1
            else:
                skipped += 1
        conn.commit()
    return {
        "inserted": inserted,
        "updated": updated,
        "filled": filled,
        "skipped": skipped,
    }


def normalize_history_search_op(raw: str | None) -> str:
    op = str(raw or "").strip().lower()
    if op in HISTORY_SEARCH_OPS_ALL:
        return op
    return DEFAULT_HISTORY_SEARCH_OP


def parse_draw_statistics_sort_param(raw: str | None) -> list[tuple[str, str]] | None:
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
        if key not in _SORT_SQL:
            continue
        if direction not in ("asc", "desc"):
            continue
        specs.append((key, direction))
    return specs or None


def draw_statistics_sort_param(specs: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}:{d}" for k, d in specs)


def _normalize_page(n: int | None) -> int:
    try:
        page = int(n) if n is not None else 1
    except (TypeError, ValueError):
        page = 1
    return max(1, page)


def _build_q_clause(
    q: str,
    q_op: str,
    search_keys: list[str],
    *,
    whitelist: frozenset[str],
) -> tuple[str, list[Any]]:
    q = (q or "").strip()
    if not q:
        return "", []
    keys = [k for k in search_keys if k in whitelist and k in _COL_SQL]
    if not keys:
        raise DrawStatisticsSearchError("no_searchable_fields")
    exprs = [_COL_SQL[k] for k in keys]
    op = normalize_history_search_op(q_op)
    params: list[Any] = []

    def as_text(e: str) -> str:
        return f"CAST({e} AS TEXT)"

    if op == HISTORY_SEARCH_OP_APPROX:
        parts = [f"instr(lower({as_text(e)}), ?) > 0" for e in exprs]
        params.extend([q.lower()] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_EQ:
        parts = [f"{as_text(e)} = ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NE:
        parts = [f"ifnull({as_text(e)}, '') != ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_CONTAINS:
        parts = [f"instr({as_text(e)}, ?) > 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NOT_CONTAINS:
        parts = [f"instr(ifnull({as_text(e)}, ''), ?) = 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_STARTS:
        parts = [f"{as_text(e)} LIKE ?" for e in exprs]
        params.extend([q + "%"] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_ENDS:
        parts = [f"{as_text(e)} LIKE ?" for e in exprs]
        params.extend(["%" + q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    return "", []


def _row_to_view(row: Any) -> dict[str, str]:
    keys = list(row.keys()) if hasattr(row, "keys") else []
    out: dict[str, str] = {}
    for key in _COL_SQL:
        out[key] = format_field_display(key, row[key] if key in keys else None)
    return out


def _filter_options(conn: Any) -> dict[str, list[str]]:
    def distinct(col: str) -> list[str]:
        rows = conn.execute(
            f"""
            SELECT DISTINCT CAST({col} AS TEXT) AS v
            FROM racing_draw_statistics
            WHERE TRIM(COALESCE(CAST({col} AS TEXT), '')) != ''
            ORDER BY v
            """
        ).fetchall()
        return [str(r["v"]) for r in rows]

    return {
        "venue_code": distinct("venue_code"),
        "rail_position": distinct("rail_position"),
        "distance_m": distinct("distance_m"),
        "draw_no": distinct("draw_no"),
        "track_type": distinct("track_type"),
    }


def fetch_draw_statistics_filtered(
    db_path: Path,
    *,
    n: int | None = 1,
    q: str = "",
    q_op: str = DEFAULT_HISTORY_SEARCH_OP,
    sort_specs: list[tuple[str, str]] | None = None,
    search_keys: list[str] | None = None,
    page_size: int = DRAW_STATISTICS_PAGE_SIZE,
    search_whitelist: frozenset[str] | None = None,
    venue_code: str | None = None,
    rail_position: str | None = None,
    distance_m: str | None = None,
    draw_no: str | None = None,
) -> DrawStatisticsListResult:
    from hppv.racing_layout import (
        DEFAULT_DRAW_STATISTICS_SEARCH_KEYS,
        RACING_DRAW_STATISTICS_TEXT_SEARCH_WHITELIST,
    )

    ensure_draw_statistics_table(db_path)
    page = _normalize_page(n)
    size = max(1, int(page_size or DRAW_STATISTICS_PAGE_SIZE))
    whitelist = search_whitelist or RACING_DRAW_STATISTICS_TEXT_SEARCH_WHITELIST
    keys = (
        list(search_keys)
        if search_keys is not None
        else list(DEFAULT_DRAW_STATISTICS_SEARCH_KEYS)
    )
    specs = sort_specs if sort_specs else [
        ("venue_code", "asc"),
        ("rail_position", "asc"),
        ("distance_m", "asc"),
        ("draw_no", "asc"),
    ]

    where: list[str] = []
    params: list[Any] = []
    venue_n = str(venue_code or "").strip() or None
    rail_raw = None if rail_position is None else str(rail_position).strip()
    rail_n = rail_raw if rail_raw not in (None, "") else None
    dist_n = str(distance_m or "").strip() or None
    draw_n = str(draw_no or "").strip() or None
    if venue_n:
        where.append("ds.venue_code = ?")
        params.append(venue_n)
    if rail_n is not None:
        where.append("ds.rail_position = ?")
        params.append(rail_n)
    if dist_n:
        where.append("CAST(ds.distance_m AS TEXT) = ?")
        params.append(dist_n)
    if draw_n:
        where.append("CAST(ds.draw_no AS TEXT) = ?")
        params.append(draw_n)

    q_clause, q_params = _build_q_clause(q, q_op, keys, whitelist=whitelist)
    if q_clause:
        where.append(q_clause)
        params.extend(q_params)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    order_parts: list[str] = []
    for key, direction in specs:
        expr = _SORT_SQL.get(key)
        if not expr:
            continue
        order_parts.append(f"{expr} {'ASC' if direction == 'asc' else 'DESC'}")
    order_parts.append("ds.id DESC")
    order_sql = ", ".join(order_parts)
    select_cols = ",\n      ".join(f"{expr} AS {key}" for key, expr in _COL_SQL.items())
    from_sql = "FROM racing_draw_statistics AS ds"

    with get_connection(db_path) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS c {from_sql}{where_sql}",
                params,
            ).fetchone()["c"]
            or 0
        )
        total_pages = max(1, int(math.ceil(total / size))) if total else 0
        if total_pages and page > total_pages:
            page = total_pages
        offset = (page - 1) * size if total else 0
        rows_raw = conn.execute(
            f"""
            SELECT {select_cols}
            {from_sql}
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, size, offset],
        ).fetchall()
        options = _filter_options(conn)

    return DrawStatisticsListResult(
        rows=[_row_to_view(r) for r in rows_raw],
        n=page if total else 0,
        total_count=total,
        total_pages=total_pages,
        page_size=size,
        q=q or "",
        all_matches=True,
        sort=draw_statistics_sort_param(specs),
        q_op=normalize_history_search_op(q_op),
        field_keys=tuple(_COL_SQL.keys()),
        search_keys=tuple(keys),
        filter_options=options,
        venue_code=venue_n,
        rail_position=rail_n,
        distance_m=dist_n,
        draw_no=draw_n,
    )


def draw_statistics_layout_i18n_maps(
    text_map: Any, keys: Any
) -> tuple[dict[str, str], dict[str, str]]:
    labels: dict[str, str] = {}
    descs: dict[str, str] = {}
    for key in keys:
        raw_label = (text_map.get(f"racing.draw_statistics.col.{key}") or "").strip()
        labels[str(key)] = raw_label if raw_label else str(key)
        descs[str(key)] = (
            text_map.get(f"racing.draw_statistics.col.desc.{key}") or ""
        ).strip()
    return labels, descs


def download_all_draw_statistics(
    *,
    progress_cb: Any | None = None,
) -> tuple[list[DrawStatisticsRow], dict[str, Any]]:
    """Fetch latest-season Going=ALL matrix; return flat rows + detail."""

    def emit(phase: str, message: str, **extra: Any) -> None:
        if progress_cb is None:
            return
        payload = {"phase": phase, "message": message, **extra}
        try:
            progress_cb(payload)
        except Exception:
            pass

    fetched_at = utc_now_iso()
    emit("fetching_total", "正在取得組合清單")
    blank = fetch_drawadvsearch_html(DRAW_ADVSEARCH_URL)
    year, combos = discover_draw_search_matrix(blank)
    emit("processing", f"組合 {len(combos)}", total=len(combos), processed=0)

    all_rows: list[DrawStatisticsRow] = []
    combo_details: list[dict[str, Any]] = []
    failed = 0
    for idx, combo in enumerate(combos, start=1):
        rc = str(combo["racecourse"])
        venue_code, track_type = RACECOURSE_MAP[rc]
        rail = str(combo.get("course") or "").strip()
        distance = int(combo["distance"])
        url = build_drawadvsearch_url(
            year=int(combo["year"]),
            racecourse=rc,
            course=rail,
            distance=distance,
            going=str(combo.get("going") or "ALL"),
        )
        try:
            html = fetch_drawadvsearch_html(url)
            rows = parse_drawadvsearch_result_html(
                html,
                venue_code=venue_code,
                track_type=track_type,
                rail_position=rail,
                distance_m=distance,
                source_url=url,
                fetched_at=fetched_at,
            )
            all_rows.extend(rows)
            combo_details.append(
                {
                    "year": year,
                    "racecourse": rc,
                    "course": rail,
                    "distance": distance,
                    "url": url,
                    "rows": len(rows),
                    "empty": len(rows) == 0,
                }
            )
        except DrawStatisticsError as e:
            failed += 1
            combo_details.append(
                {
                    "year": year,
                    "racecourse": rc,
                    "course": rail,
                    "distance": distance,
                    "url": url,
                    "error": e.code,
                    "detail": e.detail,
                }
            )
            raise
        emit(
            "processing",
            f"已處理組合 {idx}/{len(combos)} · 列 {len(all_rows)}",
            total=len(combos),
            processed=idx,
            rows=len(all_rows),
        )
    if not all_rows:
        raise DrawStatisticsError(
            "draw_rows_empty",
            f"season={year} produced 0 draw statistic rows across {len(combos)} combos",
        )
    detail = {
        "season_year": year,
        "going": "ALL",
        "combos": len(combos),
        "combo_details": combo_details,
        "failed_combos": failed,
        "empty_combos": sum(1 for c in combo_details if c.get("empty")),
        "row_count": len(all_rows),
        "endpoint": DRAW_ADVSEARCH_URL,
    }
    return all_rows, detail
