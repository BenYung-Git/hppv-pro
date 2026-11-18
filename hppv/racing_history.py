"""Read-only racing history list from existing race_records (+ horses join)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hppv.db import _RACE_RECORD_DATE_ORDER_EXPR, get_connection
from hppv.racing_layout import (
    DEFAULT_RACING_SEARCH_KEYS,
    RACING_COLUMN_BY_KEY,
    RACING_TEXT_SEARCH_WHITELIST,
    racing_search_keys_from_layout,
    racing_sort_param,
    racing_sort_specs_from_layout,
)

RACING_HISTORY_PAGE_SIZE = 50
EMPTY_DISPLAY = "—"

# Display key → SQL expression (aliases from join).
_RACING_COL_SQL: dict[str, str] = {
    "id": "rr.id",
    "horse_id": "rr.horse_id",
    "brand_no": "COALESCE(h.brand_no, '')",
    "name_zh": "COALESCE(h.name_zh, '')",
    "name_en": "COALESCE(h.name_en, '')",
    "race_date": "rr.race_date",
    "venue": "rr.venue",
    "race_no": "rr.race_no",
    "course": "rr.course",
    "distance": "rr.distance",
    "going": "rr.going",
    "class_band": "rr.class_band",
    "draw": "rr.draw",
    "rating": "rr.rating",
    "jockey": "rr.jockey",
    "trainer": "rr.trainer",
    "finish_pos": "rr.finish_pos",
    "running_pos": "rr.running_pos",
    "win_odds": "rr.win_odds",
    "time": "rr.time",
    "prize": "rr.prize",
}

_RACING_SORT_SQL: dict[str, str] = {
    "id": "rr.id",
    "horse_id": "rr.horse_id",
    "brand_no": "COALESCE(h.brand_no, '')",
    "name_zh": "COALESCE(h.name_zh, '')",
    "name_en": "COALESCE(h.name_en, '')",
    "race_date": _RACE_RECORD_DATE_ORDER_EXPR,
    "venue": "rr.venue",
    "race_no": "CAST(rr.race_no AS INTEGER)",
    "course": "rr.course",
    "distance": "CAST(rr.distance AS INTEGER)",
    "going": "rr.going",
    "class_band": "rr.class_band",
    "draw": "CAST(rr.draw AS INTEGER)",
    "rating": "CAST(rr.rating AS INTEGER)",
    "jockey": "rr.jockey",
    "trainer": "rr.trainer",
    "finish_pos": "rr.finish_pos",
    "win_odds": "CAST(rr.win_odds AS REAL)",
}

HISTORY_SEARCH_OP_APPROX = "approx"
HISTORY_SEARCH_OP_EQ = "eq"
HISTORY_SEARCH_OP_NE = "ne"
HISTORY_SEARCH_OP_CONTAINS = "contains"
HISTORY_SEARCH_OP_NOT_CONTAINS = "not_contains"
HISTORY_SEARCH_OP_STARTS = "starts"
HISTORY_SEARCH_OP_ENDS = "ends"
DEFAULT_HISTORY_SEARCH_OP = HISTORY_SEARCH_OP_APPROX
HISTORY_SEARCH_OPS_VISIBLE: tuple[str, ...] = (
    HISTORY_SEARCH_OP_APPROX,
    HISTORY_SEARCH_OP_EQ,
    HISTORY_SEARCH_OP_NE,
    HISTORY_SEARCH_OP_CONTAINS,
    HISTORY_SEARCH_OP_NOT_CONTAINS,
    HISTORY_SEARCH_OP_STARTS,
    HISTORY_SEARCH_OP_ENDS,
)
HISTORY_SEARCH_OPS_ALL = frozenset(HISTORY_SEARCH_OPS_VISIBLE)


class RacingHistoryError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class RacingHistorySearchError(RacingHistoryError):
    pass


@dataclass(frozen=True, slots=True)
class RacingHistoryListResult:
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


def normalize_history_search_op(raw: str | None) -> str:
    op = str(raw or "").strip().lower()
    if op in HISTORY_SEARCH_OPS_ALL:
        return op
    return DEFAULT_HISTORY_SEARCH_OP


def parse_racing_sort_param(raw: str | None) -> list[tuple[str, str]] | None:
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
        if key not in _RACING_SORT_SQL:
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


def _race_date_iso_expr() -> str:
    # Reuse same CASE as sort key for equality filter YYYY-MM-DD.
    return _RACE_RECORD_DATE_ORDER_EXPR


def _build_q_clause(
    q: str,
    q_op: str,
    search_keys: list[str],
) -> tuple[str, list[Any]]:
    q = (q or "").strip()
    if not q:
        return "", []
    keys = [k for k in search_keys if k in RACING_TEXT_SEARCH_WHITELIST and k in _RACING_COL_SQL]
    if not keys:
        raise RacingHistorySearchError("no_searchable_fields")

    exprs = [_RACING_COL_SQL[k] for k in keys]
    op = normalize_history_search_op(q_op)
    params: list[Any] = []

    def lower_expr(e: str) -> str:
        return f"lower({e})"

    if op == HISTORY_SEARCH_OP_APPROX:
        parts = [f"instr({lower_expr(e)}, ?) > 0" for e in exprs]
        params.extend([q.lower()] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_EQ:
        parts = [f"{e} = ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NE:
        parts = [f"ifnull({e}, '') != ?" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_CONTAINS:
        parts = [f"instr({e}, ?) > 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_NOT_CONTAINS:
        parts = [f"instr(ifnull({e}, ''), ?) = 0" for e in exprs]
        params.extend([q] * len(exprs))
        return "(" + " AND ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_STARTS:
        parts = [f"{e} LIKE ?" for e in exprs]
        params.extend([q + "%"] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    if op == HISTORY_SEARCH_OP_ENDS:
        parts = [f"{e} LIKE ?" for e in exprs]
        params.extend(["%" + q] * len(exprs))
        return "(" + " OR ".join(parts) + ")", params
    return "", []


def _format_cell(value: Any) -> str:
    if value is None:
        return EMPTY_DISPLAY
    text = str(value).strip()
    return text if text != "" else EMPTY_DISPLAY


def _row_to_view(row: Any) -> dict[str, str]:
    keys = list(row.keys()) if hasattr(row, "keys") else []
    out: dict[str, str] = {}
    for key in _RACING_COL_SQL:
        out[key] = _format_cell(row[key] if key in keys else None)
    return out


def fetch_racing_history_filtered(
    db_path: Path,
    *,
    all_matches: bool = True,
    race_date: str | None = None,
    n: int | None = 1,
    q: str = "",
    q_op: str = DEFAULT_HISTORY_SEARCH_OP,
    sort_specs: list[tuple[str, str]] | None = None,
    search_keys: list[str] | None = None,
    page_size: int = RACING_HISTORY_PAGE_SIZE,
) -> RacingHistoryListResult:
    """Server-paged read of race_records LEFT JOIN horses. Read-only."""
    page = _normalize_page(n)
    size = max(1, int(page_size or RACING_HISTORY_PAGE_SIZE))
    keys = list(search_keys) if search_keys is not None else list(DEFAULT_RACING_SEARCH_KEYS)
    specs = sort_specs if sort_specs else [("race_date", "desc")]

    where: list[str] = []
    params: list[Any] = []
    if not all_matches:
        date_text = str(race_date or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            raise RacingHistoryError("invalid_date", date_text)
        where.append(f"{_race_date_iso_expr()} = ?")
        params.append(date_text)

    q_clause, q_params = _build_q_clause(q, q_op, keys)
    if q_clause:
        where.append(q_clause)
        params.extend(q_params)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    order_parts: list[str] = []
    for key, direction in specs:
        expr = _RACING_SORT_SQL.get(key)
        if not expr:
            continue
        order_parts.append(f"{expr} {'ASC' if direction == 'asc' else 'DESC'}")
    order_parts.append("rr.id DESC")
    order_sql = ", ".join(order_parts)

    select_cols = ",\n      ".join(
        f"{expr} AS {key}" for key, expr in _RACING_COL_SQL.items()
    )
    from_sql = (
        "FROM race_records AS rr "
        "LEFT JOIN horses AS h ON h.horse_id = rr.horse_id"
    )

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

    rows = [_row_to_view(r) for r in rows_raw]
    field_keys = tuple(_RACING_COL_SQL.keys())
    return RacingHistoryListResult(
        rows=rows,
        n=page if total else 0,
        total_count=total,
        total_pages=total_pages,
        page_size=size,
        q=q or "",
        all_matches=bool(all_matches),
        sort=racing_sort_param(specs),
        q_op=normalize_history_search_op(q_op),
        field_keys=field_keys,
        search_keys=tuple(keys),
    )


def count_racing_history_rows(db_path: Path) -> int:
    with get_connection(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM race_records").fetchone()["c"] or 0)
