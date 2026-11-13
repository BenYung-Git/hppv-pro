"""Read-only Footylogic match-history list (footylogic_matches only)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE
from hppv.soccer_db import get_connection, table_exists
from hppv.soccer_ingest import HKJC_RESULT_CONFIRM_STATUS, HKJC_RESULT_VOID_STATUS
from hppv.soccer_matches import (
    EMPTY_DISPLAY,
    SoccerDataError,
    format_kickoff_hhmm,
    format_odds,
    format_score_pair,
    format_text_field,
    normalize_record_index,
    parse_all_matches_flag,
    parse_requested_date,
    resolve_all_matches_arg,
)

# History HT/FT display for official MATCHVOIDED (result_confirm_status marker).
HISTORY_VOID_SCORE_DISPLAY = "失效"

# Presentation-only FT outcome classes for history team-name colouring.
HISTORY_TEAM_RESULT_WIN = "win"
HISTORY_TEAM_RESULT_DRAW = "draw"
HISTORY_TEAM_RESULT_LOSE = "lose"
HISTORY_TEAM_RESULT_NEUTRAL = "neutral"

logger = logging.getLogger(__name__)

# Daily matches currently load the full filtered set (cursor N/total).
# History has ~90k rows — must page on the server. 50 matches daily's
# practical viewport density without inventing a daily PAGE_SIZE constant.
HISTORY_PAGE_SIZE = 50

# Only columns with a defined backend order. Composite scores are not sortable.
_HISTORY_SORT_SQL: dict[str, str] = {
    "match_date": "match_date_hkt",
    "kickoff_hhmm": "kickoff_hkt",
    "competition_id": "competition_id",
    "competition_name": "competition_name",
    "home_team": "home_team_name",
    "away_team": "away_team_name",
    "source_key": "source_key",
}

# Raw team text (name preferred, else id). Used by case-sensitive operators.
_HISTORY_TEAM_TEXT_SQL: dict[str, str] = {
    "home_team": "ifnull(home_team_name, ifnull(home_team_id, ''))",
    "away_team": "ifnull(away_team_name, ifnull(away_team_id, ''))",
}

# Legacy multi-key map kept for callers that still pass search_keys explicitly.
_HISTORY_SEARCH_SQL: dict[str, str] = {
    "home_team": f"lower({_HISTORY_TEAM_TEXT_SQL['home_team']})",
    "away_team": f"lower({_HISTORY_TEAM_TEXT_SQL['away_team']})",
    "competition_name": "lower(ifnull(competition_name, ''))",
    "competition_id": "lower(ifnull(competition_id, ''))",
    "home_team_id": "lower(ifnull(home_team_id, ''))",
    "away_team_id": "lower(ifnull(away_team_id, ''))",
    "source_key": "lower(ifnull(source_key, ''))",
}

# History Search compares home + away only (OR).
DEFAULT_HISTORY_SEARCH_KEYS: tuple[str, ...] = (
    "home_team",
    "away_team",
)

# Visible search operators (UI). Codes are stable API values.
HISTORY_SEARCH_OP_APPROX = "approx"
HISTORY_SEARCH_OP_EQ = "eq"
HISTORY_SEARCH_OP_NE = "ne"
HISTORY_SEARCH_OP_CONTAINS = "contains"
HISTORY_SEARCH_OP_NOT_CONTAINS = "not_contains"
HISTORY_SEARCH_OP_STARTS = "starts"
HISTORY_SEARCH_OP_ENDS = "ends"
# Hidden but retained (not deleted; UI must not render yet).
HISTORY_SEARCH_OP_GT = "gt"
HISTORY_SEARCH_OP_GTE = "gte"
HISTORY_SEARCH_OP_LT = "lt"
HISTORY_SEARCH_OP_LTE = "lte"
HISTORY_SEARCH_OP_BETWEEN = "between"
HISTORY_SEARCH_OP_EMPTY = "empty"
HISTORY_SEARCH_OP_NOT_EMPTY = "not_empty"

HISTORY_SEARCH_OPS_VISIBLE: tuple[str, ...] = (
    HISTORY_SEARCH_OP_APPROX,
    HISTORY_SEARCH_OP_EQ,
    HISTORY_SEARCH_OP_NE,
    HISTORY_SEARCH_OP_CONTAINS,
    HISTORY_SEARCH_OP_NOT_CONTAINS,
    HISTORY_SEARCH_OP_STARTS,
    HISTORY_SEARCH_OP_ENDS,
)
HISTORY_SEARCH_OPS_HIDDEN: tuple[str, ...] = (
    HISTORY_SEARCH_OP_GT,
    HISTORY_SEARCH_OP_GTE,
    HISTORY_SEARCH_OP_LT,
    HISTORY_SEARCH_OP_LTE,
    HISTORY_SEARCH_OP_BETWEEN,
    HISTORY_SEARCH_OP_EMPTY,
    HISTORY_SEARCH_OP_NOT_EMPTY,
)
HISTORY_SEARCH_OPS_ALL: frozenset[str] = frozenset(
    HISTORY_SEARCH_OPS_VISIBLE + HISTORY_SEARCH_OPS_HIDDEN
)
DEFAULT_HISTORY_SEARCH_OP = HISTORY_SEARCH_OP_APPROX

# SELECT * so newly migrated footylogic_matches columns flow to the API
# without editing this module's column list again.
_HISTORY_SELECT_SQL = f"SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE}"

# Physical columns that remain aliased into display keys (not duplicated as raw keys
# in the primary view payload — raw values still available under physical names).
_HISTORY_ODDS_COLUMNS = frozenset(
    {"final_odds_home", "final_odds_draw", "final_odds_away"}
)


@dataclass(frozen=True, slots=True)
class HistoryListResult:
    """One page of footylogic_matches + page cursor meta."""

    matches: list[dict[str, str]]
    n: int
    total_count: int
    total_pages: int
    page_size: int
    q: str
    all_matches: bool
    sort: str
    q_op: str = DEFAULT_HISTORY_SEARCH_OP
    field_keys: tuple[str, ...] = ()


def normalize_history_search_op(raw: Any) -> str:
    """Return a known q_op; blank/unknown → approx (default)."""
    text = str(raw or "").strip().lower()
    if text in HISTORY_SEARCH_OPS_ALL:
        return text
    return DEFAULT_HISTORY_SEARCH_OP


def format_name_or_id(name: Any, source_id: Any) -> str:
    """Prefer verified name; fall back to source id; never invent labels."""
    text = format_text_field(name)
    if text != EMPTY_DISPLAY:
        return text
    return format_text_field(source_id)


def _format_history_physical_value(col: str, value: Any, *, source_key: str) -> str:
    if col in _HISTORY_ODDS_COLUMNS:
        return format_odds(value, match_id=source_key, field=col)
    # INTEGER/REAL/TEXT: NULL → —; real 0 kept as "0" (never invent missing as 0).
    return format_text_field(value)


def _parse_valid_ft_goal(raw: Any) -> int | None:
    """Accept a real non-negative int goal count only. Never coerce NULL/blank to 0."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw).strip()
    if text == "" or text == EMPTY_DISPLAY:
        return None
    if text.isdigit():
        return int(text)
    return None


