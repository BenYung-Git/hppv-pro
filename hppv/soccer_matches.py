from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hppv.schema import SOCCER_DAILY_TABLE
from hppv.soccer_db import get_connection, report_schema_diff, table_exists

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMPTY_DISPLAY = "—"

# Status Filter is part of the list filter contract, but source→bucket mapping
# is not verified yet. Keep False so we never invent match_status SQL.
STATUS_FILTER_MAPPING_CONNECTED = False

STATUS_FILTER_KEYS = frozenset({"results", "prematch", "inplay", "void"})
DEFAULT_STATUS_FILTER = "prematch"

_SORT_SQL: dict[str, str] = {
    "match_date": "match_date",
    "kickoff_hhmm": "kickoff_at",
    "match_code": "match_code",
    "competition_code": "competition_code",
    "competition_name_zh": "competition_name_zh",
    "home_team": "home_team",
    "away_team": "away_team",
    "home_odds": "home_odds",
    "draw_odds": "draw_odds",
    "away_odds": "away_odds",
    "score_ht": "score_ht_home",
    "score_2h": "score_2h_home",
    "score_ft": "score_ft_home",
    "score_pen": "score_pen_home",
    "result_status": "result_status",
}

_SEARCH_SQL: dict[str, str] = {
    "match_date": "lower(ifnull(match_date,''))",
    "kickoff_hhmm": "lower(substr(ifnull(kickoff_at,''), 12, 5))",
    "match_code": "lower(ifnull(match_code,''))",
    "competition_code": "lower(ifnull(competition_code,''))",
    "competition_name_zh": "lower(ifnull(competition_name_zh,''))",
    "home_team": "lower(ifnull(home_team,''))",
    "away_team": "lower(ifnull(away_team,''))",
    "home_odds": (
        "CASE WHEN home_odds IS NULL OR home_odds <= 0 THEN NULL "
        "ELSE lower(printf('%.2f', home_odds)) END"
    ),
    "draw_odds": (
        "CASE WHEN draw_odds IS NULL OR draw_odds <= 0 THEN NULL "
        "ELSE lower(printf('%.2f', draw_odds)) END"
    ),
    "away_odds": (
        "CASE WHEN away_odds IS NULL OR away_odds <= 0 THEN NULL "
        "ELSE lower(printf('%.2f', away_odds)) END"
    ),
    "score_ht": (
        "CASE WHEN score_ht_home IS NULL OR score_ht_away IS NULL THEN NULL "
        "ELSE lower(score_ht_home || ' : ' || score_ht_away) END"
    ),
    "score_2h": (
        "CASE WHEN score_2h_home IS NULL OR score_2h_away IS NULL THEN NULL "
        "ELSE lower(score_2h_home || ' : ' || score_2h_away) END"
    ),
    "score_ft": (
        "CASE WHEN score_ft_home IS NULL OR score_ft_away IS NULL THEN NULL "
        "ELSE lower(score_ft_home || ' : ' || score_ft_away) END"
    ),
    "score_pen": (
        "CASE WHEN score_pen_home IS NULL OR score_pen_away IS NULL THEN NULL "
        "ELSE lower(score_pen_home || ' : ' || score_pen_away) END"
    ),
    "result_status": "lower(ifnull(result_status,''))",
}

_MATCH_SELECT_SQL = """
                SELECT
                    match_id,
                    match_code,
                    match_date,
                    kickoff_at,
                    competition_code,
                    competition_name_zh,
                    home_team,
                    away_team,
                    home_odds,
                    draw_odds,
                    away_odds,
                    score_ht_home,
                    score_ht_away,
                    score_2h_home,
                    score_2h_away,
                    score_ft_home,
                    score_ft_away,
                    score_pen_home,
                    score_pen_away,
                    result_status
                FROM soccer_daily
"""


@dataclass(frozen=True, slots=True)
class MatchListResult:
    """Full filtered list + 1-based record cursor n (N / total_count)."""

    matches: list[dict[str, str]]
    n: int
    total_count: int
    q: str
    status: str
    status_filter_applied: bool
    all_matches: bool = False


def parse_all_matches_flag(raw: Any) -> bool:
    """
    All-matches mode (no date filter).
    Explicit false: 0 / false / no / off (case-insensitive).
    Explicit true: 1 / true / yes / on.
    Missing/blank defaults to True (initial UI default).
    """
    if raw is None:
        return True
    text = str(raw).strip().lower()
    if text == "":
        return True
    if text in ("0", "false", "no", "off"):
        return False
    if text in ("1", "true", "yes", "on"):
        return True
    return True


