"""Footylogic historical results tables (separate from soccer_daily / HKJC)."""

from __future__ import annotations

from textwrap import dedent

FOOTYLOGIC_MATCHES_TABLE = "footylogic_matches"
FOOTYLOGIC_JOBS_TABLE = "footylogic_jobs"
FOOTYLOGIC_RAW_TABLE = "footylogic_raw_months"
FOOTYLOGIC_QUARANTINE_TABLE = "footylogic_quarantine"


def footylogic_schema_sql() -> str:
    """DDL for Footylogic source tables. Does not touch soccer_daily."""
    return dedent(
        f"""
        CREATE TABLE IF NOT EXISTS {FOOTYLOGIC_MATCHES_TABLE} (
            source_key TEXT PRIMARY KEY NOT NULL,
            source TEXT NOT NULL DEFAULT 'footylogic',
            competition_id TEXT NOT NULL,
            season_id TEXT NOT NULL,
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
            has_score INTEGER NOT NULL DEFAULT 0,
            score_complete INTEGER NOT NULL DEFAULT 0,
            result_confirm_status TEXT NOT NULL DEFAULT 'source_score_unconfirmed',
            event_json TEXT NOT NULL,
            source_last_updated TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fl_matches_date
            ON {FOOTYLOGIC_MATCHES_TABLE}(match_date_hkt, competition_id);
        CREATE INDEX IF NOT EXISTS idx_fl_matches_comp_season
            ON {FOOTYLOGIC_MATCHES_TABLE}(competition_id, season_id);
        CREATE INDEX IF NOT EXISTS idx_fl_matches_has_score
            ON {FOOTYLOGIC_MATCHES_TABLE}(has_score, match_date_hkt);

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
        """
    )
