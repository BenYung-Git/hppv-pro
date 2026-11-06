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
from hppv.soccer_matches import (
    EMPTY_DISPLAY,
    SoccerDataError,
    format_kickoff_hhmm,
    format_score_pair,
    format_text_field,
    normalize_record_index,
    parse_all_matches_flag,
    parse_requested_date,
    resolve_all_matches_arg,
)

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

_HISTORY_SEARCH_SQL: dict[str, str] = {
    "home_team": "lower(ifnull(home_team_name, ifnull(home_team_id, '')))",
    "away_team": "lower(ifnull(away_team_name, ifnull(away_team_id, '')))",
    "competition_name": "lower(ifnull(competition_name, ''))",
    "competition_id": "lower(ifnull(competition_id, ''))",
    "home_team_id": "lower(ifnull(home_team_id, ''))",
    "away_team_id": "lower(ifnull(away_team_id, ''))",
    "source_key": "lower(ifnull(source_key, ''))",
}

DEFAULT_HISTORY_SEARCH_KEYS: tuple[str, ...] = (
    "home_team",
    "away_team",
    "competition_name",
    "competition_id",
    "home_team_id",
    "away_team_id",
    "source_key",
)

_HISTORY_SELECT_SQL = f"""
                SELECT
                    source_key,
                    competition_id,
                    competition_name,
                    home_team_id,
                    away_team_id,
                    home_team_name,
                    away_team_name,
                    kickoff_utc,
                    kickoff_hkt,
                    match_date_hkt,
                    score_ft_home,
                    score_ft_away,
                    score_ht_home,
                    score_ht_away
                FROM {FOOTYLOGIC_MATCHES_TABLE}
"""


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


def format_name_or_id(name: Any, source_id: Any) -> str:
    """Prefer verified name; fall back to source id; never invent labels."""
    text = format_text_field(name)
    if text != EMPTY_DISPLAY:
        return text
    return format_text_field(source_id)


def _row_to_history_view(row: Any) -> dict[str, str]:
    source_key = str(row["source_key"] or "")
    return {
        "source_key": source_key,
        "match_date": format_text_field(row["match_date_hkt"]),
        "kickoff_hhmm": format_kickoff_hhmm(row["kickoff_hkt"]),
        "competition_id": format_text_field(row["competition_id"]),
        "competition_name": format_name_or_id(
            row["competition_name"], row["competition_id"]
        ),
        "home_team": format_name_or_id(row["home_team_name"], row["home_team_id"]),
        "away_team": format_name_or_id(row["away_team_name"], row["away_team_id"]),
        "score_ht": format_score_pair(
            row["score_ht_home"],
            row["score_ht_away"],
            match_id=source_key,
            field="score_ht",
        ),
        "score_ft": format_score_pair(
            row["score_ft_home"],
            row["score_ft_away"],
            match_id=source_key,
            field="score_ft",
        ),
    }


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


def _build_where_and_params(
    *,
    match_date: date | None,
    q: str,
    search_keys: list[str] | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if match_date is not None:
        # Date filter and display both use match_date_hkt (Asia/Hong_Kong calendar).
        clauses.append("match_date_hkt = ?")
        params.append(match_date.isoformat())

    needle = (q or "").strip().lower()
    if needle:
        keys = [k for k in (search_keys or list(DEFAULT_HISTORY_SEARCH_KEYS)) if k in _HISTORY_SEARCH_SQL]
        if not keys:
            clauses.append("1 = 0")
        else:
            ors = [f"instr({_HISTORY_SEARCH_SQL[k]}, ?) > 0" for k in keys]
            clauses.append("(" + " OR ".join(ors) + ")")
            params.extend([needle] * len(keys))

    if not clauses:
        return "1 = 1", params
    return " AND ".join(clauses), params


def fetch_history_filtered(
    db_path: Path,
    match_date: date | None,
    *,
    n: Any = 1,
    q: str = "",
    sort_specs: list[tuple[str, str]] | None = None,
    search_keys: list[str] | None = None,
    page_size: int = HISTORY_PAGE_SIZE,
) -> HistoryListResult:
    """
    Backend-paged read of footylogic_matches only (never quarantine / soccer_daily).

    n = 1-based page index. Default sort: kickoff_hkt DESC, source_key ASC.
    """
    if not db_path.is_file():
        raise SoccerDataError(f"soccer database file not found: {db_path}")

    size = max(1, int(page_size or HISTORY_PAGE_SIZE))
    q_text = (q or "").strip()
    all_mode = match_date is None
    specs = list(sort_specs) if sort_specs else default_history_sort_specs()
    where_sql, where_params = _build_where_and_params(
        match_date=match_date,
        q=q_text,
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
    return HistoryListResult(
        matches=matches,
        n=page if total_count else 1,
        total_count=total_count,
        total_pages=total_pages if total_count else 0,
        page_size=size,
        q=q_text,
        all_matches=all_mode,
        sort=history_sort_param(specs),
    )


# Re-export helpers used by the web layer for history query args.
__all__ = [
    "HISTORY_PAGE_SIZE",
    "HistoryListResult",
    "DEFAULT_HISTORY_SEARCH_KEYS",
    "default_history_sort_specs",
    "fetch_history_filtered",
    "format_name_or_id",
    "history_sort_param",
    "history_sort_specs_from_layout",
    "parse_history_sort_param",
    "parse_all_matches_flag",
    "parse_requested_date",
    "resolve_all_matches_arg",
    "SoccerDataError",
]