def resolve_all_matches_arg(args: Any) -> bool:
    """
    Read all_matches from a Werkzeug/Flask args mapping.
    If the key is present (even empty), parse it; only default True when absent.
    Also tolerates accidental 'amp;all_matches' from undecoded &amp; in links.
    """
    if hasattr(args, "get"):
        if "all_matches" in args:
            return parse_all_matches_flag(args.get("all_matches"))
        # Undecoded HTML entity link: ?date=X&amp;all_matches=0
        if "amp;all_matches" in args:
            return parse_all_matches_flag(args.get("amp;all_matches"))
    return True


class SoccerDataError(RuntimeError):
    """Raised when soccer match data cannot be read (not the same as zero rows)."""


def hong_kong_today() -> date:
    """Calendar date in Asia/Hong_Kong (not server local / visitor TZ)."""
    return datetime.now(HKT).date()


def shift_date(d: date, days: int) -> date:
    return d + timedelta(days=days)


def parse_requested_date(raw: str | None) -> tuple[date | None, bool]:
    """
    Parse YYYY-MM-DD from query string.

    Returns (date, invalid).
    - raw empty/None -> (hong_kong_today(), False)
    - valid date -> (date, False)
    - invalid -> (None, True)  # do not silently fall back
    """
    if raw is None or str(raw).strip() == "":
        return hong_kong_today(), False
    text = str(raw).strip()
    if not _DATE_RE.match(text):
        return None, True
    try:
        return date.fromisoformat(text), False
    except ValueError:
        return None, True


def format_kickoff_hhmm(kickoff_at: str | None) -> str:
    """Convert kickoff_at ISO timestamp to HKT HH:mm."""
    if kickoff_at is None or str(kickoff_at).strip() == "":
        return EMPTY_DISPLAY
    text = str(kickoff_at).strip()
    try:
        # Support trailing Z
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            # Spec stores kickoff with +08:00; naive values treated as HKT wall time.
            dt = dt.replace(tzinfo=HKT)
        local = dt.astimezone(HKT)
        return local.strftime("%H:%M")
    except (ValueError, TypeError, OSError) as e:
        logger.warning("Invalid kickoff_at %r: %s", kickoff_at, e)
        return EMPTY_DISPLAY


def format_text_field(value: Any) -> str:
    if value is None:
        return EMPTY_DISPLAY
    text = str(value).strip()
    return text if text else EMPTY_DISPLAY


def format_odds(value: Any, *, match_id: str = "", field: str = "") -> str:
    """
    Display odds with 2 decimal places.
    NULL -> —; 0 / negative / unparseable -> — + warning (do not coerce to 0).
    """
    if value is None:
        return EMPTY_DISPLAY
    try:
        num = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Unparseable odds match_id=%s field=%s value=%r",
            match_id,
            field,
            value,
        )
        return EMPTY_DISPLAY
    if num <= 0:
        logger.warning(
            "Abnormal odds match_id=%s field=%s value=%r",
            match_id,
            field,
            value,
        )
        return EMPTY_DISPLAY
    return f"{num:.2f}"


