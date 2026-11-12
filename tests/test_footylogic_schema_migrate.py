from __future__ import annotations

import sqlite3
from pathlib import Path

from hppv.footylogic_ingest import ensure_footylogic_schema, upsert_matches
from hppv.footylogic_parse import ParsedFootylogicMatch
from hppv.footylogic_schema import (
    FOOTYLOGIC_MATCHES_TABLE,
    footylogic_matches_needs_nullable_id_rebuild,
)
from hppv.schema import SOCCER_DAILY_REMOVED_TABLE
from hppv.soccer_db import get_connection, init_soccer_db


def _legacy_matches_ddl() -> str:
    """Pre-HKJC CREATE with competition_id/season_id NOT NULL (no hkjc cols)."""
    return f"""
    CREATE TABLE {FOOTYLOGIC_MATCHES_TABLE} (
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
    """


def test_rebuild_makes_ids_nullable_and_preserves_rows(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(_legacy_matches_ddl())
        conn.execute(
            f"""
            INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
                source_key, competition_id, season_id, month,
                home_team_name, away_team_name,
                kickoff_utc, kickoff_hkt, match_date_hkt,
                score_ft_home, score_ft_away, has_score,
                event_json, created_at, updated_at
            ) VALUES (
                'fl|1|2|h|a|2026-01-01T12:00:00Z', '1', '2', '2026-01',
                'Home', 'Away',
                '2026-01-01T12:00:00Z', '2026-01-01T20:00:00+08:00', '2026-01-01',
                2, 1, 1,
                '{{}}', 't', 't'
            )
            """
        )
        conn.commit()
        assert footylogic_matches_needs_nullable_id_rebuild(conn) is True

    with get_connection(db) as conn:
        ensure_footylogic_schema(conn)
        conn.commit()
        assert footylogic_matches_needs_nullable_id_rebuild(conn) is False
        cols = {
            str(r[1]): int(r[3])
            for r in conn.execute(f"PRAGMA table_info({FOOTYLOGIC_MATCHES_TABLE})")
        }
        assert cols["competition_id"] == 0
        assert cols["season_id"] == 0
        assert cols["month"] == 1
        assert "hkjc_match_id" in cols
        assert "hkjc_front_end_id" in cols
        row = conn.execute(
            f"SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key LIKE 'fl|%'"
        ).fetchone()
        assert row["competition_id"] == "1"
        assert row["season_id"] == "2"
        assert row["score_ft_home"] == 2
        # HKJC row with NULL competition/season must be allowed.
        conn.execute(
            f"""
            INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
                source_key, source, competition_id, season_id, month,
                kickoff_utc, kickoff_hkt, match_date_hkt,
                event_json, created_at, updated_at,
                hkjc_match_id, hkjc_front_end_id
            ) VALUES (
                'hkjc|50001', 'hkjc', NULL, NULL, '2026-11',
                '2026-11-12T12:00:00Z', '2026-11-12T20:00:00+08:00', '2026-11-12',
                '{{}}', 't', 't',
                '50001', 'FB9001'
            )
            """
        )
        conn.commit()
        assert (
            conn.execute(f"SELECT COUNT(*) FROM {FOOTYLOGIC_MATCHES_TABLE}").fetchone()[0]
            == 2
        )


def test_ensure_schema_idempotent_after_rebuild(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(_legacy_matches_ddl())
        conn.commit()
    with get_connection(db) as conn:
        ensure_footylogic_schema(conn)
        ensure_footylogic_schema(conn)
        conn.commit()
        assert footylogic_matches_needs_nullable_id_rebuild(conn) is False


def test_footylogic_upsert_still_requires_identity_ids(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    row = ParsedFootylogicMatch(
        source_key="fl|9|8|h1|a1|2026-02-01T10:00:00Z",
        competition_id="9",
        season_id="8",
        month="2026-02",
        competition_name="Cup",
        home_team_id="h1",
        away_team_id="a1",
        home_team_name="H",
        away_team_name="A",
        kickoff_utc="2026-02-01T10:00:00Z",
        kickoff_hkt="2026-02-01T18:00:00+08:00",
        match_date_hkt="2026-02-01",
        score_ft_home=1,
        score_ft_away=0,
        score_ht_home=None,
        score_ht_away=None,
        score_2h_home=None,
        score_2h_away=None,
        score_et_home=None,
        score_et_away=None,
        score_pens_home=None,
        score_pens_away=None,
        final_odds_home=None,
        final_odds_draw=None,
        final_odds_away=None,
        final_odds_timestamp=None,
        final_odds_source=None,
        has_score=True,
        score_complete=False,
        result_confirm_status="source_score_unconfirmed",
        identity_ok=True,
        quarantine_reason=None,
        event_json="{}",
        source_last_updated=None,
    )
    with get_connection(db) as conn:
        n = upsert_matches(conn, [row])
        conn.commit()
        assert n == 1
        stored = conn.execute(
            f"SELECT source, competition_id, season_id FROM {FOOTYLOGIC_MATCHES_TABLE}"
            " WHERE source_key=?",
            (row.source_key,),
        ).fetchone()
        assert stored["source"] == "footylogic"
        assert stored["competition_id"] == "9"
        assert stored["season_id"] == "8"


def test_init_soccer_db_creates_tombstone_table(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        cols = {
            str(r["name"])
            for r in conn.execute(f"PRAGMA table_info({SOCCER_DAILY_REMOVED_TABLE})")
        }
        assert cols == {"match_id", "removed_at", "reason"}