def history_team_result_classes(
    score_ft_home: Any,
    score_ft_away: Any,
    *,
    result_confirm_status: Any = None,
) -> tuple[str, str]:
    """
    Presentation-only FT outcome for history team-name colours.

    Returns (home_class, away_class) in {win, draw, lose, neutral}.
    Void / unconfirmed / missing FT → both neutral. Never uses HT.
    """
    confirm = str(result_confirm_status or "").strip()
    if confirm == HKJC_RESULT_VOID_STATUS:
        return HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL
    if confirm != HKJC_RESULT_CONFIRM_STATUS:
        return HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL
    home = _parse_valid_ft_goal(score_ft_home)
    away = _parse_valid_ft_goal(score_ft_away)
    if home is None or away is None:
        return HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL
    if home > away:
        return HISTORY_TEAM_RESULT_WIN, HISTORY_TEAM_RESULT_LOSE
    if home < away:
        return HISTORY_TEAM_RESULT_LOSE, HISTORY_TEAM_RESULT_WIN
    return HISTORY_TEAM_RESULT_DRAW, HISTORY_TEAM_RESULT_DRAW


def _row_to_history_view(row: Any) -> dict[str, str]:
    keys = list(row.keys()) if hasattr(row, "keys") else []
    source_key = str(row["source_key"] or "") if "source_key" in keys else ""
    confirm = (
        str(row["result_confirm_status"] or "")
        if "result_confirm_status" in keys
        else ""
    )
    ft_home_raw = row["score_ft_home"] if "score_ft_home" in keys else None
    ft_away_raw = row["score_ft_away"] if "score_ft_away" in keys else None
    is_void = confirm == HKJC_RESULT_VOID_STATUS
    if is_void:
        score_ht = HISTORY_VOID_SCORE_DISPLAY
        score_ft = HISTORY_VOID_SCORE_DISPLAY
    else:
        score_ht = format_score_pair(
            row["score_ht_home"] if "score_ht_home" in keys else None,
            row["score_ht_away"] if "score_ht_away" in keys else None,
            match_id=source_key,
            field="score_ht",
        )
        score_ft = format_score_pair(
            ft_home_raw,
            ft_away_raw,
            match_id=source_key,
            field="score_ft",
        )
    home_result, away_result = history_team_result_classes(
        ft_home_raw,
        ft_away_raw,
        result_confirm_status=confirm,
    )
    view: dict[str, str] = {
        "source_key": source_key or EMPTY_DISPLAY,
        "match_date": format_text_field(row["match_date_hkt"] if "match_date_hkt" in keys else None),
        "kickoff_hhmm": format_kickoff_hhmm(row["kickoff_hkt"] if "kickoff_hkt" in keys else None),
        "competition_id": format_text_field(
            row["competition_id"] if "competition_id" in keys else None
        ),
        "competition_name": format_name_or_id(
            row["competition_name"] if "competition_name" in keys else None,
            row["competition_id"] if "competition_id" in keys else None,
        ),
        "home_team": format_name_or_id(
            row["home_team_name"] if "home_team_name" in keys else None,
            row["home_team_id"] if "home_team_id" in keys else None,
        ),
        "away_team": format_name_or_id(
            row["away_team_name"] if "away_team_name" in keys else None,
            row["away_team_id"] if "away_team_id" in keys else None,
        ),
        "score_ht": score_ht,
        "score_ft": score_ft,
        # UI-only; not layout/export columns.
        "home_team_result": home_result,
        "away_team_result": away_result,
    }
    # Expose every physical column so layout/API stay schema-driven.
    for col in keys:
        if col in view:
            continue
        view[col] = _format_history_physical_value(col, row[col], source_key=source_key)
    return view