def _parse_non_negative_int(value: Any) -> int | None:
    """
    Accept only non-negative integers (incl. 0).
    Reject bool, non-integer float, decimal strings, and other invalid values.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        n = int(value)
        return n if n >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d+", text):
            return None
        return int(text)
    return None


def format_score_pair(home: Any, away: Any, *, match_id: str = "", field: str = "") -> str:
    """
    Format home/away goals. Both NULL -> —. Real 0:0 kept as "0 : 0".
    Incomplete pair (only one side) -> — (do not invent the other side).
    Negatives / decimals / invalid strings -> — + warning (never coerce).
    """
    if home is None and away is None:
        return EMPTY_DISPLAY
    if home is None or away is None:
        logger.warning(
            "Incomplete score pair match_id=%s field=%s home=%r away=%r",
            match_id,
            field,
            home,
            away,
        )
        return EMPTY_DISPLAY
    h = _parse_non_negative_int(home)
    a = _parse_non_negative_int(away)
    if h is None or a is None:
        logger.warning(
            "Invalid score pair match_id=%s field=%s home=%r away=%r",
            match_id,
            field,
            home,
            away,
        )
        return EMPTY_DISPLAY
    return f"{h} : {a}"


def _row_to_view(row: Any) -> dict[str, str]:
    match_id = str(row["match_id"] or "")
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    def _get(name: str) -> Any:
        return row[name] if name in keys else None

    return {
        "match_id": match_id,
        "match_date": format_text_field(_get("match_date")),
        "kickoff_hhmm": format_kickoff_hhmm(row["kickoff_at"]),
        "match_code": format_text_field(row["match_code"]),
        "competition_code": format_text_field(row["competition_code"]),
        "competition_name_zh": format_text_field(row["competition_name_zh"]),
        "home_team": format_text_field(row["home_team"]),
        "away_team": format_text_field(row["away_team"]),
        "home_odds": format_odds(row["home_odds"], match_id=match_id, field="home_odds"),
        "draw_odds": format_odds(row["draw_odds"], match_id=match_id, field="draw_odds"),
        "away_odds": format_odds(row["away_odds"], match_id=match_id, field="away_odds"),
        "score_ht": format_score_pair(
            _get("score_ht_home"), _get("score_ht_away"), match_id=match_id, field="score_ht"
        ),
        "score_2h": format_score_pair(
            _get("score_2h_home"), _get("score_2h_away"), match_id=match_id, field="score_2h"
        ),
        "score_ft": format_score_pair(
            _get("score_ft_home"), _get("score_ft_away"), match_id=match_id, field="score_ft"
        ),
        "score_pen": format_score_pair(
            _get("score_pen_home"), _get("score_pen_away"), match_id=match_id, field="score_pen"
        ),
        "result_status": format_text_field(_get("result_status")),
    }


def normalize_record_index(raw: Any, *, total_count: int) -> int:
    """
    1-based record cursor for N / total_count.
    Missing/non-numeric/<1 → 1; above total → total; empty set → 1 (UI shows 0 / 0).
    """
    n = 1
    if raw is not None and str(raw).strip() != "":
        text = str(raw).strip()
        if re.fullmatch(r"-?\d+", text):
            n = int(text)
    if n < 1:
        n = 1
    if total_count <= 0:
        return 1
    if n > total_count:
        return total_count
    return n


def normalize_status_filter(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in STATUS_FILTER_KEYS:
        return text
    return DEFAULT_STATUS_FILTER


def match_status_values_for_filter(status_key: str) -> list[str] | None:
    """
    Return source match_status values for a Status Filter key.
    None = mapping not connected; caller must not invent a WHERE clause.
    """
    if not STATUS_FILTER_MAPPING_CONNECTED:
        return None
    # Placeholder for verified mapping only — currently unreachable while flag is False.
    _ = status_key
    return None


def sort_specs_from_layout(layout_settings: dict[str, Any] | None) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    if not isinstance(layout_settings, dict):
        return specs
    cols = layout_settings.get("columns")
    if not isinstance(cols, list):
        return specs
    for item in cols:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        sort = item.get("sort")
        if key not in _SORT_SQL or sort not in ("asc", "desc"):
            continue
        specs.append((key, sort))
    return specs


def search_keys_from_layout(layout_settings: dict[str, Any] | None) -> list[str]:
    keys: list[str] = []
    if not isinstance(layout_settings, dict):
        return keys
    cols = layout_settings.get("columns")
    if not isinstance(cols, list):
        return keys
    for item in cols:
        if not isinstance(item, dict) or not item.get("visible"):
            continue
        key = str(item.get("key") or "")
        if key in ("icon", "ui_et") or key not in _SEARCH_SQL:
            continue
        keys.append(key)
    return keys


def parse_sort_param(raw: str | None) -> list[tuple[str, str]] | None:
    if raw is None or str(raw).strip() == "":
        return None
    specs: list[tuple[str, str]] = []
    for part in str(raw).split(","):
        token = part.strip()
        if not token or ":" not in token:
            continue
        key, _, direction = token.partition(":")
        key = key.strip()
        direction = direction.strip().lower()
        if key not in _SORT_SQL or direction not in ("asc", "desc"):
            continue
        specs.append((key, direction))
    return specs


def _build_order_clause(sort_specs: list[tuple[str, str]] | None) -> str:
    parts: list[str] = []
    if sort_specs:
        for key, direction in sort_specs:
            expr = _SORT_SQL.get(key)
            if not expr:
                continue
            parts.append(f"{expr} {'ASC' if direction == 'asc' else 'DESC'}")
    if not parts:
        parts.append("kickoff_at ASC")
    if not any(p.startswith("match_id ") for p in parts):
        parts.append("match_id ASC")
    return ", ".join(parts)


def _build_where_and_params(
    *,
    match_date: date | None,
    q: str,
    status: str,
    search_keys: list[str] | None,
) -> tuple[str, list[Any], bool]:
    """
    Shared WHERE for filtered SELECT on soccer_daily only.
    Filter = optional date + search + status (status SQL only when mapping connected).
    match_date=None means all soccer_daily rows (no date clause).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if match_date is not None:
        clauses.append("match_date = ?")
        params.append(match_date.isoformat())
    status_applied = False

    status_key = normalize_status_filter(status)
    status_values = match_status_values_for_filter(status_key)
    if status_values is not None:
        if not status_values:
            clauses.append("1 = 0")
        else:
            placeholders = ", ".join("?" for _ in status_values)
            clauses.append(f"match_status IN ({placeholders})")
            params.extend(status_values)
        status_applied = True

    needle = (q or "").strip().lower()
    if needle:
        keys = [k for k in (search_keys or []) if k in _SEARCH_SQL]
        if not keys:
            clauses.append("1 = 0")
        else:
            ors = [f"instr({_SEARCH_SQL[k]}, ?) > 0" for k in keys]
            clauses.append("(" + " OR ".join(ors) + ")")
            params.extend([needle] * len(keys))

    if not clauses:
        return "1 = 1", params, status_applied
    return " AND ".join(clauses), params, status_applied


