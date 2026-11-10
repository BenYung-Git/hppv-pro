"""Upsert Footylogic parsed matches into dedicated tables (not soccer_daily)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import json

from hppv.footylogic_parse import (
    FT_90MIN_EVIDENCE,
    ParsedFootylogicMatch,
    classify_score_2h_eligibility,
)
from hppv.footylogic_schema import (
    FOOTYLOGIC_JOBS_TABLE,
    FOOTYLOGIC_MATCHES_TABLE,
    FOOTYLOGIC_QUARANTINE_TABLE,
    FOOTYLOGIC_RAW_TABLE,
    ensure_footylogic_matches_new_columns_sql,
    footylogic_schema_sql,
)
from hppv.soccer_db import get_connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_footylogic_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(footylogic_schema_sql())
    existing = {
        str(r[1])
        for r in conn.execute(f"PRAGMA table_info({FOOTYLOGIC_MATCHES_TABLE})").fetchall()
    }
    for stmt in ensure_footylogic_matches_new_columns_sql(existing):
        conn.execute(stmt)


def init_footylogic_tables(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        ensure_footylogic_schema(conn)
        conn.commit()


def seed_jobs(conn: sqlite3.Connection, jobs: Iterable[dict[str, Any]]) -> int:
    """Insert pending jobs if missing. Does not reset completed jobs."""
    ensure_footylogic_schema(conn)
    now = _utc_now()
    inserted = 0
    for job in jobs:
        cur = conn.execute(
            f"""
            INSERT OR IGNORE INTO {FOOTYLOGIC_JOBS_TABLE} (
                job_key, competition_id, competition_name, season_id, season_name,
                month, browse_category, country_or_group, status, attempts, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
            """,
            (
                job["job_key"],
                str(job["competitionId"]),
                job.get("competitionName"),
                str(job["seasonId"]),
                job.get("seasonName"),
                job["month"],
                job.get("browse_category"),
                job.get("country_or_group"),
                now,
            ),
        )
        inserted += cur.rowcount
    return inserted


def list_jobs(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...] = ("pending", "failed"),
    limit: int | None = None,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(statuses))
    sql = (
        f"SELECT * FROM {FOOTYLOGIC_JOBS_TABLE} "
        f"WHERE status IN ({placeholders}) "
        f"ORDER BY month, competition_id, season_id"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, statuses).fetchall())


def mark_job_started(conn: sqlite3.Connection, job_key: str) -> None:
    now = _utc_now()
    conn.execute(
        f"""
        UPDATE {FOOTYLOGIC_JOBS_TABLE}
        SET status='running', attempts=attempts+1, started_at=?, updated_at=?, last_error=NULL
        WHERE job_key=?
        """,
        (now, now, job_key),
    )


def mark_job_finished(
    conn: sqlite3.Connection,
    job_key: str,
    *,
    status: str,
    http_status: int | None = None,
    events_raw: int | None = None,
    in_range: int | None = None,
    upserted: int | None = None,
    quarantined: int | None = None,
    last_error: str | None = None,
) -> None:
    now = _utc_now()
    conn.execute(
        f"""
        UPDATE {FOOTYLOGIC_JOBS_TABLE}
        SET status=?, http_status=?, events_raw=?, in_range=?, upserted=?,
            quarantined=?, last_error=?, finished_at=?, updated_at=?
        WHERE job_key=?
        """,
        (
            status,
            http_status,
            events_raw,
            in_range,
            upserted,
            quarantined,
            last_error,
            now,
            now,
            job_key,
        ),
    )


def store_raw_month(
    conn: sqlite3.Connection,
    *,
    job_key: str,
    competition_id: str,
    season_id: str,
    month: str,
    http_status: int | None,
    business_ok: bool,
    message: str | None,
    raw_json: str,
) -> None:
    now = _utc_now()
    conn.execute(
        f"""
        INSERT INTO {FOOTYLOGIC_RAW_TABLE} (
            job_key, competition_id, season_id, month, http_status, business_ok,
            message, raw_json, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_key) DO UPDATE SET
            http_status=excluded.http_status,
            business_ok=excluded.business_ok,
            message=excluded.message,
            raw_json=excluded.raw_json,
            fetched_at=excluded.fetched_at
        """,
        (
            job_key,
            competition_id,
            season_id,
            month,
            http_status,
            1 if business_ok else 0,
            message,
            raw_json,
            now,
        ),
    )


def _store_score_2h_derivation(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    score_2h_home: int,
    score_2h_away: int,
    now: str,
) -> None:
    evidence = dict(FT_90MIN_EVIDENCE)
    evidence["class_code"] = "A"
    conn.execute(
        """
        INSERT INTO footylogic_score_derivations (
            source_key, field, value_home, value_away, formula, class_code,
            evidence_json, source_fields_json, calculated_at
        ) VALUES (?, 'score_2h', ?, ?, 'score_ft - score_ht', 'A', ?, ?, ?)
        ON CONFLICT(source_key, field) DO UPDATE SET
            value_home=excluded.value_home,
            value_away=excluded.value_away,
            formula=excluded.formula,
            class_code=excluded.class_code,
            evidence_json=excluded.evidence_json,
            source_fields_json=excluded.source_fields_json,
            calculated_at=excluded.calculated_at
        """,
        (
            source_key,
            score_2h_home,
            score_2h_away,
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            json.dumps(FT_90MIN_EVIDENCE["source_fields"], ensure_ascii=False),
            now,
        ),
    )


def upsert_matches(conn: sqlite3.Connection, rows: Iterable[ParsedFootylogicMatch]) -> int:
    """
    Upsert identity-ok rows.
    Missing scores do not clear existing non-null scores.
    """
    now = _utc_now()
    n = 0
    for row in rows:
        if not row.identity_ok:
            continue
        conn.execute(
            f"""
            INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
                source_key, source, competition_id, season_id, month, competition_name,
                home_team_id, away_team_id, home_team_name, away_team_name,
                kickoff_utc, kickoff_hkt, match_date_hkt,
                score_ft_home, score_ft_away, score_ht_home, score_ht_away,
                score_2h_home, score_2h_away, score_et_home, score_et_away,
                score_pens_home, score_pens_away,
                final_odds_home, final_odds_draw, final_odds_away,
                final_odds_timestamp, final_odds_source,
                has_score, score_complete, result_confirm_status,
                event_json, source_last_updated, created_at, updated_at
            ) VALUES (
                ?, 'footylogic', ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?
            )
            ON CONFLICT(source_key) DO UPDATE SET
                competition_name=COALESCE(excluded.competition_name, {FOOTYLOGIC_MATCHES_TABLE}.competition_name),
                home_team_name=COALESCE(excluded.home_team_name, {FOOTYLOGIC_MATCHES_TABLE}.home_team_name),
                away_team_name=COALESCE(excluded.away_team_name, {FOOTYLOGIC_MATCHES_TABLE}.away_team_name),
                kickoff_hkt=excluded.kickoff_hkt,
                match_date_hkt=excluded.match_date_hkt,
                score_ft_home=CASE
                    WHEN excluded.score_ft_home IS NOT NULL THEN excluded.score_ft_home
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_ft_home END,
                score_ft_away=CASE
                    WHEN excluded.score_ft_away IS NOT NULL THEN excluded.score_ft_away
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_ft_away END,
                score_ht_home=CASE
                    WHEN excluded.score_ht_home IS NOT NULL THEN excluded.score_ht_home
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_ht_home END,
                score_ht_away=CASE
                    WHEN excluded.score_ht_away IS NOT NULL THEN excluded.score_ht_away
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_ht_away END,
                score_2h_home=CASE
                    WHEN excluded.score_2h_home IS NOT NULL THEN excluded.score_2h_home
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_2h_home END,
                score_2h_away=CASE
                    WHEN excluded.score_2h_away IS NOT NULL THEN excluded.score_2h_away
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_2h_away END,
                score_et_home=CASE
                    WHEN excluded.score_et_home IS NOT NULL THEN excluded.score_et_home
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_et_home END,
                score_et_away=CASE
                    WHEN excluded.score_et_away IS NOT NULL THEN excluded.score_et_away
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_et_away END,
                score_pens_home=CASE
                    WHEN excluded.score_pens_home IS NOT NULL THEN excluded.score_pens_home
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_pens_home END,
                score_pens_away=CASE
                    WHEN excluded.score_pens_away IS NOT NULL THEN excluded.score_pens_away
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_pens_away END,
                final_odds_home=CASE
                    WHEN excluded.final_odds_home IS NOT NULL THEN excluded.final_odds_home
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.final_odds_home END,
                final_odds_draw=CASE
                    WHEN excluded.final_odds_draw IS NOT NULL THEN excluded.final_odds_draw
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.final_odds_draw END,
                final_odds_away=CASE
                    WHEN excluded.final_odds_away IS NOT NULL THEN excluded.final_odds_away
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.final_odds_away END,
                final_odds_timestamp=CASE
                    WHEN excluded.final_odds_timestamp IS NOT NULL THEN excluded.final_odds_timestamp
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.final_odds_timestamp END,
                final_odds_source=CASE
                    WHEN excluded.final_odds_source IS NOT NULL THEN excluded.final_odds_source
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.final_odds_source END,
                has_score=CASE
                    WHEN excluded.has_score=1 THEN 1
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.has_score END,
                score_complete=CASE
                    WHEN excluded.score_complete=1 THEN 1
                    ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_complete END,
                event_json=excluded.event_json,
                source_last_updated=COALESCE(excluded.source_last_updated, {FOOTYLOGIC_MATCHES_TABLE}.source_last_updated),
                updated_at=excluded.updated_at
            """,
            (
                row.source_key,
                row.competition_id,
                row.season_id,
                row.month,
                row.competition_name,
                row.home_team_id,
                row.away_team_id,
                row.home_team_name,
                row.away_team_name,
                row.kickoff_utc,
                row.kickoff_hkt,
                row.match_date_hkt,
                row.score_ft_home,
                row.score_ft_away,
                row.score_ht_home,
                row.score_ht_away,
                row.score_2h_home,
                row.score_2h_away,
                row.score_et_home,
                row.score_et_away,
                row.score_pens_home,
                row.score_pens_away,
                row.final_odds_home,
                row.final_odds_draw,
                row.final_odds_away,
                row.final_odds_timestamp,
                row.final_odds_source,
                1 if row.has_score else 0,
                1 if row.score_complete else 0,
                row.result_confirm_status,
                row.event_json,
                row.source_last_updated,
                now,
                now,
            ),
        )
        if row.score_2h_home is not None and row.score_2h_away is not None:
            # Only record derivation when we computed 2H (class A). Never invent.
            if (
                classify_score_2h_eligibility(
                    score_ft_home=row.score_ft_home,
                    score_ft_away=row.score_ft_away,
                    score_ht_home=row.score_ht_home,
                    score_ht_away=row.score_ht_away,
                )
                == "A"
            ):
                _store_score_2h_derivation(
                    conn,
                    source_key=row.source_key,
                    score_2h_home=row.score_2h_home,
                    score_2h_away=row.score_2h_away,
                    now=now,
                )
        n += 1
    return n


def insert_quarantine(
    conn: sqlite3.Connection,
    rows: Iterable[ParsedFootylogicMatch],
    *,
    job_key: str,
) -> int:
    now = _utc_now()
    n = 0
    for row in rows:
        conn.execute(
            f"""
            INSERT INTO {FOOTYLOGIC_QUARANTINE_TABLE} (
                job_key, reason, competition_id, season_id, month, kickoff_utc,
                home_team_id, away_team_id, home_team_name, away_team_name,
                event_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_key,
                row.quarantine_reason or "unknown",
                row.competition_id,
                row.season_id,
                row.month,
                row.kickoff_utc,
                row.home_team_id,
                row.away_team_id,
                row.home_team_name,
                row.away_team_name,
                row.event_json,
                now,
            ),
        )
        n += 1
    return n
