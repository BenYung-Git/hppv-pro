"""Racing horse form panel: same-distance / recent / place stats (read-only).

Mirrors soccer_match_form patterns for racing daily expand panels.
Shared sampling (HORSE_FORM_LIMIT / HORSE_FORM_LIMIT_OPTIONS):
- Newest-first before anchor; take at most that many rows
- WV/WV-A occupy sample slots but are not valid_played
- Empty cells use —
- Cutoff: race date strictly before anchor ISO date
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from hppv.db import _RACE_RECORD_DATE_ORDER_EXPR, get_connection, is_acceptable_horse_id
from hppv.display_format import EMPTY_DISPLAY, format_date_display, format_text_field

# Shared sampling settings (single source; UI/API/filters must use these).
HORSE_FORM_LIMIT = 12
HORSE_FORM_LIMIT_OPTIONS: tuple[int, ...] = (HORSE_FORM_LIMIT,)
HORSE_FORM_LIMITS: frozenset[int] = frozenset(HORSE_FORM_LIMIT_OPTIONS)
DEFAULT_HORSE_FORM_LIMIT = HORSE_FORM_LIMIT

RecentFilter = Literal["all", "same_venue", "same_distance"]
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


def normalize_recent_filter(raw: Any) -> RecentFilter:
    text = str(raw or "all").strip().lower()
    if text in ("all", "same_venue", "same_distance"):
        return text  # type: ignore[return-value]
    # Accept short aliases from UI.
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
        "draw": format_text_field(row["draw"] if "draw" in keys else None),
        "handicap": format_text_field(row["handicap"] if "handicap" in keys else None),
        "jockey": format_text_field(row["jockey"] if "jockey" in keys else None),
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
    """Split the same recent rows by historical handicap vs today's pounds (non-exclusive)."""
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
          rr.draw,
          rr.handicap,
          rr.jockey,
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


# Only HV meetings write racecard_starters.同程勝率 (ST stays display-only in horse form).
_SAME_DISTANCE_WIN_RATE_WRITE_VENUE_CODE = "HV"


def update_hv_same_distance_win_rates_for_meeting(
    conn: Any,
    *,
    race_date: str,
    racecourse: str,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> int:
    """After racecard upsert: write HV same-venue+distance win rates; skip non-HV."""
    from hppv.racing_hkjc_graphql import venue_display_name
    from hppv.schema import RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN, qident

    code = str(racecourse or "").strip().upper()
    if code != _SAME_DISTANCE_WIN_RATE_WRITE_VENUE_CODE:
        return 0
    venue_zh = venue_display_name(code)
    if not venue_zh:
        return 0
    date_iso = normalize_anchor_date_iso(race_date)
    col = qident(RACECARD_SAME_DISTANCE_WIN_RATE_COLUMN)
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
        pct = compute_same_venue_distance_win_rate_pct_points(
            conn, anchor, limit=limit
        )
        db_val = format_pct_points_for_db(pct)
        cur = conn.execute(
            f"""
            UPDATE racecard_starters
               SET {col} = ?
             WHERE id = ?;
            """,
            (db_val, starter_id),
        )
        updated += int(cur.rowcount or 0)
    return updated


def recompute_all_hv_same_distance_win_rates(
    db_path: Path,
    *,
    limit: int = DEFAULT_HORSE_FORM_LIMIT,
) -> dict[str, int]:
    """Recompute racecard_starters.同程勝率 for every HV meeting in the DB."""
    meetings = 0
    starters_updated = 0
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT race_date, racecourse
              FROM racecard_starters
             WHERE upper(trim(racecourse)) = 'HV'
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

    same_distance = fetch_same_distance_form(conn, anchor, limit=filters.limit)
    recent = fetch_recent_form(
        conn,
        anchor,
        limit=filters.limit,
        recent_filter=filters.recent_filter,
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
    return {
        "cutoff": "before_race_date",
        "cutoff_label_key": "racing.horse_form.cutoff",
        "limit": filters.limit,
        "limit_options": list(HORSE_FORM_LIMIT_OPTIONS),
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
            "recent_filter": filters.recent_filter,
        },
        "same_distance": same_distance,
        "recent": recent,
        "summary": summarize_places(recent),
        # Top: today venue + distance; bottom: other main venue (ST↔HV) + distance.
        "same_venue_distance_win": same_venue_distance_win,
        "other_venue_distance_win": other_venue_distance_win,
        "handicap_stats": summarize_handicap_groups(
            recent, today_handicap=anchor.handicap
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
    recent_filter: Any = None,
) -> dict[str, Any]:
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
        recent_filter=normalize_recent_filter(recent_filter),
    )
    with get_connection(db_path) as conn:
        return build_horse_form_payload(conn, anchor, filters)
