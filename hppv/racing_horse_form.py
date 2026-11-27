"""Racing horse form panel: same-distance / recent / place stats (read-only).

Mirrors soccer_match_form patterns for racing daily expand panels.
Shared sampling (HORSE_FORM_LIMIT / HORSE_FORM_LIMIT_OPTIONS):
- Newest-first before anchor; take at most that many rows
- WV/WV-A occupy sample slots but are not valid_played
- Empty cells use —
- Cutoff: race date strictly before anchor ISO date
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from hppv.db import _RACE_RECORD_DATE_ORDER_EXPR, get_connection, is_acceptable_horse_id
from hppv.display_format import EMPTY_DISPLAY, format_date_display, format_text_field
from hppv.schema import (
    RACECARD_ATTAINMENT_RATE_COLUMN,
    RACECARD_DRAW_PLACE_RATE_COLUMN,
    RACECARD_DRAW_WIN_RATE_COLUMN,
    RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN,
    RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN,
    RACECARD_WEIGHTED_WIN_RATE_COLUMN,
    qident,
)

logger = logging.getLogger(__name__)

# Shared sampling settings (single source; UI/API/filters must use these).
HORSE_FORM_LIMIT = 12
HORSE_FORM_LIMIT_OPTIONS: tuple[int, ...] = (HORSE_FORM_LIMIT,)
HORSE_FORM_LIMITS: frozenset[int] = frozenset(HORSE_FORM_LIMIT_OPTIONS)
DEFAULT_HORSE_FORM_LIMIT = HORSE_FORM_LIMIT

# Section 2（近場往績）場次下拉：全部／12／20／30。
RECENT_FORM_LIMIT_ALL = "all"
RECENT_FORM_LIMIT_OPTIONS: tuple[int, ...] = (12, 20, 30)
RECENT_FORM_LIMIT_CHOICES: frozenset[str] = frozenset(
    {RECENT_FORM_LIMIT_ALL, *(str(n) for n in RECENT_FORM_LIMIT_OPTIONS)}
)
DEFAULT_RECENT_FORM_LIMIT: int | str = 12
# Cap when 「全部」：足夠覆蓋單一馬隻往績，避免無 LIMIT 全表掃描。
RECENT_FORM_ALL_FETCH_CAP = 500

RecentFilter = Literal["all", "same_venue", "same_distance"]
RecentFormLimit = int | Literal["all"]
PlaceBucket = Literal[
    "first", "second", "third", "fourth", "other", "withdrawn", "unknown"
]

_RE_DISTANCE_DIGITS = re.compile(r"(\d+)")
_RE_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_WV = re.compile(r"^WV(?:-A)?$", re.IGNORECASE)
_RE_HANDICAP_POUNDS = re.compile(r"^\d+$")


class HorseFormError(Exception):
    code = "read_error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class HorseFormValidationError(HorseFormError):
    code = "invalid_request"


@dataclass(frozen=True, slots=True)
class HorseFormAnchor:
    horse_id: str
    race_date_iso: str
    venue: str
    distance: str
    race_no: str | None = None
    horse_name: str | None = None
    handicap: str | None = None


@dataclass(frozen=True, slots=True)
class HorseFormFilters:
    limit: int = DEFAULT_HORSE_FORM_LIMIT
    recent_limit: RecentFormLimit = DEFAULT_RECENT_FORM_LIMIT
    # Kept for API backward-compat; UI no longer exposes venue/distance filter.
    recent_filter: RecentFilter = "all"


def normalize_horse_form_limit(raw: Any) -> int:
    """Return allowed limit. Omitted/blank → default; other values → validation error."""
    if raw is None:
        return DEFAULT_HORSE_FORM_LIMIT
    text = str(raw).strip()
    if text == "":
        return DEFAULT_HORSE_FORM_LIMIT
    try:
        value = int(text)
    except (TypeError, ValueError) as e:
        raise HorseFormValidationError(f"invalid limit: {raw!r}") from e
    if value not in HORSE_FORM_LIMITS:
        raise HorseFormValidationError(f"invalid limit: {value}")
    return value


def normalize_recent_form_limit(raw: Any) -> RecentFormLimit:
    """近場場次：全部／12／20／30。空白 → 預設 12。"""
    if raw is None:
        return DEFAULT_RECENT_FORM_LIMIT
    text = str(raw).strip().lower()
    if text == "":
        return DEFAULT_RECENT_FORM_LIMIT
    if text in ("all", "全部"):
        return RECENT_FORM_LIMIT_ALL
    try:
        value = int(text)
    except (TypeError, ValueError) as e:
        raise HorseFormValidationError(f"invalid recent_limit: {raw!r}") from e
    if value not in RECENT_FORM_LIMIT_OPTIONS:
        raise HorseFormValidationError(f"invalid recent_limit: {value}")
    return value


def recent_form_fetch_limit(recent_limit: RecentFormLimit) -> int:
    """Resolve SQL/take limit for recent rows."""
    if recent_limit == RECENT_FORM_LIMIT_ALL:
        return RECENT_FORM_ALL_FETCH_CAP
    return int(recent_limit)


def normalize_recent_filter(raw: Any) -> RecentFilter:
    """Legacy filter; unused by UI. Unknown → all (no invent of other modes)."""
    text = str(raw or "all").strip().lower()
    if text in ("all", "same_venue", "same_distance"):
        return text  # type: ignore[return-value]
    if text in ("venue", "same-venue"):
        return "same_venue"
    if text in ("distance", "same-distance", "same_course"):
        return "same_distance"
    return "all"


def normalize_distance(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text or text == EMPTY_DISPLAY:
        return ""
    m = _RE_DISTANCE_DIGITS.search(text)
    return m.group(1) if m else text


def normalize_venue(raw: Any) -> str:
    text = str(raw or "").strip()
    if text == EMPTY_DISPLAY:
        return ""
    return text


def normalize_anchor_date_iso(raw: Any) -> str:
    text = str(raw or "").strip()
    if _RE_ISO_DATE.fullmatch(text):
        return text
    disp = format_date_display(text)
    if _RE_ISO_DATE.fullmatch(disp):
        return disp
    raise HorseFormValidationError(f"invalid anchor date: {text!r}")


def parse_handicap_pounds(raw: Any) -> int | None:
    """Parse racecard/history handicap as whole pounds. Missing/invalid → None (no invent)."""
    text = str(raw or "").strip()
    if not text or text == EMPTY_DISPLAY:
        return None
    if not _RE_HANDICAP_POUNDS.fullmatch(text):
        return None
    return int(text)


def parse_anchor(
    *,
    horse_id: Any,
    race_date: Any,
    venue: Any,
    distance: Any,
    race_no: Any = None,
    horse_name: Any = None,
    handicap: Any = None,
) -> HorseFormAnchor:
    from hppv.racing_hkjc_graphql import venue_for_race_records

    hid = str(horse_id or "").strip()
    if not hid or not is_acceptable_horse_id(hid):
        raise HorseFormValidationError("invalid horse_id")
    date_iso = normalize_anchor_date_iso(race_date)
    dist = normalize_distance(distance)
    ven_raw = normalize_venue(venue)
    # ST/HV/FC → Chinese race_records.venue; already-Chinese labels kept.
    ven = venue_for_race_records(ven_raw) if ven_raw else ""
    if not dist:
        raise HorseFormValidationError("distance required")
    rn = str(race_no or "").strip() or None
    name = str(horse_name or "").strip() or None
    hc_raw = str(handicap).strip() if handicap is not None else ""
    hc = hc_raw if hc_raw and hc_raw != EMPTY_DISPLAY else None
    return HorseFormAnchor(
        horse_id=hid,
        race_date_iso=date_iso,
        venue=ven,
        distance=dist,
        race_no=rn,
        horse_name=name,
        handicap=hc,
    )


def classify_finish_pos(raw: Any) -> PlaceBucket:
    text = str(raw or "").strip()
    if not text or text == EMPTY_DISPLAY:
        return "unknown"
    if _RE_WV.fullmatch(text):
        return "withdrawn"
    if text.isdigit():
        n = int(text)
        if n == 1:
            return "first"
        if n == 2:
            return "second"
        if n == 3:
            return "third"
        if n == 4:
            return "fourth"
        if n >= 5:
            return "other"
        return "unknown"
    # Non-numeric official marks other than WV → unknown (not treated as loss).
    return "unknown"


def _row_to_view(row: Any) -> dict[str, str]:
    finish = format_text_field(row["finish_pos"] if "finish_pos" in row.keys() else None)
    keys = row.keys() if hasattr(row, "keys") else ()
    return {
        "race_date": format_date_display(
            row["race_date"] if "race_date" in keys else None
        ),
        "venue": format_text_field(row["venue"] if "venue" in keys else None),
        "distance": format_text_field(row["distance"] if "distance" in keys else None),
        "class_band": format_text_field(
            row["class_band"] if "class_band" in keys else None
        ),
        "course": format_text_field(row["course"] if "course" in keys else None),
        "draw": format_text_field(row["draw"] if "draw" in keys else None),
        "handicap": format_text_field(row["handicap"] if "handicap" in keys else None),
        "jockey": format_text_field(row["jockey"] if "jockey" in keys else None),
        "rating": format_text_field(row["rating"] if "rating" in keys else None),
        "finish_pos": finish,
        "running_pos": format_text_field(
            row["running_pos"] if "running_pos" in keys else None
        ),
        "time": format_text_field(row["time"] if "time" in keys else None),
        "win_odds": format_text_field(row["win_odds"] if "win_odds" in keys else None),
        "place_bucket": classify_finish_pos(
            row["finish_pos"] if "finish_pos" in keys else None
        ),
    }


def is_awt_course(course: Any) -> bool:
    """True when race_records.course indicates 全天候／AWT."""
    from hppv.racing_standard_times import track_type_from_race_records_course

    return track_type_from_race_records_course(course) == "AWT"


def valid_played_total_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    """Return valid_played / total for a row sample (WV counts in total only)."""
    summary = summarize_places(rows)
    return {
        "valid_played": int(summary["valid_played"]),
        "total": int(summary["played"]),
    }


def surface_place_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Place chips + valid/total for one surface group (shared summarize_places)."""
    summary = summarize_places(rows)
    return {
        "valid_played": int(summary["valid_played"]),
        "total": int(summary["played"]),
        "first": int(summary["first"]),
        "second": int(summary["second"]),
        "third": int(summary["third"]),
        "fourth": int(summary["fourth"]),
        "other": int(summary["other"]),
        "withdrawn": int(summary["withdrawn"]),
        "unknown": int(summary["unknown"]),
    }


