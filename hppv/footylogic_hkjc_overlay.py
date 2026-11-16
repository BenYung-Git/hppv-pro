"""HKJC duplicate skip for FootyLogic history download (never overlay/overwrite)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE

OVERLAY_TABLE = "footylogic_hkjc_overlay"

# Reliable FL↔HKJC twin → skip FL write (never apply HKJC onto FL).
STATUS_HKJC_DUP_SKIP = "hkjc_duplicate_skip"
STATUS_UNCERTAIN = "uncertain"
STATUS_NO_MATCH = "no_match"
# Legacy aliases (older rows / tests may still reference these names).
STATUS_APPLIED = STATUS_HKJC_DUP_SKIP  # deprecated meaning: was overlay; now skip
STATUS_SKIPPED = STATUS_NO_MATCH


class _MatchLike(Protocol):
    @property
    def source_key(self) -> str: ...

    @property
    def match_date_hkt(self) -> Any: ...

    @property
    def home_team_name(self) -> Any: ...

    @property
    def away_team_name(self) -> Any: ...

    @property
    def competition_name(self) -> Any: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_overlay_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {OVERLAY_TABLE} (
            fl_source_key TEXT PRIMARY KEY NOT NULL,
            hkjc_source_key TEXT NOT NULL,
            status TEXT NOT NULL,
            match_basis TEXT NOT NULL,
            fl_original_json TEXT,
            reason TEXT,
            applied_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_fl_hkjc_overlay_status
            ON {OVERLAY_TABLE}(status)
        """
    )


def _norm_name(value: Any) -> str:
    return str(value or "").strip()


def _norm_comp(value: Any) -> str:
    return str(value or "").strip()


def _get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return getattr(row, key, None)


def _is_home_away_swap(fl: Any, hk: Any) -> bool:
    return (
        _norm_name(_get(fl, "home_team_name")) == _norm_name(_get(hk, "away_team_name"))
        and _norm_name(_get(fl, "away_team_name")) == _norm_name(_get(hk, "home_team_name"))
        and _norm_name(_get(fl, "home_team_name"))
        != _norm_name(_get(fl, "away_team_name"))
    )


def match_fl_to_hkjc(
    fl_row: Any,
    hkjc_rows: list[Any],
) -> tuple[str, Any | None, str]:
    """
    Return (status, hkjc_row|None, reason).

    STATUS_HKJC_DUP_SKIP: reliable same date+home+away+competition → skip FL write.
    STATUS_UNCERTAIN: ambiguous / swap / competition mismatch → keep FL, mark review.
    STATUS_NO_MATCH: no HKJC twin → proceed with FL write.
    """
    date = _norm_name(_get(fl_row, "match_date_hkt"))
    home = _norm_name(_get(fl_row, "home_team_name"))
    away = _norm_name(_get(fl_row, "away_team_name"))
    comp = _norm_comp(_get(fl_row, "competition_name"))
    if not date or not home or not away:
        return STATUS_UNCERTAIN, None, "missing_identity"

    same_date = [r for r in hkjc_rows if _norm_name(_get(r, "match_date_hkt")) == date]
    if not same_date:
        return STATUS_NO_MATCH, None, "no_hkjc_same_date"

    swaps = [r for r in same_date if _is_home_away_swap(fl_row, r)]
    if swaps:
        return STATUS_UNCERTAIN, None, "home_away_swapped"

    exact_teams = [
        r
        for r in same_date
        if _norm_name(_get(r, "home_team_name")) == home
        and _norm_name(_get(r, "away_team_name")) == away
    ]
    if not exact_teams:
        return STATUS_NO_MATCH, None, "no_hkjc_same_teams"

    if comp:
        exact_comp = [
            r for r in exact_teams if _norm_comp(_get(r, "competition_name")) == comp
        ]
    else:
        exact_comp = []

    if len(exact_comp) == 1:
        return STATUS_HKJC_DUP_SKIP, exact_comp[0], "date_home_away_competition"
    if len(exact_comp) > 1:
        return STATUS_UNCERTAIN, None, "multiple_hkjc_same_competition"

    if len(exact_teams) == 1:
        only = exact_teams[0]
        hk_comp = _norm_comp(_get(only, "competition_name"))
        if comp and hk_comp and comp != hk_comp:
            return STATUS_UNCERTAIN, None, "competition_mismatch"
        if not comp or not hk_comp:
            return STATUS_UNCERTAIN, None, "competition_incomplete"
        return STATUS_HKJC_DUP_SKIP, only, "date_home_away_competition"

    return STATUS_UNCERTAIN, None, "multiple_hkjc_same_teams"


def record_hkjc_decision(
    conn: sqlite3.Connection,
    *,
    fl_source_key: str,
    status: str,
    reason: str,
    match_basis: str = "",
    hkjc_source_key: str = "",
) -> None:
    """Persist skip/uncertain decision. Never mutates footylogic_matches."""
    ensure_overlay_schema(conn)
    now = _utc_now()
    conn.execute(
        f"""
        INSERT INTO {OVERLAY_TABLE} (
            fl_source_key, hkjc_source_key, status, match_basis,
            fl_original_json, reason, applied_at, updated_at
        ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?)
        ON CONFLICT(fl_source_key) DO UPDATE SET
            status=excluded.status,
            hkjc_source_key=excluded.hkjc_source_key,
            match_basis=excluded.match_basis,
            reason=excluded.reason,
            fl_original_json=NULL,
            applied_at=NULL,
            updated_at=excluded.updated_at
        """,
        (
            fl_source_key,
            hkjc_source_key or "",
            status,
            match_basis or reason,
            reason,
            now,
        ),
    )


