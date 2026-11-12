"""Footylogic historical results tables (separate from soccer_daily / HKJC)."""

from __future__ import annotations

import sqlite3
from textwrap import dedent

FOOTYLOGIC_MATCHES_TABLE = "footylogic_matches"
FOOTYLOGIC_JOBS_TABLE = "footylogic_jobs"
FOOTYLOGIC_RAW_TABLE = "footylogic_raw_months"
FOOTYLOGIC_QUARANTINE_TABLE = "footylogic_quarantine"

# Canonical physical columns after nullable-id + HKJC additive migration.
FOOTYLOGIC_MATCHES_COLUMNS: tuple[str, ...] = (
    "source_key",
    "source",
    "competition_id",
    "season_id",
    "month",
    "competition_name",
    "home_team_id",
    "away_team_id",
    "home_team_name",
    "away_team_name",
    "kickoff_utc",
    "kickoff_hkt",
    "match_date_hkt",
    "score_ft_home",
    "score_ft_away",
    "score_ht_home",
    "score_ht_away",
    "score_2h_home",
    "score_2h_away",
    "score_et_home",
    "score_et_away",
    "score_pens_home",
    "score_pens_away",
    "final_odds_home",
    "final_odds_draw",
    "final_odds_away",
    "final_odds_timestamp",
    "final_odds_source",
    "has_score",
    "score_complete",
    "result_confirm_status",
    "event_json",
    "source_last_updated",
    "created_at",
    "updated_at",
    "hkjc_match_id",
    "hkjc_front_end_id",
)


def _footylogic_matches_create_sql(
    *, table_name: str, if_not_exists: bool = False
) -> str:
    """CREATE TABLE for footylogic_matches (or a rebuild temp name)."""
    exists = "IF NOT EXISTS " if if_not_exists else ""
    return dedent(
        f"""
        CREATE TABLE {exists}{table_name} (
            source_key TEXT PRIMARY KEY NOT NULL,
            source TEXT NOT NULL DEFAULT 'footylogic',
            competition_id TEXT,
            season_id TEXT,
            month TEXT NOT NULL,
            competition_name TEXT,
            home_team_id TEXT,
            away_team_id TEXT,
            home_team_name TEXT,
            away_team_name TEXT,
            kickoff_utc TEXT NOT NULL,
            kickoff_hkt TEXT NOT NULL,
            match_date_hkt TEXT NOT NULL,
            score_ft_home INTEGER,
            score_ft_away INTEGER,
            score_ht_home INTEGER,
            score_ht_away INTEGER,
            score_2h_home INTEGER,
            score_2h_away INTEGER,
            score_et_home INTEGER,
            score_et_away INTEGER,
            score_pens_home INTEGER,
            score_pens_away INTEGER,
            final_odds_home REAL,
            final_odds_draw REAL,
            final_odds_away REAL,
            final_odds_timestamp TEXT,
            final_odds_source TEXT,
            has_score INTEGER NOT NULL DEFAULT 0,
            score_complete INTEGER NOT NULL DEFAULT 0,
            result_confirm_status TEXT NOT NULL DEFAULT 'source_score_unconfirmed',
            event_json TEXT NOT NULL,
            source_last_updated TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            hkjc_match_id TEXT,
            hkjc_front_end_id TEXT
        );
        """
    )


def footylogic_matches_base_indexes_sql(
    *, table_name: str = FOOTYLOGIC_MATCHES_TABLE
) -> str:
    """Indexes that do not require HKJC columns (safe on legacy tables)."""
    return dedent(
        f"""
        CREATE INDEX IF NOT EXISTS idx_fl_matches_date
            ON {table_name}(match_date_hkt, competition_id);
        CREATE INDEX IF NOT EXISTS idx_fl_matches_comp_season
            ON {table_name}(competition_id, season_id);
        CREATE INDEX IF NOT EXISTS idx_fl_matches_has_score
            ON {table_name}(has_score, match_date_hkt);
        """
    )