def fetch_other_stats_by_surface(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    fetch_cap: int = RECENT_FORM_ALL_FETCH_CAP,
) -> dict[str, Any]:
    """Career-to-date (before anchor) place + valid/total for HV / ST(turf) / AWT.

    Groups are mutually exclusive:
      - HV: venue 跑馬地
      - ST: venue 沙田 and not AWT course
      - AWT: course is 全天候／AWT (usually 沙田)
    """
    raw = _fetch_candidates(
        conn,
        horse_id=anchor.horse_id,
        before_iso=anchor.race_date_iso,
        limit=fetch_cap,
        venue=None,
    )
    # Take all fetched rows as views (cap already applied).
    views = _take_views(raw, limit=max(fetch_cap, 1), distance=None)
    hv_rows: list[dict[str, str]] = []
    st_rows: list[dict[str, str]] = []
    awt_rows: list[dict[str, str]] = []
    for row in views:
        if is_awt_course(row.get("course")):
            awt_rows.append(row)
            continue
        ven = str(row.get("venue") or "").strip()
        if ven == "跑馬地":
            hv_rows.append(row)
        elif ven == "沙田":
            st_rows.append(row)
    return {
        "hv": {"code": "HV", "venue": "跑馬地", **surface_place_stats(hv_rows)},
        "st": {"code": "ST", "venue": "沙田", **surface_place_stats(st_rows)},
        "awt": {"code": "AWT", "venue": "全天候", **surface_place_stats(awt_rows)},
    }


