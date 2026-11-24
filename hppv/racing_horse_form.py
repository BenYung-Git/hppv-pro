"""Racing horse form panel: same-distance / recent / place stats (read-only).

Mirrors soccer_match_form patterns for racing daily expand panels.
Decisions (temporary product defaults):
- Shared limit 5/10, default 10
- WV/WV-A occupy the N sample slots but are not valid_played
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

HORSE_FORM_LIMITS: frozenset[int] = frozenset({5, 10})
DEFAULT_HORSE_FORM_LIMIT = 10

RecentFilter = Literal["all", "same_venue", "same_distance"]
PlaceBucket = Literal[
    "first", "second", "third", "other", "withdrawn", "unknown"
]

_RE_DISTANCE_DIGITS = re.compile(r"(\d+)")
_RE_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_WV = re.compile(r"^WV(?:-A)?$", re.IGNORECASE)


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


@dataclass(frozen=True, slots=True)
class HorseFormFilters:
    limit: int = DEFAULT_HORSE_FORM_LIMIT
    recent_filter: RecentFilter = "all"


def normalize_horse_form_limit(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HORSE_FORM_LIMIT
    if value in HORSE_FORM_LIMITS:
        return value
    return DEFAULT_HORSE_FORM_LIMIT


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


def parse_anchor(
    *,
    horse_id: Any,
    race_date: Any,
    venue: Any,
    distance: Any,
    race_no: Any = None,
    horse_name: Any = None,
) -> HorseFormAnchor:
    hid = str(horse_id or "").strip()
    if not hid or not is_acceptable_horse_id(hid):
        raise HorseFormValidationError("invalid horse_id")
    date_iso = normalize_anchor_date_iso(race_date)
    dist = normalize_distance(distance)
    ven = normalize_venue(venue)
    if not dist:
        raise HorseFormValidationError("distance required")
    rn = str(race_no or "").strip() or None
    name = str(horse_name or "").strip() or None
    return HorseFormAnchor(
        horse_id=hid,
        race_date_iso=date_iso,
        venue=ven,
        distance=dist,
        race_no=rn,
        horse_name=name,
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
        if n >= 4:
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
    first = second = third = other = withdrawn = unknown = 0
    for row in rows:
        bucket = row.get("place_bucket") or classify_finish_pos(row.get("finish_pos"))
        if bucket == "first":
            first += 1
        elif bucket == "second":
            second += 1
        elif bucket == "third":
            third += 1
        elif bucket == "other":
            other += 1
        elif bucket == "withdrawn":
            withdrawn += 1
        else:
            unknown += 1
    valid = first + second + third + other
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
        "other": other,
        "withdrawn": withdrawn,
        "unknown": unknown,
        "win_rate": win_rate,
        "place_rate": place_rate,
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


def build_horse_form_payload(
    conn: Any,
    anchor: HorseFormAnchor,
    filters: HorseFormFilters,
) -> dict[str, Any]:
    same_distance = fetch_same_distance_form(conn, anchor, limit=filters.limit)
    recent = fetch_recent_form(
        conn,
        anchor,
        limit=filters.limit,
        recent_filter=filters.recent_filter,
    )
    return {
        "cutoff": "before_race_date",
        "cutoff_label_key": "racing.horse_form.cutoff",
        "limit": filters.limit,
        "horse": {
            "horse_id": anchor.horse_id,
            "horse_name": anchor.horse_name or "",
        },
        "anchor": {
            "race_date": anchor.race_date_iso,
            "venue": anchor.venue,
            "distance": anchor.distance,
            "race_no": anchor.race_no or "",
        },
        "filters": {
            "recent_filter": filters.recent_filter,
        },
        "same_distance": same_distance,
        "recent": recent,
        "summary": summarize_places(recent),
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
    )
    filters = HorseFormFilters(
        limit=normalize_horse_form_limit(limit),
        recent_filter=normalize_recent_filter(recent_filter),
    )
    with get_connection(db_path) as conn:
        return build_horse_form_payload(conn, anchor, filters)
