from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hppv.schema import soccer_daily_expected_columns, soccer_sync_state_expected_columns
from hppv.soccer_db import SoccerSchemaMismatchError, get_connection, init_soccer_db


def test_additive_score_columns_upgrade_existing_db(tmp_path: Path) -> None:
    import sqlite3

    from hppv.soccer_db import ensure_soccer_daily_columns, init_soccer_db, report_schema_diff

    db_path = tmp_path / "soccer.sqlite3"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE soccer_daily (
                match_id TEXT PRIMARY KEY NOT NULL,
                match_code TEXT,
                match_date TEXT NOT NULL,
                kickoff_at TEXT NOT NULL,
                competition_code TEXT,
                competition_name_zh TEXT,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                match_status TEXT,
                home_odds REAL,
                draw_odds REAL,
                away_odds REAL,
                odds_status TEXT,
                odds_fetched_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO soccer_daily (match_id, match_date, kickoff_at, home_team, away_team, created_at, updated_at) "
            "VALUES ('KEEP','2026-11-04','2026-11-04T20:00:00+08:00','H','A','t','t')"
        )
        conn.commit()

    init_soccer_db(db_path)
    with get_connection(db_path) as conn:
        assert report_schema_diff(conn) is None
        row = conn.execute(
            "SELECT match_id, score_ht_home, result_status FROM soccer_daily WHERE match_id='KEEP'"
        ).fetchone()
        assert row["match_id"] == "KEEP"
        assert row["score_ht_home"] is None
        assert row["result_status"] is None
        assert ensure_soccer_daily_columns(conn) == []


def test_init_soccer_db_creates_table_and_index(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)

    with get_connection(db_path) as conn:
        cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(soccer_daily);").fetchall()
        }
        assert cols == soccer_daily_expected_columns()
        assert "competition_code" in cols
        assert "competition_name_zh" in cols
        assert "competition_code" != "competition_name_zh"

        sync_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(soccer_sync_state);").fetchall()
        }
        assert sync_cols == soccer_sync_state_expected_columns()

        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_soccer_daily_date_kickoff",),
        ).fetchone()
        assert idx is not None


def test_init_soccer_db_is_idempotent_and_preserves_data(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO soccer_daily (
                match_id, match_date, kickoff_at, home_team, away_team,
                home_odds, draw_odds, away_odds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                "M1",
                "2026-11-04",
                "2026-11-04T20:00:00+08:00",
                "Home FC",
                "Away FC",
                "2026-11-04T00:00:00+00:00",
                "2026-11-04T00:00:00+00:00",
            ),
        )
        conn.commit()

    init_soccer_db(db_path)

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT match_id, home_odds, draw_odds, away_odds FROM soccer_daily WHERE match_id = ?",
            ("M1",),
        ).fetchone()
        assert row is not None
        assert row["match_id"] == "M1"
        assert row["home_odds"] is None
        assert row["draw_odds"] is None
        assert row["away_odds"] is None
        count = conn.execute("SELECT COUNT(*) AS c FROM soccer_daily;").fetchone()
        assert int(count["c"]) == 1


def test_match_id_primary_key_rejects_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)

    insert_sql = """
        INSERT INTO soccer_daily (
            match_id, match_date, kickoff_at, home_team, away_team,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    values = (
        "M1",
        "2026-11-04",
        "2026-11-04T20:00:00+08:00",
        "Home FC",
        "Away FC",
        "2026-11-04T00:00:00+00:00",
        "2026-11-04T00:00:00+00:00",
    )
    with get_connection(db_path) as conn:
        conn.execute(insert_sql, values)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert_sql, values)


def test_schema_mismatch_is_reported_without_drop(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE soccer_daily (match_id TEXT PRIMARY KEY, legacy_only TEXT);"
        )
        conn.execute(
            "INSERT INTO soccer_daily (match_id, legacy_only) VALUES ('KEEP', 'x');"
        )
        conn.commit()

    with pytest.raises(SoccerSchemaMismatchError) as exc:
        init_soccer_db(db_path)
    msg = str(exc.value)
    assert "缺少欄位" in msg or "多餘欄位" in msg

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT match_id, legacy_only FROM soccer_daily WHERE match_id = 'KEEP';"
        ).fetchone()
        assert row == ("KEEP", "x")