def summarize_places(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Stats for the same row set as section 2 (recent)."""
    first = second = third = fourth = other = withdrawn = unknown = 0
    for row in rows:
        bucket = row.get("place_bucket") or classify_finish_pos(row.get("finish_pos"))
        if bucket == "first":
            first += 1
        elif bucket == "second":
            second += 1
        elif bucket == "third":
            third += 1
        elif bucket == "fourth":
            fourth += 1
        elif bucket == "other":
            other += 1
        elif bucket == "withdrawn":
            withdrawn += 1
        else:
            unknown += 1
    valid = first + second + third + fourth + other
    if valid <= 0:
        win_rate: str | float = EMPTY_DISPLAY
        place_rate: str | float = EMPTY_DISPLAY
    else:
        win_rate = round(first / valid, 4)
        place_rate = round((first + second + third) / valid, 4)
    return {
        "played": len(rows),
        "valid_played": valid,
        "first": first,
        "second": second,
        "third": third,
        "fourth": fourth,
        "other": other,
        "withdrawn": withdrawn,
        "unknown": unknown,
        "win_rate": win_rate,
        "place_rate": place_rate,
    }


def summarize_win_counts(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Shared wins / valid_played / win_rate from the same place rules as summarize_places."""
    summary = summarize_places(rows)
    return {
        "wins": int(summary["first"]),
        "valid_played": int(summary["valid_played"]),
        "win_rate": summary["win_rate"],
    }


def summarize_handicap_groups(
    rows: list[dict[str, str]],
    *,
    today_handicap: Any,
) -> dict[str, Any]:
    """Split form rows by historical handicap vs today's pounds (non-exclusive).

    Caller supplies the sample (horse form uses 同場同程 rows).
    """
    today_display = format_text_field(today_handicap)
    today_n = parse_handicap_pounds(today_handicap)
    empty = summarize_win_counts([])
    if today_n is None:
        return {
            "today_handicap": today_display,
            "today_handicap_valid": False,
            "ge": empty,
            "le": empty,
        }
    ge_rows: list[dict[str, str]] = []
    le_rows: list[dict[str, str]] = []
    for row in rows:
        hist_n = parse_handicap_pounds(row.get("handicap"))
        if hist_n is None:
            continue
        if hist_n >= today_n:
            ge_rows.append(row)
        if hist_n <= today_n:
            le_rows.append(row)
    return {
        "today_handicap": str(today_n),
        "today_handicap_valid": True,
        "ge": summarize_win_counts(ge_rows),
        "le": summarize_win_counts(le_rows),
    }


def _fetch_candidates(
    conn: Any,
    *,
    horse_id: str,
    before_iso: str,
    limit: int,
    venue: str | None = None,
) -> list[Any]:
    """Fetch newest-first rows before anchor; optional venue filter in SQL."""
    params: list[Any] = [horse_id, before_iso]
    where = [
        "rr.horse_id = ?",
        f"{_RACE_RECORD_DATE_ORDER_EXPR} < ?",
        "TRIM(COALESCE(rr.race_date, '')) != ''",
    ]
    if venue:
        where.append("TRIM(COALESCE(rr.venue, '')) = ?")
        params.append(venue)
    # Buffer for post-filters (same distance) / sparse histories.
    fetch_n = max(limit * 8, limit)
    params.append(fetch_n)
    sql = f"""
        SELECT
          rr.race_date,
          rr.venue,
          rr.distance,
          rr.class_band,
          rr.course,
          rr.draw,
          rr.handicap,
          rr.jockey,
          rr.rating,
          rr.finish_pos,
          rr.running_pos,
          rr.time,
          rr.win_odds,
          rr.race_no
        FROM race_records AS rr
        WHERE {" AND ".join(where)}
        ORDER BY {_RACE_RECORD_DATE_ORDER_EXPR} DESC, CAST(rr.race_no AS INTEGER) DESC, rr.id DESC
        LIMIT ?
    """
    return list(conn.execute(sql, params).fetchall())


def _take_views(
    rows: list[Any],
    *,
    limit: int,
    distance: str | None = None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in rows:
        if distance and normalize_distance(r["distance"]) != distance:
            continue
        out.append(_row_to_view(r))
        if len(out) >= limit:
            break
    return out


def fetch_same_distance_form(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int,
) -> list[dict[str, str]]:
    rows = _fetch_candidates(
        conn,
        horse_id=anchor.horse_id,
        before_iso=anchor.race_date_iso,
        limit=limit,
    )
    return _take_views(rows, limit=limit, distance=anchor.distance)


def fetch_same_venue_distance_form(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int,
    venue: str | None = None,
) -> list[dict[str, str]]:
    """Same venue + same distance; reuses _fetch_candidates / _take_views sampling.

    ``venue`` overrides anchor.venue when provided (code or Chinese).
    """
    from hppv.racing_hkjc_graphql import venue_for_race_records

    venue_raw = venue if venue is not None else anchor.venue
    venue_zh = venue_for_race_records(venue_raw)
    if not venue_zh:
        return []
    rows = _fetch_candidates(
        conn,
        horse_id=anchor.horse_id,
        before_iso=anchor.race_date_iso,
        limit=limit,
        venue=venue_zh,
    )
    return _take_views(rows, limit=limit, distance=anchor.distance)


def win_rate_to_pct_points(win_rate: Any) -> float | None:
    """Shared win_rate (ratio) → percentage points. EMPTY / invalid → None (no 0% invent)."""
    if win_rate is None or win_rate == EMPTY_DISPLAY:
        return None
    try:
        ratio = float(win_rate)
    except (TypeError, ValueError):
        return None
    if ratio != ratio:  # NaN
        return None
    return ratio * 100.0


def format_pct_points_for_db(pct: float | None) -> str | None:
    """Store percentage points as numeric text without '%'. None → NULL."""
    if pct is None:
        return None
    text = f"{pct:.4f}".rstrip("0").rstrip(".")
    return text if text else None


def compute_same_venue_distance_win_rate_pct_points(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> float | None:
    """Venue+distance win rate as percentage points via summarize_win_counts."""
    rows = fetch_same_venue_distance_form(conn, anchor, limit=limit)
    if not rows:
        return None
    summary = summarize_win_counts(rows)
    if int(summary["valid_played"]) <= 0:
        return None
    return win_rate_to_pct_points(summary["win_rate"])


def compute_same_venue_distance_place_rate_pct_points(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> float | None:
    """Venue+distance top-3 place rate as percentage points via summarize_places."""
    rows = fetch_same_venue_distance_form(conn, anchor, limit=limit)
    if not rows:
        return None
    summary = summarize_places(rows)
    if int(summary["valid_played"]) <= 0:
        return None
    return win_rate_to_pct_points(summary["place_rate"])


def compute_same_venue_distance_win_place_pct_points(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> tuple[float | None, float | None]:
    """One form fetch → (win %, place %) percentage points."""
    rows = fetch_same_venue_distance_form(conn, anchor, limit=limit)
    if not rows:
        return None, None
    summary = summarize_places(rows)
    if int(summary["valid_played"]) <= 0:
        return None, None
    return (
        win_rate_to_pct_points(summary["win_rate"]),
        win_rate_to_pct_points(summary["place_rate"]),
    )


def compute_same_venue_distance_win_place_counts(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> tuple[int, int, int] | None:
    """One form fetch → (valid_played n, wins k, place_top3 k) or None.

    Uses the same fetch + summarize_places path as 同程勝率／同程位置率.
    """
    rows = fetch_same_venue_distance_form(conn, anchor, limit=limit)
    if not rows:
        return None
    summary = summarize_places(rows)
    n = int(summary["valid_played"])
    if n <= 0:
        return None
    wins = int(summary["first"])
    place_top3 = (
        int(summary["first"]) + int(summary["second"]) + int(summary["third"])
    )
    return n, wins, place_top3


def compute_same_venue_distance_attainment_counts(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> tuple[int, int] | None:
    """Same-venue+distance sample → (attained k, valid n) or None when n==0.

    Reuses fetch_same_venue_distance_form (limit 12). A start counts as valid only
    when finish time parses to seconds and an exact standard-time row exists for
    that start's venue/track/distance/class with published_on ≤ that race date.
    """
    from hppv.racing_standard_times import (
        lookup_standard_time_seconds,
        map_class_band_to_standard,
        parse_race_time_to_seconds,
        track_type_from_race_records_course,
        venue_code_from_race_records_venue,
    )

    rows = fetch_same_venue_distance_form(conn, anchor, limit=limit)
    if not rows:
        return None
    attained = 0
    valid = 0
    for row in rows:
        finish_sec = parse_race_time_to_seconds(row.get("time"))
        if finish_sec is None:
            continue
        venue_code = venue_code_from_race_records_venue(row.get("venue"))
        track_type = track_type_from_race_records_course(row.get("course"))
        class_std = map_class_band_to_standard(row.get("class_band"))
        dist_txt = normalize_distance(row.get("distance"))
        race_iso = format_date_display(row.get("race_date"))
        if (
            not venue_code
            or not track_type
            or not class_std
            or not dist_txt.isdigit()
            or not race_iso
            or race_iso == EMPTY_DISPLAY
        ):
            continue
        std_sec = lookup_standard_time_seconds(
            conn,
            venue_code=venue_code,
            track_type=track_type,
            distance_m=int(dist_txt),
            class_band=class_std,
            as_of_iso=race_iso,
        )
        if std_sec is None:
            continue
        valid += 1
        if finish_sec <= std_sec:
            attained += 1
    if valid <= 0:
        return None
    return attained, valid


def compute_same_venue_distance_attainment_pct_points(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> float | None:
    """達標率 percentage points (1dp stored later); n==0 → None; all miss → 0.0."""
    counts = compute_same_venue_distance_attainment_counts(
        conn, anchor, limit=limit
    )
    if counts is None:
        return None
    attained, valid = counts
    return (float(attained) / float(valid)) * 100.0


# 同場同程相關率：本地馬場（ST／HV／FC）依「今日馬場＋同途程」寫入。
_RATE_WRITE_VENUE_CODES: frozenset[str] = frozenset({"ST", "HV", "FC"})


def _is_rate_write_venue(racecourse: str) -> bool:
    """True for ST/HV/FC meetings that store form-derived rates on starters."""
    return str(racecourse or "").strip().upper() in _RATE_WRITE_VENUE_CODES


def update_hv_same_distance_win_rates_for_meeting(
    conn: Any,
    *,
    race_date: str,
    racecourse: str,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> int:
    """After racecard upsert: write same-venue+distance win/place rates (ST/HV/FC)."""
    from hppv.racing_hkjc_graphql import venue_display_name
    from hppv.schema import (
        RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN,
        RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN,
        qident,
    )

    code = str(racecourse or "").strip().upper()
    if not _is_rate_write_venue(code):
        return 0
    venue_zh = venue_display_name(code)
    if not venue_zh:
        return 0
    date_iso = normalize_anchor_date_iso(race_date)
    win_col = qident(RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN)
    place_col = qident(RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN)
    starters = conn.execute(
        """
        SELECT s.id AS starter_id,
               s.horse_id AS horse_id,
               s."馬號" AS horse_no,
               ri.distance AS distance,
               ri.race_no AS race_no
          FROM racecard_starters AS s
          JOIN racecard_race_info AS ri
            ON ri.race_date = s.race_date
           AND ri.racecourse = s.racecourse
           AND ri.race_no = s.race_no
         WHERE s.race_date = ?
           AND s.racecourse = ?
           AND TRIM(COALESCE(s.horse_id, '')) != ''
           AND TRIM(COALESCE(s."馬號", '')) != ''
           AND TRIM(COALESCE(ri.distance, '')) != ''
        """,
        (race_date, racecourse),
    ).fetchall()
    updated = 0
    for row in starters:
        horse_id = str(row["horse_id"] if hasattr(row, "keys") else row[1] or "").strip()
        if not horse_id or not is_acceptable_horse_id(horse_id):
            continue
        distance = row["distance"] if hasattr(row, "keys") else row[3]
        race_no = row["race_no"] if hasattr(row, "keys") else row[4]
        starter_id = int(row["starter_id"] if hasattr(row, "keys") else row[0])
        try:
            anchor = parse_anchor(
                horse_id=horse_id,
                race_date=date_iso,
                venue=venue_zh,
                distance=distance,
                race_no=race_no,
            )
        except HorseFormValidationError:
            continue
        win_pct, place_pct = compute_same_venue_distance_win_place_pct_points(
            conn, anchor, limit=limit
        )
        win_val = format_pct_points_for_db(win_pct)
        place_val = format_pct_points_for_db(place_pct)
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


def recompute_all_hv_same_distance_win_rates(
    db_path: Path,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> dict[str, int]:
    """Recompute racecard_starters.同程勝率／同程位置率 for every ST/HV/FC meeting."""
    meetings = 0
    starters_updated = 0
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT race_date, racecourse
              FROM racecard_starters
             WHERE upper(trim(racecourse)) IN ('ST', 'HV', 'FC')
             ORDER BY race_date, racecourse
            """
        ).fetchall()
        for row in rows:
            race_date = str(row["race_date"] if hasattr(row, "keys") else row[0])
            racecourse = str(row["racecourse"] if hasattr(row, "keys") else row[1])
            n = update_hv_same_distance_win_rates_for_meeting(
                conn,
                race_date=race_date,
                racecourse=racecourse,
                limit=limit,
            )
            meetings += 1
            starters_updated += n
        conn.commit()
    return {"meetings": meetings, "starters_updated": starters_updated}


def update_hv_attainment_rate_for_meeting(
    conn: Any,
    *,
    race_date: str,
    racecourse: str,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> int:
    """After same-distance sample sources: write 達標率 for ST/HV/FC."""
    code = str(racecourse or "").strip().upper()
    if not _is_rate_write_venue(code):
        return 0
    from hppv.racing_hkjc_graphql import venue_display_name

    venue_zh = venue_display_name(code)
    if not venue_zh:
        return 0
    date_iso = normalize_anchor_date_iso(race_date)
    out_col = qident(RACECARD_ATTAINMENT_RATE_COLUMN)
    starters = conn.execute(
        """
        SELECT s.id AS starter_id,
               s.horse_id AS horse_id,
               s."馬號" AS horse_no,
               ri.distance AS distance,
               ri.race_no AS race_no
          FROM racecard_starters AS s
          JOIN racecard_race_info AS ri
            ON ri.race_date = s.race_date
           AND ri.racecourse = s.racecourse
           AND ri.race_no = s.race_no
         WHERE s.race_date = ?
           AND s.racecourse = ?
           AND TRIM(COALESCE(s.horse_id, '')) != ''
           AND TRIM(COALESCE(s."馬號", '')) != ''
           AND TRIM(COALESCE(ri.distance, '')) != ''
        """,
        (race_date, racecourse),
    ).fetchall()
    updated = 0
    for row in starters:
        horse_id = str(row["horse_id"] if hasattr(row, "keys") else row[1] or "").strip()
        if not horse_id or not is_acceptable_horse_id(horse_id):
            continue
        distance = row["distance"] if hasattr(row, "keys") else row[3]
        race_no = row["race_no"] if hasattr(row, "keys") else row[4]
        starter_id = int(row["starter_id"] if hasattr(row, "keys") else row[0])
        try:
            anchor = parse_anchor(
                horse_id=horse_id,
                race_date=date_iso,
                venue=venue_zh,
                distance=distance,
                race_no=race_no,
            )
        except HorseFormValidationError:
            continue
        counts = compute_same_venue_distance_attainment_counts(
            conn, anchor, limit=limit
        )
        if counts is None:
            stored = None
            attained = 0
            valid = 0
            pct = None
        else:
            attained, valid = counts
            pct = (float(attained) / float(valid)) * 100.0
            stored = format_weighted_win_rate_for_db(pct)
        logger.info(
            "達標率 meeting=%s/%s race_no=%s horse_id=%s attained=%s valid=%s rate=%s",
            race_date,
            racecourse,
            race_no,
            horse_id,
            attained,
            valid,
            stored,
        )
        cur = conn.execute(
            f"""
            UPDATE racecard_starters
               SET {out_col} = ?
             WHERE id = ?;
            """,
            (stored, starter_id),
        )
        updated += int(cur.rowcount or 0)
    return updated


def recompute_all_hv_attainment_rates(
    db_path: Path,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> dict[str, int]:
    """Recompute racecard_starters.達標率 for every ST/HV/FC meeting."""
    meetings = 0
    starters_updated = 0
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT race_date, racecourse
              FROM racecard_starters
             WHERE upper(trim(racecourse)) IN ('ST', 'HV', 'FC')
             ORDER BY race_date, racecourse
            """
        ).fetchall()
        for row in rows:
            race_date = str(row["race_date"] if hasattr(row, "keys") else row[0])
            racecourse = str(row["racecourse"] if hasattr(row, "keys") else row[1])
            n = update_hv_attainment_rate_for_meeting(
                conn,
                race_date=race_date,
                racecourse=racecourse,
                limit=limit,
            )
            meetings += 1
            starters_updated += n
        conn.commit()
    return {"meetings": meetings, "starters_updated": starters_updated}


def parse_pct_points_text(raw: Any) -> float | None:
    """Parse stored percentage-point text. NULL/blank/invalid → None; real 0 kept."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text == EMPTY_DISPLAY:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def format_weighted_win_rate_for_db(pct: float | None) -> str | None:
    """Store weighted rate as percentage points with exactly one decimal (HALF_UP)."""
    if pct is None:
        return None
    quantized = Decimal(str(pct)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def compute_weighted_win_rate_pct_points(
    *,
    same_distance_win: float | None,
    same_distance_place: float | None,
    draw_win: float | None,
    draw_place: float | None,
) -> float | None:
    """Weighted average of available percentage-point inputs; all NULL → None."""
    from hppv.racing_rate_settings import RATE_COMPONENT_WEIGHTS

    by_col = {
        RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN: same_distance_win,
        RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN: same_distance_place,
        RACECARD_DRAW_WIN_RATE_COLUMN: draw_win,
        RACECARD_DRAW_PLACE_RATE_COLUMN: draw_place,
    }
    numerator = 0.0
    denominator = 0
    for col, weight in RATE_COMPONENT_WEIGHTS:
        value = by_col[col]
        if value is None:
            continue
        numerator += float(value) * int(weight)
        denominator += int(weight)
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def update_hv_weighted_win_rate_for_meeting(
    conn: Any,
    *,
    race_date: str,
    racecourse: str,
) -> int:
    """Write 加權總勝出率 from the four rate columns (ST/HV/FC)."""
    code = str(racecourse or "").strip().upper()
    if not _is_rate_write_venue(code):
        return 0
    out_col = qident(RACECARD_WEIGHTED_WIN_RATE_COLUMN)
    sd_win_col = qident(RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN)
    sd_place_col = qident(RACECARD_SAME_DISTANCE_PLACE_RATE_COLUMN)
    draw_win_col = qident(RACECARD_DRAW_WIN_RATE_COLUMN)
    draw_place_col = qident(RACECARD_DRAW_PLACE_RATE_COLUMN)
    starters = conn.execute(
        f"""
        SELECT id,
               {sd_win_col} AS same_distance_win,
               {sd_place_col} AS same_distance_place,
               {draw_win_col} AS draw_win,
               {draw_place_col} AS draw_place
          FROM racecard_starters
         WHERE race_date = ?
           AND racecourse = ?
        """,
        (race_date, racecourse),
    ).fetchall()
    updated = 0
    for row in starters:
        if hasattr(row, "keys"):
            starter_id = int(row["id"])
            sd_win = parse_pct_points_text(row["same_distance_win"])
            sd_place = parse_pct_points_text(row["same_distance_place"])
            draw_win = parse_pct_points_text(row["draw_win"])
            draw_place = parse_pct_points_text(row["draw_place"])
        else:
            starter_id = int(row[0])
            sd_win = parse_pct_points_text(row[1])
            sd_place = parse_pct_points_text(row[2])
            draw_win = parse_pct_points_text(row[3])
            draw_place = parse_pct_points_text(row[4])
        weighted = compute_weighted_win_rate_pct_points(
            same_distance_win=sd_win,
            same_distance_place=sd_place,
            draw_win=draw_win,
            draw_place=draw_place,
        )
        stored = format_weighted_win_rate_for_db(weighted)
        cur = conn.execute(
            f"""
            UPDATE racecard_starters
               SET {out_col} = ?
             WHERE id = ?
            """,
            (stored, starter_id),
        )
        updated += int(cur.rowcount or 0)
    return updated


def recompute_all_hv_weighted_win_rates(db_path: Path) -> dict[str, int]:
    """Recompute racecard_starters.加權總勝出率 for every ST/HV/FC meeting."""
    meetings = 0
    starters_updated = 0
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT race_date, racecourse
              FROM racecard_starters
             WHERE upper(trim(racecourse)) IN ('ST', 'HV', 'FC')
             ORDER BY race_date, racecourse
            """
        ).fetchall()
        for row in rows:
            race_date = str(row["race_date"] if hasattr(row, "keys") else row[0])
            racecourse = str(row["racecourse"] if hasattr(row, "keys") else row[1])
            n = update_hv_weighted_win_rate_for_meeting(
                conn, race_date=race_date, racecourse=racecourse
            )
            meetings += 1
            starters_updated += n
        conn.commit()
    return {"meetings": meetings, "starters_updated": starters_updated}


_SMOOTHED_MISSING_MULTIPLIER = 1.0


def smooth_count_rate(k: int, n: int, *, prior: float | None = None) -> float:
    """(k + prior) / (n + CONSTANT). Default prior = RATE_SMOOTH_PRIOR."""
    from hppv.racing_rate_settings import RATE_SMOOTH_CONSTANT, RATE_SMOOTH_PRIOR

    use_prior = float(RATE_SMOOTH_PRIOR) if prior is None else float(prior)
    return (float(k) + use_prior) / (float(n) + float(RATE_SMOOTH_CONSTANT))


def rate_to_field_multiplier(smooth_rate: float, baseline: float) -> float:
    """smooth_rate / baseline. baseline must be > 0."""
    return float(smooth_rate) / float(baseline)


def compute_smoothed_component_multiplier(
    *,
    k: int | None,
    n: int | None,
    baseline: float,
    prior: float | None = None,
) -> float:
    """Missing/invalid (k,n) → 1.0; else smoothed rate / baseline."""
    if k is None or n is None or n <= 0 or baseline <= 0:
        return _SMOOTHED_MISSING_MULTIPLIER
    if k < 0:
        return _SMOOTHED_MISSING_MULTIPLIER
    return rate_to_field_multiplier(
        smooth_count_rate(k, n, prior=prior), baseline
    )


def compute_attainment_field_multipliers(
    counts_by_id: dict[int, tuple[int, int] | None],
) -> dict[int, float]:
    """Per-starter 達標率 multipliers for one race.

    Prior = RATE_SMOOTH_CONSTANT × field mean raw attainment (horses with n>0).
    No horse with valid n → all multipliers 1.
    Horse without valid n → multiplier 1.
    """
    from hppv.racing_rate_settings import RATE_SMOOTH_CONSTANT

    with_data: list[tuple[int, int, int]] = []
    for starter_id, counts in counts_by_id.items():
        if counts is None:
            continue
        attained, valid = counts
        if valid <= 0:
            continue
        with_data.append((starter_id, int(attained), int(valid)))

    out: dict[int, float] = {
        starter_id: _SMOOTHED_MISSING_MULTIPLIER for starter_id in counts_by_id
    }
    if not with_data:
        return out

    raw_mean = sum(k / n for _id, k, n in with_data) / float(len(with_data))
    prior = float(RATE_SMOOTH_CONSTANT) * float(raw_mean)
    smooth_by_id: dict[int, float] = {}
    for starter_id, k, n in with_data:
        smooth_by_id[starter_id] = smooth_count_rate(k, n, prior=prior)
    baseline = sum(smooth_by_id.values()) / float(len(smooth_by_id))
    if baseline <= 0:
        return out
    for starter_id, smooth in smooth_by_id.items():
        out[starter_id] = rate_to_field_multiplier(smooth, baseline)
    return out


def compute_smoothed_composite_multiplier(
    *,
    same_distance_win_m: float,
    same_distance_place_m: float,
    draw_win_m: float,
    draw_place_m: float,
    attainment_m: float = _SMOOTHED_MISSING_MULTIPLIER,
) -> float:
    """Weighted average of five multipliers; denominator always 6+W."""
    from hppv.racing_rate_settings import (
        RATE_SMOOTHED_WEIGHT_TOTAL,
        RATE_WEIGHT_ATTAINMENT,
        RATE_WEIGHT_DRAW_PLACE,
        RATE_WEIGHT_DRAW_WIN,
        RATE_WEIGHT_SAME_DISTANCE_PLACE,
        RATE_WEIGHT_SAME_DISTANCE_WIN,
    )

    numerator = (
        same_distance_win_m * RATE_WEIGHT_SAME_DISTANCE_WIN
        + same_distance_place_m * RATE_WEIGHT_SAME_DISTANCE_PLACE
        + draw_win_m * RATE_WEIGHT_DRAW_WIN
        + draw_place_m * RATE_WEIGHT_DRAW_PLACE
        + attainment_m * RATE_WEIGHT_ATTAINMENT
    )
    return float(numerator) / float(RATE_SMOOTHED_WEIGHT_TOTAL)


def format_smoothed_weighted_rate_for_db(pct: float | None) -> str | None:
    """Store 平滑加權率 as percentage points with exactly one decimal (HALF_UP)."""
    return format_weighted_win_rate_for_db(pct)


def update_hv_smoothed_weighted_rate_for_meeting(
    conn: Any,
    *,
    race_date: str,
    racecourse: str,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> int:
    """Write 平滑加權率 per race after same-distance/draw sources are ready (ST/HV/FC).

    Field size = non-reserve starters; size <= 3 → NULL for the race.
    Missing component counts → multiplier 1 (weight unchanged).
    """
    from hppv.racing_draw_statistics import (
        lookup_draw_start_win_place_counts,
        rail_position_for_draw_lookup,
        track_type_from_race_course_area,
    )
    from hppv.racing_hkjc_graphql import venue_display_name
    from hppv.schema import RACECARD_SMOOTHED_WEIGHTED_RATE_COLUMN

    code = str(racecourse or "").strip().upper()
    if not _is_rate_write_venue(code):
        return 0
    venue_zh = venue_display_name(code)
    if not venue_zh:
        return 0
    date_iso = normalize_anchor_date_iso(race_date)
    out_col = qident(RACECARD_SMOOTHED_WEIGHTED_RATE_COLUMN)

    race_nos = [
        int(r["race_no"] if hasattr(r, "keys") else r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT race_no
              FROM racecard_starters
             WHERE race_date = ?
               AND racecourse = ?
             ORDER BY CAST(race_no AS INTEGER)
            """,
            (race_date, racecourse),
        ).fetchall()
    ]
    draw_table = conn.execute(
        """
        SELECT 1 FROM sqlite_master
         WHERE type = 'table' AND name = 'racing_draw_statistics'
         LIMIT 1
        """
    ).fetchone()

    updated = 0
    for race_no in race_nos:
        starters = conn.execute(
            """
            SELECT s.id AS starter_id,
                   s.horse_id AS horse_id,
                   s."馬號" AS horse_no,
                   s."後備" AS reserve,
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
               AND s.race_no = ?
             ORDER BY CAST(s."馬號" AS INTEGER), s.id
            """,
            (race_date, racecourse, race_no),
        ).fetchall()
        field_starters = [
            r
            for r in starters
            if str(r["reserve"] if hasattr(r, "keys") else "").strip() == "否"
        ]
        field_size = len(field_starters)
        if field_size <= 3:
            for row in starters:
                starter_id = int(row["starter_id"] if hasattr(row, "keys") else row[0])
                cur = conn.execute(
                    f"UPDATE racecard_starters SET {out_col} = NULL WHERE id = ?",
                    (starter_id,),
                )
                updated += int(cur.rowcount or 0)
            continue

        win_baseline = 1.0 / float(field_size)
        place_baseline = 3.0 / float(field_size)
        per_starter: list[
            tuple[
                int,
                int | None,
                int | None,
                int | None,
                int | None,
                int | None,
                int | None,
            ]
        ] = []
        attainment_counts: dict[int, tuple[int, int] | None] = {}
        for row in field_starters:
            starter_id = int(row["starter_id"])
            horse_id = str(row["horse_id"] or "").strip()
            distance = row["distance"]
            # Same-distance counts (shared n for win/place) + attainment on same sample
            sd_n: int | None = None
            sd_wins: int | None = None
            sd_places: int | None = None
            att_counts: tuple[int, int] | None = None
            if horse_id and is_acceptable_horse_id(horse_id) and str(distance or "").strip():
                try:
                    anchor = parse_anchor(
                        horse_id=horse_id,
                        race_date=date_iso,
                        venue=venue_zh,
                        distance=distance,
                        race_no=race_no,
                    )
                except HorseFormValidationError:
                    anchor = None
                if anchor is not None:
                    counts = compute_same_venue_distance_win_place_counts(
                        conn, anchor, limit=limit
                    )
                    if counts is not None:
                        sd_n, sd_wins, sd_places = counts
                    att_counts = compute_same_venue_distance_attainment_counts(
                        conn, anchor, limit=limit
                    )

            # Draw counts (shared n for win/place)
            draw_n: int | None = None
            draw_wins: int | None = None
            draw_places: int | None = None
            if draw_table:
                track_type = track_type_from_race_course_area(row["race_course_area"])
                dist_txt = normalize_distance(distance)
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
                    draw_counts = lookup_draw_start_win_place_counts(
                        conn,
                        venue_code=code,
                        track_type=track_type,
                        rail_position=rail,
                        distance_m=int(dist_txt),
                        draw_no=int(draw_txt),
                    )
                    if draw_counts is not None:
                        draw_n, draw_wins, draw_places = draw_counts

            attainment_counts[starter_id] = att_counts
            per_starter.append(
                (starter_id, sd_wins, sd_n, sd_places, draw_wins, draw_n, draw_places)
            )

        attainment_m_by_id = compute_attainment_field_multipliers(attainment_counts)
        composites: list[tuple[int, float]] = []
        for (
            starter_id,
            sd_wins,
            sd_n,
            sd_places,
            draw_wins,
            draw_n,
            draw_places,
        ) in per_starter:
            sd_win_m = compute_smoothed_component_multiplier(
                k=sd_wins, n=sd_n, baseline=win_baseline
            )
            sd_place_m = compute_smoothed_component_multiplier(
                k=sd_places, n=sd_n, baseline=place_baseline
            )
            draw_win_m = compute_smoothed_component_multiplier(
                k=draw_wins, n=draw_n, baseline=win_baseline
            )
            draw_place_m = compute_smoothed_component_multiplier(
                k=draw_places, n=draw_n, baseline=place_baseline
            )
            attainment_m = attainment_m_by_id.get(
                starter_id, _SMOOTHED_MISSING_MULTIPLIER
            )
            composite = compute_smoothed_composite_multiplier(
                same_distance_win_m=sd_win_m,
                same_distance_place_m=sd_place_m,
                draw_win_m=draw_win_m,
                draw_place_m=draw_place_m,
                attainment_m=attainment_m,
            )
            composites.append((starter_id, composite))

        total = sum(c for _id, c in composites)
        values_by_id: dict[int, str | None] = {}
        if total <= 0:
            for starter_id, _c in composites:
                values_by_id[starter_id] = None
        else:
            for starter_id, composite in composites:
                pct = (composite / total) * 100.0
                values_by_id[starter_id] = format_smoothed_weighted_rate_for_db(pct)

        for row in starters:
            starter_id = int(row["starter_id"])
            reserve = str(row["reserve"] or "").strip()
            stored = values_by_id.get(starter_id) if reserve == "否" else None
            cur = conn.execute(
                f"UPDATE racecard_starters SET {out_col} = ? WHERE id = ?",
                (stored, starter_id),
            )
            updated += int(cur.rowcount or 0)
    return updated


def recompute_all_hv_smoothed_weighted_rates(db_path: Path) -> dict[str, int]:
    """Recompute racecard_starters.平滑加權率 for every ST/HV/FC meeting."""
    meetings = 0
    starters_updated = 0
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT race_date, racecourse
              FROM racecard_starters
             WHERE upper(trim(racecourse)) IN ('ST', 'HV', 'FC')
             ORDER BY race_date, racecourse
            """
        ).fetchall()
        for row in rows:
            race_date = str(row["race_date"] if hasattr(row, "keys") else row[0])
            racecourse = str(row["racecourse"] if hasattr(row, "keys") else row[1])
            n = update_hv_smoothed_weighted_rate_for_meeting(
                conn, race_date=race_date, racecourse=racecourse
            )
            meetings += 1
            starters_updated += n
        conn.commit()
    return {"meetings": meetings, "starters_updated": starters_updated}


def fetch_recent_form(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    limit: int,
    recent_filter: RecentFilter,
) -> list[dict[str, str]]:
    if recent_filter == "same_venue" and not anchor.venue:
        return []
    venue = anchor.venue if recent_filter == "same_venue" else None
    distance = anchor.distance if recent_filter == "same_distance" else None
    rows = _fetch_candidates(
        conn,
        horse_id=anchor.horse_id,
        before_iso=anchor.race_date_iso,
        limit=limit,
        venue=venue,
    )
    return _take_views(rows, limit=limit, distance=distance)


def _venue_distance_win_payload(
    conn: Any,
    anchor: HorseFormAnchor,
    *,
    venue_code: str,
    limit: int,
) -> dict[str, Any]:
    """Shared win counts for one venue code + anchor distance."""
    rows = fetch_same_venue_distance_form(
        conn, anchor, limit=limit, venue=venue_code
    )
    return {"venue_code": venue_code, **summarize_win_counts(rows)}


def build_horse_form_payload(
    conn: Any,
    anchor: HorseFormAnchor,
    filters: HorseFormFilters,
) -> dict[str, Any]:
    from hppv.racing_hkjc_graphql import other_main_venue_code, venue_code_name

    # Section 1: same venue + same distance only (aligns with 同程勝率／達標率 sample).
    same_distance = fetch_same_venue_distance_form(conn, anchor, limit=filters.limit)
    # Section 2: recent form — no venue/distance filter; field-count via recent_limit.
    recent = fetch_recent_form(
        conn,
        anchor,
        limit=recent_form_fetch_limit(filters.recent_limit),
        recent_filter="all",
    )
    venue_code = venue_code_name(anchor.venue)
    same_venue_distance_win = (
        _venue_distance_win_payload(
            conn, anchor, venue_code=venue_code, limit=filters.limit
        )
        if venue_code
        else {"venue_code": "", **summarize_win_counts([])}
    )
    other_code = other_main_venue_code(venue_code) if venue_code else None
    other_venue_distance_win = (
        _venue_distance_win_payload(
            conn, anchor, venue_code=other_code, limit=filters.limit
        )
        if other_code
        else None
    )
    other_stats = fetch_other_stats_by_surface(conn, anchor)
    return {
        "cutoff": "before_race_date",
        "cutoff_label_key": "racing.horse_form.cutoff",
        "limit": filters.limit,
        "limit_options": list(HORSE_FORM_LIMIT_OPTIONS),
        "recent_limit": filters.recent_limit,
        "recent_limit_options": [RECENT_FORM_LIMIT_ALL, *RECENT_FORM_LIMIT_OPTIONS],
        "horse": {
            "horse_id": anchor.horse_id,
            "horse_name": anchor.horse_name or "",
        },
        "anchor": {
            "race_date": anchor.race_date_iso,
            "venue": anchor.venue,
            "venue_code": venue_code,
            "distance": anchor.distance,
            "race_no": anchor.race_no or "",
            "handicap": format_text_field(anchor.handicap),
        },
        "filters": {
            "recent_limit": filters.recent_limit,
        },
        "same_distance": same_distance,
        "recent": recent,
        "summary": summarize_places(recent),
        # Section 3: HV / ST(turf only) / AWT career counts before anchor.
        "other_stats": other_stats,
        # Kept for callers / tests; section 3 UI no longer shows these.
        "same_venue_distance_win": same_venue_distance_win,
        "other_venue_distance_win": other_venue_distance_win,
        "handicap_stats": summarize_handicap_groups(
            same_distance, today_handicap=anchor.handicap
        ),
        "stats_eligibility": {
            "valid": "numeric finish_pos >= 1",
            "withdrawn": ["WV", "WV-A"],
            "excluded_from_rates": ["withdrawn", "unknown"],
        },
    }


def fetch_horse_form(
    db_path: Path,
    *,
    horse_id: Any,
    race_date: Any,
    venue: Any,
    distance: Any,
    race_no: Any = None,
    horse_name: Any = None,
    handicap: Any = None,
    limit: Any = None,
    recent_limit: Any = None,
    recent_filter: Any = None,
) -> dict[str, Any]:
    del recent_filter  # UI removed venue/distance filter; always recent=all.
    anchor = parse_anchor(
        horse_id=horse_id,
        race_date=race_date,
        venue=venue,
        distance=distance,
        race_no=race_no,
        horse_name=horse_name,
        handicap=handicap,
    )
    filters = HorseFormFilters(
        limit=normalize_horse_form_limit(limit),
        recent_limit=normalize_recent_form_limit(recent_limit),
        recent_filter="all",
    )
    with get_connection(db_path) as conn:
        return build_horse_form_payload(conn, anchor, filters)