def parse_history_sort_param(raw: str | None) -> list[tuple[str, str]] | None:
    """
    Parse sort=key:dir,key:dir.
    None/blank → None (caller may fall back to layout defaults).
    """
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
        if key not in _HISTORY_SORT_SQL or direction not in ("asc", "desc"):
            continue
        specs.append((key, direction))
    return specs or None


def default_history_sort_specs() -> list[tuple[str, str]]:
    return [("kickoff_hhmm", "desc")]


def history_sort_specs_from_layout(layout_settings: dict[str, Any] | None) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    if not isinstance(layout_settings, dict):
        return default_history_sort_specs()
    cols = layout_settings.get("columns")
    if not isinstance(cols, list):
        return default_history_sort_specs()
    for item in cols:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        sort = item.get("sort")
        if key not in _HISTORY_SORT_SQL or sort not in ("asc", "desc"):
            continue
        specs.append((key, sort))
    return specs or default_history_sort_specs()


def history_sort_param(specs: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}:{d}" for k, d in specs)


def _build_order_clause(sort_specs: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for key, direction in sort_specs:
        expr = _HISTORY_SORT_SQL.get(key)
        if not expr:
            continue
        parts.append(f"{expr} {'ASC' if direction == 'asc' else 'DESC'}")
    if not parts:
        parts.append("kickoff_hkt DESC")
    # Stable unique key (never invent match numbers).
    if not any("source_key" in p for p in parts):
        parts.append("source_key ASC")
    return ", ".join(parts)


def _team_text_keys(search_keys: list[str] | None) -> list[str]:
    keys = [k for k in (search_keys or list(DEFAULT_HISTORY_SEARCH_KEYS)) if k in _HISTORY_TEAM_TEXT_SQL]
    return keys or list(DEFAULT_HISTORY_SEARCH_KEYS)


def _or_team_clause(expr_for_key: Any, keys: list[str]) -> str:
    return "(" + " OR ".join(expr_for_key(k) for k in keys) + ")"


def _build_q_clause(
    *,
    q: str,
    q_op: str,
    search_keys: list[str] | None,
) -> tuple[str | None, list[Any]]:
    """
    Build text filter on home/away team fields.

    Positive ops: home OR away matches → include.
    Negative ops (ne / not_contains): NOT (home OR away match) → neither matches.
    approx: legacy lower+instr (case-insensitive). Other visible ops: case-sensitive.
    """
    op = normalize_history_search_op(q_op)
    keys = _team_text_keys(search_keys)
    raw_needle = (q or "").strip()

    if op == HISTORY_SEARCH_OP_EMPTY:
        # Hidden: either team text empty.
        return (
            _or_team_clause(
                lambda k: f"trim({_HISTORY_TEAM_TEXT_SQL[k]}) = ''",
                keys,
            ),
            [],
        )
    if op == HISTORY_SEARCH_OP_NOT_EMPTY:
        return (
            _or_team_clause(
                lambda k: f"trim({_HISTORY_TEAM_TEXT_SQL[k]}) != ''",
                keys,
            ),
            [],
        )

    # Hidden numeric/between ops are retained but not applied until UI exposes them.
    if op in {
        HISTORY_SEARCH_OP_GT,
        HISTORY_SEARCH_OP_GTE,
        HISTORY_SEARCH_OP_LT,
        HISTORY_SEARCH_OP_LTE,
        HISTORY_SEARCH_OP_BETWEEN,
    }:
        return None, []

    if not raw_needle:
        return None, []

    if op == HISTORY_SEARCH_OP_APPROX:
        needle = raw_needle.lower()
        clause = _or_team_clause(
            lambda k: f"instr(lower({_HISTORY_TEAM_TEXT_SQL[k]}), ?) > 0",
            keys,
        )
        return clause, [needle] * len(keys)

    if op == HISTORY_SEARCH_OP_EQ:
        clause = _or_team_clause(
            lambda k: f"{_HISTORY_TEAM_TEXT_SQL[k]} = ?",
            keys,
        )
        return clause, [raw_needle] * len(keys)

    if op == HISTORY_SEARCH_OP_NE:
        # Neither home nor away equals needle.
        positives = _or_team_clause(
            lambda k: f"{_HISTORY_TEAM_TEXT_SQL[k]} = ?",
            keys,
        )
        return f"NOT {positives}", [raw_needle] * len(keys)

    if op == HISTORY_SEARCH_OP_CONTAINS:
        clause = _or_team_clause(
            lambda k: f"instr({_HISTORY_TEAM_TEXT_SQL[k]}, ?) > 0",
            keys,
        )
        return clause, [raw_needle] * len(keys)

    if op == HISTORY_SEARCH_OP_NOT_CONTAINS:
        positives = _or_team_clause(
            lambda k: f"instr({_HISTORY_TEAM_TEXT_SQL[k]}, ?) > 0",
            keys,
        )
        return f"NOT {positives}", [raw_needle] * len(keys)

    if op == HISTORY_SEARCH_OP_STARTS:
        clause = _or_team_clause(
            lambda k: (
                f"substr({_HISTORY_TEAM_TEXT_SQL[k]}, 1, length(?)) = ?"
            ),
            keys,
        )
        params: list[Any] = []
        for _k in keys:
            params.extend([raw_needle, raw_needle])
        return clause, params

    if op == HISTORY_SEARCH_OP_ENDS:
        clause = _or_team_clause(
            lambda k: (
                f"substr({_HISTORY_TEAM_TEXT_SQL[k]}, "
                f"length({_HISTORY_TEAM_TEXT_SQL[k]}) - length(?) + 1) = ?"
            ),
            keys,
        )
        params = []
        for _k in keys:
            params.extend([raw_needle, raw_needle])
        return clause, params

    return None, []


def _build_where_and_params(
    *,
    match_date: date | None,
    q: str,
    q_op: str,
    search_keys: list[str] | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if match_date is not None:
        # Date filter and display both use match_date_hkt (Asia/Hong_Kong calendar).
        clauses.append("match_date_hkt = ?")
        params.append(match_date.isoformat())

    q_clause, q_params = _build_q_clause(q=q, q_op=q_op, search_keys=search_keys)
    if q_clause:
        clauses.append(q_clause)
        params.extend(q_params)

    if not clauses:
        return "1 = 1", params
    return " AND ".join(clauses), params


def fetch_history_filtered(
    db_path: Path,
    match_date: date | None,
    *,
    n: Any = 1,
    q: str = "",
    q_op: str = DEFAULT_HISTORY_SEARCH_OP,
    sort_specs: list[tuple[str, str]] | None = None,
    search_keys: list[str] | None = None,
    page_size: int = HISTORY_PAGE_SIZE,
) -> HistoryListResult:
    """
    Backend-paged read of footylogic_matches only (never quarantine / soccer_daily).

    n = 1-based page index. Default sort: kickoff_hkt DESC, source_key ASC.
    Text filter compares home_team / away_team with q_op (default approx).
    """
    if not db_path.is_file():
        raise SoccerDataError(f"soccer database file not found: {db_path}")

    size = max(1, int(page_size or HISTORY_PAGE_SIZE))
    q_text = (q or "").strip()
    op = normalize_history_search_op(q_op)
    all_mode = match_date is None
    specs = list(sort_specs) if sort_specs else default_history_sort_specs()
    where_sql, where_params = _build_where_and_params(
        match_date=match_date,
        q=q_text,
        q_op=op,
        search_keys=search_keys,
    )
    order_sql = _build_order_clause(specs)

    try:
        with get_connection(db_path) as conn:
            if not table_exists(conn, FOOTYLOGIC_MATCHES_TABLE):
                raise SoccerDataError(f"table missing: {FOOTYLOGIC_MATCHES_TABLE}")
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM {FOOTYLOGIC_MATCHES_TABLE}
                WHERE {where_sql};
                """,
                where_params,
            ).fetchone()
            total_count = int(total_row["c"] if total_row is not None else 0)
            total_pages = max(1, (total_count + size - 1) // size) if total_count else 0
            page = normalize_record_index(n, total_count=total_pages if total_pages else 0)
            if total_count <= 0:
                rows: list[Any] = []
            else:
                offset = (page - 1) * size
                rows = conn.execute(
                    f"""
                    {_HISTORY_SELECT_SQL}
                    WHERE {where_sql}
                    ORDER BY {order_sql}
                    LIMIT ? OFFSET ?;
                    """,
                    [*where_params, size, offset],
                ).fetchall()
    except SoccerDataError:
        raise
    except Exception as e:
        label = "all_matches" if all_mode else f"match_date_hkt={match_date.isoformat()}"
        raise SoccerDataError(f"history query failed for {label}: {e}") from e

    matches = [_row_to_history_view(r) for r in rows]
    field_keys: tuple[str, ...] = tuple(matches[0].keys()) if matches else ()
    return HistoryListResult(
        matches=matches,
        n=page if total_count else 1,
        total_count=total_count,
        total_pages=total_pages if total_count else 0,
        page_size=size,
        q=q_text,
        all_matches=all_mode,
        sort=history_sort_param(specs),
        q_op=op,
        field_keys=field_keys,
    )


# Re-export helpers used by the web layer for history query args.
__all__ = [
    "DEFAULT_HISTORY_SEARCH_KEYS",
    "DEFAULT_HISTORY_SEARCH_OP",
    "HISTORY_PAGE_SIZE",
    "HISTORY_SEARCH_OPS_HIDDEN",
    "HISTORY_SEARCH_OPS_VISIBLE",
    "HistoryListResult",
    "SoccerDataError",
    "default_history_sort_specs",
    "fetch_history_filtered",
    "format_name_or_id",
    "history_sort_param",
    "history_sort_specs_from_layout",
    "normalize_history_search_op",
    "parse_all_matches_flag",
    "parse_history_sort_param",
    "parse_requested_date",
    "resolve_all_matches_arg",
]