# Back-compat name used by older call sites / tests.
def record_uncertain_or_skipped(
    conn: sqlite3.Connection,
    *,
    fl_source_key: str,
    status: str,
    reason: str,
    hkjc_source_key: str = "",
) -> None:
    record_hkjc_decision(
        conn,
        fl_source_key=fl_source_key,
        status=status,
        reason=reason,
        hkjc_source_key=hkjc_source_key,
    )


def load_hkjc_by_date_for_year(
    conn: sqlite3.Connection,
    *,
    year: int,
) -> dict[str, list[sqlite3.Row]]:
    year_prefix = f"{int(year):04d}-"
    rows = list(
        conn.execute(
            f"""
            SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE}
             WHERE source='hkjc'
               AND match_date_hkt LIKE ?
            """,
            (year_prefix + "%",),
        )
    )
    by_date: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_date.setdefault(_norm_name(r["match_date_hkt"]), []).append(r)
    return by_date


def filter_fl_rows_for_hkjc_skip(
    conn: sqlite3.Connection,
    rows: Iterable[_MatchLike],
    *,
    hkjc_by_date: dict[str, list[Any]],
) -> tuple[list[Any], dict[str, int]]:
    """
    Split parsed FL rows into upsertable vs HKJC-duplicate-skip.

    - Reliable HKJC twin → skip (no insert/update); record decision.
    - Uncertain → keep in upsert list; record uncertain (do not auto-skip).
    - No match → upsert as FL-only.
    Never deletes existing FL rows. Never writes HKJC scores onto FL.
    """
    ensure_overlay_schema(conn)
    to_upsert: list[Any] = []
    stats = {
        "accepted": 0,
        "hkjc_duplicate_skip": 0,
        "uncertain": 0,
        "fl_only": 0,
        "upsert_candidates": 0,
    }
    for row in rows:
        stats["accepted"] += 1
        date = _norm_name(_get(row, "match_date_hkt"))
        candidates = hkjc_by_date.get(date, [])
        status, hk, reason = match_fl_to_hkjc(row, candidates)
        fl_key = str(_get(row, "source_key") or "")
        if status == STATUS_HKJC_DUP_SKIP and hk is not None:
            record_hkjc_decision(
                conn,
                fl_source_key=fl_key,
                status=STATUS_HKJC_DUP_SKIP,
                reason=reason,
                match_basis=reason,
                hkjc_source_key=str(_get(hk, "source_key") or ""),
            )
            stats["hkjc_duplicate_skip"] += 1
            continue
        if status == STATUS_UNCERTAIN:
            record_hkjc_decision(
                conn,
                fl_source_key=fl_key,
                status=STATUS_UNCERTAIN,
                reason=reason,
            )
            stats["uncertain"] += 1
            to_upsert.append(row)
            continue
        stats["fl_only"] += 1
        to_upsert.append(row)

    stats["upsert_candidates"] = len(to_upsert)
    return to_upsert, stats


def run_hkjc_duplicate_skip_audit_for_year(
    conn: sqlite3.Connection,
    *,
    year: int,
) -> dict[str, int]:
    """
    Post-pass audit on already-stored FL rows for the year.

    Reliable HKJC twins are only recorded as skip decisions — FL rows are NOT
    updated, overwritten, or deleted. Uncertain matches are marked for review.
    """
    ensure_overlay_schema(conn)
    year_prefix = f"{int(year):04d}-"
    fl_rows = list(
        conn.execute(
            f"""
            SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE}
             WHERE source='footylogic'
               AND match_date_hkt LIKE ?
            """,
            (year_prefix + "%",),
        )
    )
    by_date = load_hkjc_by_date_for_year(conn, year=year)
    stats = {
        "fl_candidates": len(fl_rows),
        "hkjc_candidates": sum(len(v) for v in by_date.values()),
        "hkjc_duplicate_skip": 0,
        "uncertain": 0,
        "fl_only": 0,
        # legacy keys for callers
        "applied": 0,
        "skipped": 0,
    }

    for fl in fl_rows:
        date = _norm_name(fl["match_date_hkt"])
        status, hk, reason = match_fl_to_hkjc(fl, by_date.get(date, []))
        if status == STATUS_HKJC_DUP_SKIP and hk is not None:
            # Already in DB from older overlay era or prior write — do not
            # mutate the FL row; only mark skip decision for reporting.
            record_hkjc_decision(
                conn,
                fl_source_key=str(fl["source_key"]),
                status=STATUS_HKJC_DUP_SKIP,
                reason=reason,
                match_basis=reason,
                hkjc_source_key=str(hk["source_key"]),
            )
            stats["hkjc_duplicate_skip"] += 1
            stats["applied"] = stats["hkjc_duplicate_skip"]  # legacy alias
        elif status == STATUS_UNCERTAIN:
            record_hkjc_decision(
                conn,
                fl_source_key=str(fl["source_key"]),
                status=STATUS_UNCERTAIN,
                reason=reason,
            )
            stats["uncertain"] += 1
        else:
            stats["fl_only"] += 1

    return stats


# Back-compat name: former overlay runner; now audit/skip-record only.
def run_hkjc_overlay_for_year(
    conn: sqlite3.Connection,
    *,
    year: int,
) -> dict[str, int]:
    return run_hkjc_duplicate_skip_audit_for_year(conn, year=year)
