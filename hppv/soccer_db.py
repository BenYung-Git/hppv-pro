from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from hppv.config import default_config
from hppv.schema import (
    SOCCER_DAILY_ADDITIVE_COLUMNS,
    SOCCER_DAILY_TABLE,
    SOCCER_LAYOUT_SETTINGS_TABLE,
    SOCCER_SYNC_STATE_TABLE,
    soccer_daily_expected_columns,
    soccer_daily_index_sql,
    soccer_daily_schema_sql,
    soccer_layout_settings_expected_columns,
    soccer_layout_settings_schema_sql,
    soccer_sync_state_expected_columns,
    soccer_sync_state_schema_sql,
)


class SoccerSchemaMismatchError(RuntimeError):
    """Raised when a soccer DB table exists but does not match the expected schema."""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a soccer SQLite connection with safe defaults."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Get a soccer SQLite connection (recommended wrapper)."""
    return connect(db_path)


def _read_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}");').fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["name"]) for row in rows}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1;",
        (table,),
    ).fetchone()
    return row is not None


def ensure_soccer_daily_columns(conn: sqlite3.Connection) -> list[str]:
    """
    Idempotent additive upgrade: ADD COLUMN for missing expected soccer_daily fields.
    Does not drop/rename columns. Returns names of columns added.
    """
    if not table_exists(conn, SOCCER_DAILY_TABLE):
        return []
    actual = _read_table_columns(conn, SOCCER_DAILY_TABLE)
    added: list[str] = []
    for name, sql_type in SOCCER_DAILY_ADDITIVE_COLUMNS:
        if name in actual:
            continue
        conn.execute(f'ALTER TABLE "{SOCCER_DAILY_TABLE}" ADD COLUMN "{name}" {sql_type};')
        added.append(name)
    return added


def report_schema_diff(
    conn: sqlite3.Connection,
    table: str = SOCCER_DAILY_TABLE,
    *,
    allow_extra: bool = False,
) -> str | None:
    """
    Return a human-readable schema diff if table exists but columns differ.
    Return None when table is missing or matches expected columns.

    allow_extra=True: only missing expected columns are errors (used by read paths
    so additive upgrades do not block listing when extras are present).
    """
    if not table_exists(conn, table):
        return None
    expected = soccer_daily_expected_columns()
    actual = _read_table_columns(conn, table)
    missing = sorted(expected - actual)
    unexpected = [] if allow_extra else sorted(actual - expected)
    if not missing and not unexpected:
        return None
    parts = [f"表 `{table}` 結構與預期 schema 不符。"]
    if missing:
        parts.append("缺少欄位: " + ", ".join(missing))
    if unexpected:
        parts.append("多餘欄位: " + ", ".join(unexpected))
    parts.append("不會刪表或重建；請手動處理後再初始化。")
    return "\n".join(parts)


def report_sync_state_schema_diff(conn: sqlite3.Connection) -> str | None:
    if not table_exists(conn, SOCCER_SYNC_STATE_TABLE):
        return None
    expected = soccer_sync_state_expected_columns()
    actual = _read_table_columns(conn, SOCCER_SYNC_STATE_TABLE)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if not missing and not unexpected:
        return None
    parts = [f"表 `{SOCCER_SYNC_STATE_TABLE}` 結構與預期不符。"]
    if missing:
        parts.append("缺少欄位: " + ", ".join(missing))
    if unexpected:
        parts.append("多餘欄位: " + ", ".join(unexpected))
    parts.append("不會刪表或重建；請手動處理後再初始化。")
    return "\n".join(parts)


def report_layout_settings_schema_diff(conn: sqlite3.Connection) -> str | None:
    if not table_exists(conn, SOCCER_LAYOUT_SETTINGS_TABLE):
        return None
    expected = soccer_layout_settings_expected_columns()
    actual = _read_table_columns(conn, SOCCER_LAYOUT_SETTINGS_TABLE)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if not missing and not unexpected:
        return None
    parts = [f"表 `{SOCCER_LAYOUT_SETTINGS_TABLE}` 結構與預期不符。"]
    if missing:
        parts.append("缺少欄位: " + ", ".join(missing))
    if unexpected:
        parts.append("多餘欄位: " + ", ".join(unexpected))
    parts.append("不會刪表或重建；請手動處理後再初始化。")
    return "\n".join(parts)


def init_soccer_db(db_path: Path) -> dict[str, int]:
    """
    Create soccer.sqlite3 schema if missing
    (soccer_daily + translations + soccer_sync_state + soccer_layout_settings).

    Idempotent: safe to re-run; does not drop tables or clear data.
    Seeds missing translation keys only (never overwrites existing rows).
    """
    # Local import avoids circular import at module load.
    from hppv.translations import ensure_translations_schema, seed_initial_translations

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        sync_diff = report_sync_state_schema_diff(conn)
        if sync_diff is not None:
            raise SoccerSchemaMismatchError(sync_diff)
        layout_diff = report_layout_settings_schema_diff(conn)
        if layout_diff is not None:
            raise SoccerSchemaMismatchError(layout_diff)
        conn.executescript(soccer_daily_schema_sql())
        ensure_soccer_daily_columns(conn)
        diff = report_schema_diff(conn)
        if diff is not None:
            raise SoccerSchemaMismatchError(diff)
        conn.executescript(soccer_daily_index_sql())
        conn.executescript(soccer_sync_state_schema_sql())
        conn.executescript(soccer_layout_settings_schema_sql())
        ensure_translations_schema(conn)
        inserted = seed_initial_translations(conn)
    return {"translations_inserted": inserted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HPPV soccer DB utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser(
        "init",
        help="Initialize soccer.sqlite3 (soccer_daily + translations seed)",
    )
    init_p.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override soccer DB path (default: AppConfig.soccer_db_path)",
    )

    args = parser.parse_args(argv)
    if args.command == "init":
        cfg = default_config()
        db_path = args.db_path or cfg.soccer_db_path
        try:
            result = init_soccer_db(db_path)
        except SoccerSchemaMismatchError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"Initialized soccer DB: {db_path.resolve()}")
        print(f"translations_inserted={result['translations_inserted']}")
        return 0
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