def footylogic_matches_hkjc_indexes_sql(
    *, table_name: str = FOOTYLOGIC_MATCHES_TABLE
) -> str:
    """Indexes requiring hkjc_match_id; call only after that column exists."""
    return dedent(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fl_matches_hkjc_match_id
            ON {table_name}(hkjc_match_id)
            WHERE hkjc_match_id IS NOT NULL;
        """
    )


def footylogic_matches_indexes_sql(*, table_name: str = FOOTYLOGIC_MATCHES_TABLE) -> str:
    return (
        footylogic_matches_base_indexes_sql(table_name=table_name)
        + footylogic_matches_hkjc_indexes_sql(table_name=table_name)
    )


def footylogic_schema_sql() -> str:
    """DDL for Footylogic source tables. Does not touch soccer_daily."""
    return dedent(
        f"""
        {_footylogic_matches_create_sql(
            table_name=FOOTYLOGIC_MATCHES_TABLE, if_not_exists=True
        )}
        {footylogic_matches_base_indexes_sql()}

        CREATE TABLE IF NOT EXISTS {FOOTYLOGIC_JOBS_TABLE} (
            job_key TEXT PRIMARY KEY NOT NULL,
            competition_id TEXT NOT NULL,
            competition_name TEXT,
            season_id TEXT NOT NULL,
            season_name TEXT,
            month TEXT NOT NULL,
            browse_category TEXT,
            country_or_group TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            http_status INTEGER,
            events_raw INTEGER,
            in_range INTEGER,
            upserted INTEGER,
            quarantined INTEGER,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fl_jobs_status
            ON {FOOTYLOGIC_JOBS_TABLE}(status, month);

        CREATE TABLE IF NOT EXISTS {FOOTYLOGIC_RAW_TABLE} (
            job_key TEXT PRIMARY KEY NOT NULL,
            competition_id TEXT NOT NULL,
            season_id TEXT NOT NULL,
            month TEXT NOT NULL,
            http_status INTEGER,
            business_ok INTEGER,
            message TEXT,
            raw_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {FOOTYLOGIC_QUARANTINE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_key TEXT,
            reason TEXT NOT NULL,
            competition_id TEXT,
            season_id TEXT,
            month TEXT,
            kickoff_utc TEXT,
            home_team_id TEXT,
            away_team_id TEXT,
            home_team_name TEXT,
            away_team_name TEXT,
            event_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS footylogic_score_derivations (
            source_key TEXT NOT NULL,
            field TEXT NOT NULL,
            value_home INTEGER,
            value_away INTEGER,
            formula TEXT NOT NULL,
            class_code TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            source_fields_json TEXT NOT NULL,
            calculated_at TEXT NOT NULL,
            PRIMARY KEY (source_key, field)
        );
        """
    )


# Added after initial Footylogic ingest; migrate via ALTER when missing.
# Score/odds columns stay here for DBs that already have nullable ids without rebuild.
FOOTYLOGIC_MATCHES_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("score_2h_home", "INTEGER"),
    ("score_2h_away", "INTEGER"),
    ("score_et_home", "INTEGER"),
    ("score_et_away", "INTEGER"),
    ("score_pens_home", "INTEGER"),
    ("score_pens_away", "INTEGER"),
    ("final_odds_home", "REAL"),
    ("final_odds_draw", "REAL"),
    ("final_odds_away", "REAL"),
    ("final_odds_timestamp", "TEXT"),
    ("final_odds_source", "TEXT"),
    ("hkjc_match_id", "TEXT"),
    ("hkjc_front_end_id", "TEXT"),
)


def ensure_footylogic_matches_new_columns_sql(existing_columns: set[str]) -> list[str]:
    """Return idempotent ALTER TABLE statements for missing match columns."""
    sqls: list[str] = []
    for name, col_type in FOOTYLOGIC_MATCHES_NEW_COLUMNS:
        if name in existing_columns:
            continue
        sqls.append(
            f"ALTER TABLE {FOOTYLOGIC_MATCHES_TABLE} ADD COLUMN {name} {col_type}"
        )
    return sqls


def _pragma_column_notnull(conn: sqlite3.Connection, table: str, column: str) -> int | None:
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        if str(row[1]) == column:
            return int(row[3])
    return None


def footylogic_matches_needs_nullable_id_rebuild(conn: sqlite3.Connection) -> bool:
    """True when competition_id or season_id still carry NOT NULL (pre-HKJC schema)."""
    for col in ("competition_id", "season_id"):
        notnull = _pragma_column_notnull(conn, FOOTYLOGIC_MATCHES_TABLE, col)
        if notnull is None:
            return False
        if notnull == 1:
            return True
    return False


def rebuild_footylogic_matches_nullable_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Rebuild footylogic_matches so competition_id / season_id are nullable and
    HKJC identity columns exist. Preserves all rows and values.

    SQLite cannot DROP NOT NULL in place; rebuild is required.
    Idempotent: no-op when already nullable.
    """
    if not footylogic_matches_needs_nullable_id_rebuild(conn):
        return {"rebuilt": 0, "rows": 0}

    existing = {
        str(r[1])
        for r in conn.execute(f"PRAGMA table_info({FOOTYLOGIC_MATCHES_TABLE})").fetchall()
    }
    before = int(
        conn.execute(f"SELECT COUNT(*) FROM {FOOTYLOGIC_MATCHES_TABLE}").fetchone()[0]
    )
    tmp = f"{FOOTYLOGIC_MATCHES_TABLE}__migrate_nullable"
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    conn.executescript(_footylogic_matches_create_sql(table_name=tmp))

    select_parts: list[str] = []
    for col in FOOTYLOGIC_MATCHES_COLUMNS:
        if col in existing:
            select_parts.append(col)
        else:
            select_parts.append(f"NULL AS {col}")
    cols_csv = ", ".join(FOOTYLOGIC_MATCHES_COLUMNS)
    select_csv = ", ".join(select_parts)
    conn.execute(
        f"INSERT INTO {tmp} ({cols_csv}) SELECT {select_csv} FROM {FOOTYLOGIC_MATCHES_TABLE}"
    )
    after = int(conn.execute(f"SELECT COUNT(*) FROM {tmp}").fetchone()[0])
    if after != before:
        raise RuntimeError(
            f"footylogic_matches rebuild row mismatch: before={before} after={after}"
        )

    conn.execute(f"DROP TABLE {FOOTYLOGIC_MATCHES_TABLE}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {FOOTYLOGIC_MATCHES_TABLE}")
    conn.executescript(footylogic_matches_base_indexes_sql())
    conn.executescript(footylogic_matches_hkjc_indexes_sql())
    return {"rebuilt": 1, "rows": after}