def fetch_matches_filtered(
    db_path: Path,
    match_date: date | None,
    *,
    n: Any = 1,
    q: str = "",
    status: str = DEFAULT_STATUS_FILTER,
    sort_specs: list[tuple[str, str]] | None = None,
    search_keys: list[str] | None = None,
) -> MatchListResult:
    """
    Return ALL soccer_daily matches for the current filter.
    match_date=None skips date filter (all-matches mode). Never reads footylogic_*.
    No per-page LIMIT. n is the 1-based cursor for UI "N / total_count".
    """
    if not db_path.is_file():
        raise SoccerDataError(f"soccer database file not found: {db_path}")

    q_text = (q or "").strip()
    status_text = normalize_status_filter(status)
    all_mode = match_date is None
    where_sql, where_params, status_applied = _build_where_and_params(
        match_date=match_date,
        q=q_text,
        status=status_text,
        search_keys=search_keys,
    )
    order_sql = _build_order_clause(sort_specs)

    try:
        with get_connection(db_path) as conn:
            if not table_exists(conn, SOCCER_DAILY_TABLE):
                raise SoccerDataError(f"table missing: {SOCCER_DAILY_TABLE}")
            diff = report_schema_diff(conn, SOCCER_DAILY_TABLE, allow_extra=True)
            if diff is not None:
                raise SoccerDataError(diff)
            rows = conn.execute(
                f"""
                {_MATCH_SELECT_SQL}
                WHERE {where_sql}
                ORDER BY {order_sql};
                """,
                where_params,
            ).fetchall()
    except SoccerDataError:
        raise
    except Exception as e:
        label = "all_matches" if all_mode else f"match_date={match_date.isoformat()}"
        raise SoccerDataError(f"filtered query failed for {label}: {e}") from e

    matches = [_row_to_view(r) for r in rows]
    total_count = len(matches)
    return MatchListResult(
        matches=matches,
        n=normalize_record_index(n, total_count=total_count),
        total_count=total_count,
        q=q_text,
        status=status_text,
        status_filter_applied=status_applied,
        all_matches=all_mode,
    )


def fetch_matches_by_date(db_path: Path, match_date: date) -> list[dict[str, str]]:
    """
    Read-only: all matches for match_date (YYYY-MM-DD), ordered by kickoff_at, match_id.

    Raises SoccerDataError on missing DB/table/schema/query failure.
    Empty list means zero stored rows for that date (not a read failure).
    """
    if not db_path.is_file():
        raise SoccerDataError(f"soccer database file not found: {db_path}")

    date_text = match_date.isoformat()
    try:
        with get_connection(db_path) as conn:
            if not table_exists(conn, SOCCER_DAILY_TABLE):
                raise SoccerDataError(f"table missing: {SOCCER_DAILY_TABLE}")
            diff = report_schema_diff(conn, SOCCER_DAILY_TABLE, allow_extra=True)
            if diff is not None:
                raise SoccerDataError(diff)
            rows = conn.execute(
                f"""
                {_MATCH_SELECT_SQL}
                WHERE match_date = ?
                ORDER BY kickoff_at ASC, match_id ASC;
                """,
                (date_text,),
            ).fetchall()
    except SoccerDataError:
        raise
    except Exception as e:
        raise SoccerDataError(f"query failed for match_date={date_text}: {e}") from e

    return [_row_to_view(r) for r in rows]

