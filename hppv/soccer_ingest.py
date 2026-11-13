from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hppv.footylogic_ingest import ensure_footylogic_schema
from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE
from hppv.schema import (
    SOCCER_DAILY_REMOVED_TABLE,
    SOCCER_DAILY_TABLE,
    SOCCER_SYNC_STATE_TABLE,
)
from hppv.soccer_db import get_connection, table_exists

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")
REQUEST_KEY_MATCHES_AND_ODDS = "matches_and_odds"
REQUEST_KEY_UPDATE_ODDS_AND_RESULTS = "update_odds_and_results"
REQUEST_KEY_MATCH_RESULTS = "match_results"
REQUEST_KEY_MATCH_RESULTS_BY_YEAR = "match_results_by_year"
REQUEST_KEY_UPDATE_RESULTS = "update_results"

TOMBSTONE_REASON_OFFICIAL_FT = "official_ft_to_history"
TOMBSTONE_REASON_OFFICIAL_VOID = "official_void_to_history"
HKJC_SOURCE = "hkjc"
HKJC_RESULT_CONFIRM_STATUS = "hkjc_official_ft_confirmed"
# Official MATCHVOIDED → history marker (scores cleared; HT/FT display as 失效).
HKJC_RESULT_VOID_STATUS = "hkjc_match_voided"

# Official frontend ch(e, n): resultType==1 goals; stageId 3=HT, 5=FT (not 9/ET this round).
RESULT_TYPE_GOALS = 1
STAGE_ID_HT = 3
STAGE_ID_FT = 5
STATUS_MATCHENDED = "MATCHENDED"
STATUS_INPLAYMATCHENDED = "INPLAYMATCHENDED"
STATUS_MATCHVOIDED = "MATCHVOIDED"

# Verified from HKJC foPools HAD combinations[].str (not array order).
HAD_STR_HOME = "H"
HAD_STR_DRAW = "D"
HAD_STR_AWAY = "A"


class SoccerIngestError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ParsedMatch:
    match_id: str
    match_code: str | None
    match_date: str
    kickoff_at: str
    competition_code: str | None
    competition_name_zh: str | None
    home_team: str
    away_team: str
    match_status: str | None
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    odds_status: str | None
    has_complete_had: bool


@dataclass(frozen=True, slots=True)
class IngestResult:
    code: str  # success | skipped_* | failed_*
    processed: int
    inserted: int
    updated: int
    had_complete: int
    had_incomplete: int
    last_success_at: str | None
    detail: str = ""
    skipped_missing: int = 0
    results_applied: bool = False
    request_key: str = REQUEST_KEY_MATCHES_AND_ODDS
    ht_applied: int = 0
    ft_applied: int = 0
    sh_applied: int = 0
    skipped_status: int = 0
    skipped_unconfirmed: int = 0
    skipped_insufficient: int = 0
    match_num_total: int | None = None
    matches_returned: int | None = None
    # Odds-update diagnostics (optional; default 0 for other ingest paths).
    odds_changed: int = 0
    odds_unchanged: int = 0
    # update_results / tombstone diagnostics.
    daily_deleted: int = 0
    skipped_tombstone: int = 0
    skipped_no_kickoff: int = 0
    skipped_not_in_daily: int = 0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


def format_hkt_display(utc_iso: str) -> str:
    """Format stored UTC ISO to Hong Kong YYYY-MM-DD HH:mm:ss."""
    text = utc_iso.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(HKT)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def parse_kickoff_hkt(kickoff_raw: Any) -> tuple[str, str]:
    """
    Parse source kickOffTime into (kickoff_at +08:00 ISO, match_date YYYY-MM-DD).
    Does not use matchDate field.
    """
    if kickoff_raw is None or str(kickoff_raw).strip() == "":
        raise SoccerIngestError("invalid_match", "kickOffTime missing")
    text = str(kickoff_raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise SoccerIngestError("invalid_match", f"invalid kickOffTime: {kickoff_raw!r}") from e
    if dt.tzinfo is None:
        raise SoccerIngestError("invalid_match", f"kickOffTime missing timezone: {kickoff_raw!r}")
    local = dt.astimezone(HKT)
    kickoff_at = local.isoformat()
    match_date = local.date().isoformat()
    return kickoff_at, match_date


def _parse_odds_value(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0:
        return None
    return val


def extract_had_from_match(match: dict[str, Any]) -> tuple[dict[str, float] | None, str | None]:
    """
    Extract complete HAD odds mapped by combination.str H/D/A.

    Returns (odds_map or None, odds_status or None).
    Raises SoccerIngestError(code='had_ambiguous') if multiple HAD pools/lines cannot be resolved.
    """
    pools = [p for p in (match.get("foPools") or []) if isinstance(p, dict) and p.get("oddsType") == "HAD"]
    if not pools:
        return None, None
    if len(pools) > 1:
        raise SoccerIngestError(
            "had_ambiguous",
            f"match {match.get('id')!r} has {len(pools)} HAD pools",
        )

    pool = pools[0]
    odds_status = str(pool.get("status")).strip() if pool.get("status") is not None else None
    if odds_status == "":
        odds_status = None

    lines = [ln for ln in (pool.get("lines") or []) if isinstance(ln, dict)]
    if not lines:
        return None, odds_status

    main_lines = [ln for ln in lines if ln.get("main") is True]
    if len(main_lines) == 1:
        line = main_lines[0]
    elif len(lines) == 1:
        line = lines[0]
    else:
        raise SoccerIngestError(
            "had_ambiguous",
            f"match {match.get('id')!r} has ambiguous HAD lines",
        )

    by_str: dict[str, float] = {}
    for comb in line.get("combinations") or []:
        if not isinstance(comb, dict):
            continue
        key = str(comb.get("str") or "").strip()
        if key not in {HAD_STR_HOME, HAD_STR_DRAW, HAD_STR_AWAY}:
            continue
        odds = _parse_odds_value(comb.get("currentOdds"))
        if odds is None:
            continue
        if key in by_str:
            raise SoccerIngestError(
                "had_ambiguous",
                f"match {match.get('id')!r} duplicate HAD option {key}",
            )
        by_str[key] = odds

    if HAD_STR_HOME in by_str and HAD_STR_DRAW in by_str and HAD_STR_AWAY in by_str:
        return {
            "home": by_str[HAD_STR_HOME],
            "draw": by_str[HAD_STR_DRAW],
            "away": by_str[HAD_STR_AWAY],
        }, odds_status
    return None, odds_status


def parse_match_item(raw: Any) -> ParsedMatch:
    if not isinstance(raw, dict):
        raise SoccerIngestError("invalid_match", "match is not an object")

    match_id = str(raw.get("id") or "").strip()
    if not match_id:
        raise SoccerIngestError("invalid_match", "match id missing")

    home = raw.get("homeTeam") if isinstance(raw.get("homeTeam"), dict) else {}
    away = raw.get("awayTeam") if isinstance(raw.get("awayTeam"), dict) else {}
    home_team = str(home.get("name_ch") or "").strip()
    away_team = str(away.get("name_ch") or "").strip()
    if not home_team or not away_team:
        raise SoccerIngestError("invalid_match", f"team name missing for {match_id}")

    kickoff_at, match_date = parse_kickoff_hkt(raw.get("kickOffTime"))

    tourn = raw.get("tournament") if isinstance(raw.get("tournament"), dict) else {}
    competition_code = str(tourn.get("code") or "").strip() or None
    competition_name_zh = str(tourn.get("name_ch") or "").strip() or None
    match_code = str(raw.get("frontEndId") or "").strip() or None
    match_status = str(raw.get("status") or "").strip() or None

    odds_map, odds_status = extract_had_from_match(raw)
    if odds_map:
        return ParsedMatch(
            match_id=match_id,
            match_code=match_code,
            match_date=match_date,
            kickoff_at=kickoff_at,
            competition_code=competition_code,
            competition_name_zh=competition_name_zh,
            home_team=home_team,
            away_team=away_team,
            match_status=match_status,
            home_odds=odds_map["home"],
            draw_odds=odds_map["draw"],
            away_odds=odds_map["away"],
            odds_status=odds_status,
            has_complete_had=True,
        )
    return ParsedMatch(
        match_id=match_id,
        match_code=match_code,
        match_date=match_date,
        kickoff_at=kickoff_at,
        competition_code=competition_code,
        competition_name_zh=competition_name_zh,
        home_team=home_team,
        away_team=away_team,
        match_status=match_status,
        home_odds=None,
        draw_odds=None,
        away_odds=None,
        odds_status=odds_status,
        has_complete_had=False,
    )


def parse_matches_payload(response_obj: Any) -> list[ParsedMatch]:
    """
    Validate GraphQL JSON object for ingest.
    Requires: no errors, data.matches is a list.
    """
    if not isinstance(response_obj, dict):
        raise SoccerIngestError("structure_mismatch", "response is not an object")
    if response_obj.get("errors"):
        raise SoccerIngestError("graphql_errors", "response contains GraphQL errors")
    data = response_obj.get("data")
    if not isinstance(data, dict):
        raise SoccerIngestError("structure_mismatch", "data missing")
    matches = data.get("matches")
    if not isinstance(matches, list):
        raise SoccerIngestError("structure_mismatch", "data.matches is not an array")

    parsed: list[ParsedMatch] = []
    seen: set[str] = set()
    for item in matches:
        m = parse_match_item(item)
        if m.match_id in seen:
            raise SoccerIngestError("duplicate_id", f"duplicate match_id {m.match_id}")
        seen.add(m.match_id)
        parsed.append(m)
    return parsed


def get_last_success_at(db_path: Path, request_key: str = REQUEST_KEY_MATCHES_AND_ODDS) -> str | None:
    if not db_path.is_file():
        return None
    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_SYNC_STATE_TABLE):
            return None
        row = conn.execute(
            "SELECT last_success_at FROM soccer_sync_state WHERE request_key = ?;",
            (request_key,),
        ).fetchone()
    if not row:
        return None
    return str(row["last_success_at"])


def ingest_matches_and_odds(
    db_path: Path,
    response_obj: Any,
    *,
    request_key: str = REQUEST_KEY_MATCHES_AND_ODDS,
    now_utc: str | None = None,
) -> IngestResult:
    """
    Parse and upsert matches in one transaction with sync_state update.
    """
    if not db_path.is_file():
        raise SoccerIngestError("db_missing", f"database missing: {db_path}")

    parsed = parse_matches_payload(response_obj)
    if not parsed:
        return IngestResult(
            code="skipped_empty",
            processed=0,
            inserted=0,
            updated=0,
            had_complete=0,
            had_incomplete=0,
            last_success_at=get_last_success_at(db_path, request_key),
            detail="empty matches",
        )

    stamp = now_utc or utc_now_iso()
    had_complete = sum(1 for m in parsed if m.has_complete_had)
    had_incomplete = len(parsed) - had_complete

    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_DAILY_TABLE):
            raise SoccerIngestError("table_missing", "soccer_daily missing; run init")
        if not table_exists(conn, SOCCER_SYNC_STATE_TABLE):
            raise SoccerIngestError(
                "table_missing",
                "soccer_sync_state missing; run: python -m hppv.soccer_db init",
            )

        inserted = 0
        updated = 0
        skipped_tombstone = 0
        tombstone_ready = table_exists(conn, SOCCER_DAILY_REMOVED_TABLE)
        try:
            conn.execute("BEGIN;")
            for m in parsed:
                existing = conn.execute(
                    "SELECT match_id, created_at, home_odds, draw_odds, away_odds, "
                    "odds_fetched_at, odds_status FROM soccer_daily WHERE match_id = ?;",
                    (m.match_id,),
                ).fetchone()

                if existing is None:
                    if tombstone_ready:
                        removed = conn.execute(
                            f"SELECT 1 FROM {SOCCER_DAILY_REMOVED_TABLE} "
                            "WHERE match_id = ? LIMIT 1;",
                            (m.match_id,),
                        ).fetchone()
                        if removed is not None:
                            skipped_tombstone += 1
                            continue
                    odds_fetched = stamp if m.has_complete_had else None
                    conn.execute(
                        """
                        INSERT INTO soccer_daily (
                            match_id, match_code, match_date, kickoff_at,
                            competition_code, competition_name_zh,
                            home_team, away_team, match_status,
                            home_odds, draw_odds, away_odds,
                            odds_status, odds_fetched_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            m.match_id,
                            m.match_code,
                            m.match_date,
                            m.kickoff_at,
                            m.competition_code,
                            m.competition_name_zh,
                            m.home_team,
                            m.away_team,
                            m.match_status,
                            m.home_odds,
                            m.draw_odds,
                            m.away_odds,
                            m.odds_status,
                            odds_fetched,
                            stamp,
                            stamp,
                        ),
                    )
                    inserted += 1
                else:
                    if m.has_complete_had:
                        home_odds, draw_odds, away_odds = m.home_odds, m.draw_odds, m.away_odds
                        odds_fetched = stamp
                        odds_status = m.odds_status
                    else:
                        home_odds = existing["home_odds"]
                        draw_odds = existing["draw_odds"]
                        away_odds = existing["away_odds"]
                        odds_fetched = existing["odds_fetched_at"]
                        # Update odds_status only when source provides a clear HAD pool status.
                        odds_status = (
                            m.odds_status
                            if m.odds_status is not None
                            else existing["odds_status"]
                        )

                    conn.execute(
                        """
                        UPDATE soccer_daily SET
                            match_code = ?,
                            match_date = ?,
                            kickoff_at = ?,
                            competition_code = ?,
                            competition_name_zh = ?,
                            home_team = ?,
                            away_team = ?,
                            match_status = ?,
                            home_odds = ?,
                            draw_odds = ?,
                            away_odds = ?,
                            odds_status = ?,
                            odds_fetched_at = ?,
                            updated_at = ?
                        WHERE match_id = ?;
                        """,
                        (
                            m.match_code,
                            m.match_date,
                            m.kickoff_at,
                            m.competition_code,
                            m.competition_name_zh,
                            m.home_team,
                            m.away_team,
                            m.match_status,
                            home_odds,
                            draw_odds,
                            away_odds,
                            odds_status,
                            odds_fetched,
                            stamp,
                            m.match_id,
                        ),
                    )
                    updated += 1

            conn.execute(
                """
                INSERT INTO soccer_sync_state (request_key, last_success_at)
                VALUES (?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    last_success_at = excluded.last_success_at;
                """,
                (request_key, stamp),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.exception("soccer ingest transaction failed: %s", e)
            raise SoccerIngestError("transaction_failed", str(e)) from e

    return IngestResult(
        code="success",
        processed=len(parsed),
        inserted=inserted,
        updated=updated,
        had_complete=had_complete,
        had_incomplete=had_incomplete,
        last_success_at=stamp,
        skipped_missing=0,
        results_applied=False,
        request_key=request_key,
        skipped_tombstone=skipped_tombstone,
    )


def _norm_odds_for_compare(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return round(num, 6)


def _odds_fields_equal(
    home: Any,
    draw: Any,
    away: Any,
    status: Any,
    *,
    home2: Any,
    draw2: Any,
    away2: Any,
    status2: Any,
) -> bool:
    st1 = str(status).strip() if status is not None and str(status).strip() != "" else None
    st2 = str(status2).strip() if status2 is not None and str(status2).strip() != "" else None
    return (
        _norm_odds_for_compare(home) == _norm_odds_for_compare(home2)
        and _norm_odds_for_compare(draw) == _norm_odds_for_compare(draw2)
        and _norm_odds_for_compare(away) == _norm_odds_for_compare(away2)
        and st1 == st2
    )


def update_odds_for_existing_matches(
    db_path: Path,
    response_obj: Any,
    *,
    request_key: str = REQUEST_KEY_UPDATE_ODDS_AND_RESULTS,
    now_utc: str | None = None,
) -> IngestResult:
    """
    Update HAD odds for existing soccer_daily rows only.
    Does not insert new matches; does not overwrite basic match fields;
    does not apply official results (not connected yet).
    Incomplete HAD never blanks previously stored valid odds.
    """
    if not db_path.is_file():
        raise SoccerIngestError("db_missing", f"database missing: {db_path}")

    parsed = parse_matches_payload(response_obj)
    if not parsed:
        return IngestResult(
            code="skipped_empty",
            processed=0,
            inserted=0,
            updated=0,
            had_complete=0,
            had_incomplete=0,
            last_success_at=get_last_success_at(db_path, request_key),
            detail="empty matches",
            skipped_missing=0,
            results_applied=False,
            request_key=request_key,
        )

    stamp = now_utc or utc_now_iso()
    had_complete = 0
    had_incomplete = 0
    skipped_missing = 0
    updated = 0
    odds_changed = 0
    odds_unchanged = 0

    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_DAILY_TABLE):
            raise SoccerIngestError("table_missing", "soccer_daily missing; run init")
        if not table_exists(conn, SOCCER_SYNC_STATE_TABLE):
            raise SoccerIngestError(
                "table_missing",
                "soccer_sync_state missing; run: python -m hppv.soccer_db init",
            )

        try:
            conn.execute("BEGIN;")
            for m in parsed:
                existing = conn.execute(
                    "SELECT match_id, home_odds, draw_odds, away_odds, "
                    "odds_fetched_at, odds_status FROM soccer_daily WHERE match_id = ?;",
                    (m.match_id,),
                ).fetchone()
                if existing is None:
                    skipped_missing += 1
                    continue

                if m.has_complete_had:
                    had_complete += 1
                    home_odds, draw_odds, away_odds = m.home_odds, m.draw_odds, m.away_odds
                    odds_fetched = stamp
                    odds_status = m.odds_status
                else:
                    had_incomplete += 1
                    home_odds = existing["home_odds"]
                    draw_odds = existing["draw_odds"]
                    away_odds = existing["away_odds"]
                    odds_fetched = existing["odds_fetched_at"]
                    odds_status = (
                        m.odds_status
                        if m.odds_status is not None
                        else existing["odds_status"]
                    )

                same = _odds_fields_equal(
                    home_odds,
                    draw_odds,
                    away_odds,
                    odds_status,
                    home2=existing["home_odds"],
                    draw2=existing["draw_odds"],
                    away2=existing["away_odds"],
                    status2=existing["odds_status"],
                )
                if same:
                    odds_unchanged += 1
                else:
                    odds_changed += 1

                conn.execute(
                    """
                    UPDATE soccer_daily SET
                        home_odds = ?,
                        draw_odds = ?,
                        away_odds = ?,
                        odds_status = ?,
                        odds_fetched_at = ?,
                        updated_at = ?
                    WHERE match_id = ?;
                    """,
                    (
                        home_odds,
                        draw_odds,
                        away_odds,
                        odds_status,
                        odds_fetched,
                        stamp,
                        m.match_id,
                    ),
                )
                updated += 1

            if updated == 0:
                conn.rollback()
                return IngestResult(
                    code="skipped_no_existing",
                    processed=len(parsed),
                    inserted=0,
                    updated=0,
                    had_complete=had_complete,
                    had_incomplete=had_incomplete,
                    last_success_at=get_last_success_at(db_path, request_key),
                    detail="no existing match_id updated",
                    skipped_missing=skipped_missing,
                    results_applied=False,
                    request_key=request_key,
                    odds_changed=0,
                    odds_unchanged=0,
                )

            conn.execute(
                """
                INSERT INTO soccer_sync_state (request_key, last_success_at)
                VALUES (?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    last_success_at = excluded.last_success_at;
                """,
                (request_key, stamp),
            )
            conn.commit()
        except SoccerIngestError:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            logger.exception("soccer odds-update transaction failed: %s", e)
            raise SoccerIngestError("transaction_failed", str(e)) from e

    return IngestResult(
        code="success",
        processed=len(parsed),
        inserted=0,
        updated=updated,
        had_complete=had_complete,
        had_incomplete=had_incomplete,
        last_success_at=stamp,
        skipped_missing=skipped_missing,
        results_applied=False,
        request_key=request_key,
        odds_changed=odds_changed,
        odds_unchanged=odds_unchanged,
    )


@dataclass(frozen=True, slots=True)
class OfficialScorePair:
    home: int
    away: int


@dataclass(frozen=True, slots=True)
class ParsedOfficialResult:
    match_id: str
    source_status: str
    ht: OfficialScorePair | None
    ft: OfficialScorePair | None
    sh: OfficialScorePair | None


def parse_non_negative_goal(raw: Any, *, field: str) -> int:
    """Official goal count: non-negative integer only (0 is valid). Reject bool/float/str."""
    if isinstance(raw, bool) or raw is None:
        raise SoccerIngestError("invalid_result", f"{field} invalid type: {raw!r}")
    if isinstance(raw, int):
        if raw < 0:
            raise SoccerIngestError("invalid_result", f"{field} negative: {raw!r}")
        return raw
    raise SoccerIngestError("invalid_result", f"{field} not an integer: {raw!r}")


def select_official_stage_score(
    results: list[Any],
    *,
    stage_id: int,
    match_id: str,
) -> OfficialScorePair | None:
    """
    Official ch()-compatible selection for one stage:
    resultType==1, resultConfirmType>1, payoutConfirmed, stageId==stage_id;
    then max sequence; same max sequence must not conflict.
    """
    if not isinstance(results, list):
        raise SoccerIngestError("structure_mismatch", f"results not a list match_id={match_id}")

    candidates: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            raise SoccerIngestError("structure_mismatch", f"result row not object match_id={match_id}")
        if row.get("resultType") != RESULT_TYPE_GOALS:
            continue
        confirm = row.get("resultConfirmType")
        if not isinstance(confirm, int) or confirm <= 1:
            continue
        if row.get("payoutConfirmed") is not True:
            continue
        if row.get("stageId") != stage_id:
            continue
        candidates.append(row)

    if not candidates:
        return None

    sequences: list[int] = []
    for row in candidates:
        seq = row.get("sequence")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise SoccerIngestError(
                "invalid_result",
                f"invalid sequence match_id={match_id} stageId={stage_id} seq={seq!r}",
            )
        sequences.append(seq)
    max_seq = max(sequences)
    at_max = [row for row in candidates if row.get("sequence") == max_seq]
    pairs: set[tuple[int, int]] = set()
    for row in at_max:
        home = parse_non_negative_goal(row.get("homeResult"), field="homeResult")
        away = parse_non_negative_goal(row.get("awayResult"), field="awayResult")
        pairs.add((home, away))
    if len(pairs) != 1:
        raise SoccerIngestError(
            "result_conflict",
            f"conflicting scores at max sequence match_id={match_id} "
            f"stageId={stage_id} sequence={max_seq} pairs={sorted(pairs)}",
        )
    home, away = next(iter(pairs))
    return OfficialScorePair(home=home, away=away)


def compute_second_half(
    ht: OfficialScorePair,
    ft: OfficialScorePair,
    *,
    match_id: str,
) -> OfficialScorePair:
    sh_home = ft.home - ht.home
    sh_away = ft.away - ht.away
    if sh_home < 0 or sh_away < 0:
        raise SoccerIngestError(
            "invalid_result",
            f"FT < HT match_id={match_id} ht={ht.home}-{ht.away} ft={ft.home}-{ft.away}",
        )
    return OfficialScorePair(home=sh_home, away=sh_away)


def parse_official_result_item(raw: Any) -> ParsedOfficialResult:
    if not isinstance(raw, dict):
        raise SoccerIngestError("structure_mismatch", "match result item is not an object")
    mid_raw = raw.get("id")
    if mid_raw is None or str(mid_raw).strip() == "":
        raise SoccerIngestError("invalid_result", "match id missing")
    match_id = str(mid_raw).strip()
    status = str(raw.get("status") or "").strip()
    results = raw.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        raise SoccerIngestError("structure_mismatch", f"results not a list match_id={match_id}")

    # Explicit MATCHVOIDED only — never infer void from empty results.
    if status == STATUS_MATCHVOIDED:
        return ParsedOfficialResult(
            match_id=match_id,
            source_status=status,
            ht=None,
            ft=None,
            sh=None,
        )

    ht = select_official_stage_score(results, stage_id=STAGE_ID_HT, match_id=match_id)
    ft = select_official_stage_score(results, stage_id=STAGE_ID_FT, match_id=match_id)
    sh: OfficialScorePair | None = None
    if ht is not None and ft is not None:
        sh = compute_second_half(ht, ft, match_id=match_id)
    return ParsedOfficialResult(
        match_id=match_id,
        source_status=status,
        ht=ht,
        ft=ft,
        sh=sh,
    )


def _team_display_name(team_obj: Any) -> str | None:
    """Prefer name_ch; if blank, use real name_en. Never invent placeholders."""
    if not isinstance(team_obj, dict):
        return None
    name_ch = str(team_obj.get("name_ch") or "").strip()
    if name_ch:
        return name_ch
    name_en = str(team_obj.get("name_en") or "").strip()
    if name_en:
        return name_en
    return None


def extract_history_create_fields(
    raw: dict[str, Any],
    *,
    match_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """
    Map matchResults item → soccer_daily create fields.
    Returns (fields, missing_required_field_names).
    match_code may be NULL when frontEndId is blank (do not invent).
    """
    missing: list[str] = []
    home_team = _team_display_name(raw.get("homeTeam"))
    away_team = _team_display_name(raw.get("awayTeam"))
    if not home_team:
        missing.append("homeTeam.name_ch|name_en")
    if not away_team:
        missing.append("awayTeam.name_ch|name_en")

    kickoff_at: str | None = None
    match_date: str | None = None
    if raw.get("kickOffTime") is None or str(raw.get("kickOffTime") or "").strip() == "":
        missing.append("kickOffTime")
    else:
        try:
            kickoff_at, match_date = parse_kickoff_hkt(raw.get("kickOffTime"))
        except SoccerIngestError:
            missing.append("kickOffTime")

    if missing:
        return None, missing

    tourn = raw.get("tournament") if isinstance(raw.get("tournament"), dict) else {}
    competition_code = str(tourn.get("code") or "").strip() or None
    competition_name_zh = str(tourn.get("name_ch") or "").strip() or None
    match_code = str(raw.get("frontEndId") or "").strip() or None
    match_status = str(raw.get("status") or "").strip() or None

    assert home_team is not None and away_team is not None
    assert kickoff_at is not None and match_date is not None
    return {
        "match_id": match_id,
        "match_code": match_code,
        "match_date": match_date,
        "kickoff_at": kickoff_at,
        "competition_code": competition_code,
        "competition_name_zh": competition_name_zh,
        "home_team": home_team,
        "away_team": away_team,
        "match_status": match_status,
    }, []


def _extract_match_results_envelope(
    response_obj: Any,
) -> tuple[list[Any], int | None, dict[str, Any] | None]:
    """Return (matches list, matchNumByDate.total or None, variables hint unused)."""
    if not isinstance(response_obj, dict):
        raise SoccerIngestError("structure_mismatch", "response is not an object")
    if response_obj.get("errors"):
        raise SoccerIngestError("graphql_errors", "response contains GraphQL errors")
    data = response_obj.get("data")
    if not isinstance(data, dict):
        raise SoccerIngestError("structure_mismatch", "data missing")
    matches = data.get("matches")
    if not isinstance(matches, list):
        raise SoccerIngestError("structure_mismatch", "data.matches is not an array")
    total: int | None = None
    num = data.get("matchNumByDate")
    if isinstance(num, dict) and "total" in num:
        raw_total = num.get("total")
        if isinstance(raw_total, bool) or not isinstance(raw_total, int):
            raise SoccerIngestError("structure_mismatch", f"matchNumByDate.total invalid: {raw_total!r}")
        total = raw_total
    return matches, total, None


def assert_match_results_completeness(
    *,
    matches_returned: int,
    match_num_total: int | None,
    variables: dict[str, Any] | None,
) -> None:
    """
    When endIndex==0 (verified full short-range fetch), require matches == total.
    Other paging modes must not claim full success if totals disagree with a full fetch.
    """
    if match_num_total is None:
        return
    end_index = None
    if isinstance(variables, dict) and "endIndex" in variables:
        end_index = variables.get("endIndex")
    if end_index == 0:
        if matches_returned != match_num_total:
            raise SoccerIngestError(
                "incomplete_results",
                f"matches={matches_returned} != matchNumByDate.total={match_num_total} "
                f"(endIndex=0 full-range check)",
            )


def ingest_official_match_results(
    db_path: Path,
    response_obj: Any,
    *,
    request_key: str = REQUEST_KEY_MATCH_RESULTS,
    now_utc: str | None = None,
    variables: dict[str, Any] | None = None,
    allow_insert: bool = False,
) -> IngestResult:
    """
    Apply official HT/FT/(derived 2H) for MATCHENDED rows.

    - Existing match_id: update confirmed scores only; do not clear missing stages;
      do not overwrite basic fields or odds_*.
    - Unknown match_id: skipped unless allow_insert=True (match_results_by_year),
      then INSERT history row from real matchResults fields when sufficient.
    - result_status unchanged on update; NULL on insert.
    """
    if not db_path.is_file():
        raise SoccerIngestError("db_missing", f"database missing: {db_path}")

    matches_raw, match_num_total, _ = _extract_match_results_envelope(response_obj)
    matches_returned = len(matches_raw)
    assert_match_results_completeness(
        matches_returned=matches_returned,
        match_num_total=match_num_total,
        variables=variables,
    )

    if matches_returned == 0:
        return IngestResult(
            code="skipped_empty",
            processed=0,
            inserted=0,
            updated=0,
            had_complete=0,
            had_incomplete=0,
            last_success_at=get_last_success_at(db_path, request_key),
            detail="empty matches",
            skipped_missing=0,
            results_applied=False,
            request_key=request_key,
            match_num_total=match_num_total,
            matches_returned=0,
        )

    paired: list[tuple[ParsedOfficialResult, dict[str, Any]]] = []
    seen: set[str] = set()
    for item in matches_raw:
        if not isinstance(item, dict):
            raise SoccerIngestError("structure_mismatch", "match result item is not an object")
        parsed = parse_official_result_item(item)
        if parsed.match_id in seen:
            raise SoccerIngestError("duplicate_id", f"duplicate match_id {parsed.match_id}")
        seen.add(parsed.match_id)
        paired.append((parsed, item))

    stamp = now_utc or utc_now_iso()
    skipped_missing = 0
    skipped_status = 0
    skipped_unconfirmed = 0
    skipped_insufficient = 0
    insufficient_details: list[str] = []
    ht_applied = 0
    ft_applied = 0
    sh_applied = 0
    updated = 0
    inserted = 0
    pending_updates: list[tuple[ParsedOfficialResult, dict[str, Any]]] = []
    pending_inserts: list[tuple[ParsedOfficialResult, dict[str, Any], dict[str, Any]]] = []

    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_DAILY_TABLE):
            raise SoccerIngestError("table_missing", "soccer_daily missing; run init")
        if not table_exists(conn, SOCCER_SYNC_STATE_TABLE):
            raise SoccerIngestError(
                "table_missing",
                "soccer_sync_state missing; run: python -m hppv.soccer_db init",
            )

        existing_ids = {
            str(r["match_id"])
            for r in conn.execute("SELECT match_id FROM soccer_daily;").fetchall()
        }

        for p, raw in paired:
            if p.source_status == STATUS_INPLAYMATCHENDED:
                skipped_status += 1
                continue
            if p.source_status != STATUS_MATCHENDED:
                skipped_status += 1
                continue
            if p.ht is None and p.ft is None:
                skipped_unconfirmed += 1
                continue

            if p.match_id not in existing_ids:
                if not allow_insert:
                    skipped_missing += 1
                    continue
                create_fields, missing = extract_history_create_fields(raw, match_id=p.match_id)
                if create_fields is None:
                    skipped_insufficient += 1
                    insufficient_details.append(
                        f"{p.match_id}: missing {','.join(missing)}"
                    )
                    continue
                score_vals = {
                    "score_ht_home": p.ht.home if p.ht is not None else None,
                    "score_ht_away": p.ht.away if p.ht is not None else None,
                    "score_ft_home": p.ft.home if p.ft is not None else None,
                    "score_ft_away": p.ft.away if p.ft is not None else None,
                    "score_2h_home": p.sh.home if p.sh is not None else None,
                    "score_2h_away": p.sh.away if p.sh is not None else None,
                    "will_ht": p.ht is not None,
                    "will_ft": p.ft is not None,
                    "will_sh": p.sh is not None,
                }
                pending_inserts.append((p, create_fields, score_vals))
                continue

            row = conn.execute(
                """
                SELECT score_ht_home, score_ht_away, score_ft_home, score_ft_away,
                       score_2h_home, score_2h_away, result_fetched_at
                FROM soccer_daily WHERE match_id = ?;
                """,
                (p.match_id,),
            ).fetchone()
            if row is None:
                skipped_missing += 1
                continue

            new_ht_h = row["score_ht_home"]
            new_ht_a = row["score_ht_away"]
            new_ft_h = row["score_ft_home"]
            new_ft_a = row["score_ft_away"]
            new_sh_h = row["score_2h_home"]
            new_sh_a = row["score_2h_away"]
            will_ht = False
            will_ft = False
            will_sh = False

            if p.ht is not None:
                new_ht_h, new_ht_a = p.ht.home, p.ht.away
                will_ht = True
            if p.ft is not None:
                new_ft_h, new_ft_a = p.ft.home, p.ft.away
                will_ft = True
            if p.sh is not None:
                new_sh_h, new_sh_a = p.sh.home, p.sh.away
                will_sh = True

            if not (will_ht or will_ft or will_sh):
                skipped_unconfirmed += 1
                continue

            pending_updates.append(
                (
                    p,
                    {
                        "score_ht_home": new_ht_h,
                        "score_ht_away": new_ht_a,
                        "score_ft_home": new_ft_h,
                        "score_ft_away": new_ft_a,
                        "score_2h_home": new_sh_h,
                        "score_2h_away": new_sh_a,
                        "will_ht": will_ht,
                        "will_ft": will_ft,
                        "will_sh": will_sh,
                    },
                )
            )

        if not pending_updates and not pending_inserts:
            return IngestResult(
                code="skipped_no_existing",
                processed=len(paired),
                inserted=0,
                updated=0,
                had_complete=0,
                had_incomplete=0,
                last_success_at=get_last_success_at(db_path, request_key),
                detail=(
                    f"no MATCHENDED scores applied; "
                    f"skipped_missing={skipped_missing} skipped_status={skipped_status} "
                    f"skipped_unconfirmed={skipped_unconfirmed} "
                    f"skipped_insufficient={skipped_insufficient}; "
                    + ("; ".join(insufficient_details[:20]) if insufficient_details else "")
                ),
                skipped_missing=skipped_missing,
                results_applied=False,
                request_key=request_key,
                skipped_status=skipped_status,
                skipped_unconfirmed=skipped_unconfirmed,
                skipped_insufficient=skipped_insufficient,
                match_num_total=match_num_total,
                matches_returned=matches_returned,
            )

        try:
            conn.execute("BEGIN;")
            for p, create_fields, score_vals in pending_inserts:
                conn.execute(
                    """
                    INSERT INTO soccer_daily (
                        match_id, match_code, match_date, kickoff_at,
                        competition_code, competition_name_zh, home_team, away_team,
                        match_status,
                        home_odds, draw_odds, away_odds, odds_status, odds_fetched_at,
                        score_ht_home, score_ht_away, score_2h_home, score_2h_away,
                        score_ft_home, score_ft_away, score_pen_home, score_pen_away,
                        result_status, result_fetched_at, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?,
                        NULL, NULL, NULL, NULL, NULL,
                        ?, ?, ?, ?,
                        ?, ?, NULL, NULL,
                        NULL, ?, ?, ?
                    );
                    """,
                    (
                        create_fields["match_id"],
                        create_fields["match_code"],
                        create_fields["match_date"],
                        create_fields["kickoff_at"],
                        create_fields["competition_code"],
                        create_fields["competition_name_zh"],
                        create_fields["home_team"],
                        create_fields["away_team"],
                        create_fields["match_status"],
                        score_vals["score_ht_home"],
                        score_vals["score_ht_away"],
                        score_vals["score_2h_home"],
                        score_vals["score_2h_away"],
                        score_vals["score_ft_home"],
                        score_vals["score_ft_away"],
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
                inserted += 1
                if score_vals["will_ht"]:
                    ht_applied += 1
                if score_vals["will_ft"]:
                    ft_applied += 1
                if score_vals["will_sh"]:
                    sh_applied += 1

            for p, vals in pending_updates:
                conn.execute(
                    """
                    UPDATE soccer_daily SET
                        score_ht_home = ?,
                        score_ht_away = ?,
                        score_ft_home = ?,
                        score_ft_away = ?,
                        score_2h_home = ?,
                        score_2h_away = ?,
                        result_fetched_at = ?,
                        updated_at = ?
                    WHERE match_id = ?;
                    """,
                    (
                        vals["score_ht_home"],
                        vals["score_ht_away"],
                        vals["score_ft_home"],
                        vals["score_ft_away"],
                        vals["score_2h_home"],
                        vals["score_2h_away"],
                        stamp,
                        stamp,
                        p.match_id,
                    ),
                )
                updated += 1
                if vals["will_ht"]:
                    ht_applied += 1
                if vals["will_ft"]:
                    ft_applied += 1
                if vals["will_sh"]:
                    sh_applied += 1

            conn.execute(
                """
                INSERT INTO soccer_sync_state (request_key, last_success_at)
                VALUES (?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    last_success_at = excluded.last_success_at;
                """,
                (request_key, stamp),
            )
            conn.commit()
        except SoccerIngestError:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            logger.exception("soccer official-results transaction failed: %s", e)
            raise SoccerIngestError("transaction_failed", str(e)) from e

    detail = (
        f"inserted={inserted} updated={updated} "
        f"ht_applied={ht_applied} ft_applied={ft_applied} sh_applied={sh_applied}; "
        f"skipped_missing={skipped_missing} skipped_status={skipped_status} "
        f"skipped_unconfirmed={skipped_unconfirmed} "
        f"skipped_insufficient={skipped_insufficient}; "
        f"matches_returned={matches_returned} match_num_total={match_num_total}"
    )
    if insufficient_details:
        detail += "; " + "; ".join(insufficient_details[:20])
    return IngestResult(
        code="success",
        processed=len(paired),
        inserted=inserted,
        updated=updated,
        had_complete=ht_applied,
        had_incomplete=ft_applied,
        last_success_at=stamp,
        detail=detail,
        skipped_missing=skipped_missing,
        results_applied=True,
        request_key=request_key,
        ht_applied=ht_applied,
        ft_applied=ft_applied,
        sh_applied=sh_applied,
        skipped_status=skipped_status,
        skipped_unconfirmed=skipped_unconfirmed,
        skipped_insufficient=skipped_insufficient,
        match_num_total=match_num_total,
        matches_returned=matches_returned,
    )


def hkjc_history_source_key(match_id: str) -> str:
    return f"{HKJC_SOURCE}|{match_id}"


def parse_kickoff_utc_and_hkt(kickoff_raw: Any) -> tuple[str, str, str]:
    """
    Parse kickOffTime → (kickoff_utc with Z, kickoff_hkt +08:00 ISO, match_date HKT).
    Raises SoccerIngestError when missing/invalid (caller should skip the match).
    """
    kickoff_hkt, match_date = parse_kickoff_hkt(kickoff_raw)
    text = str(kickoff_raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    assert dt.tzinfo is not None
    utc = dt.astimezone(timezone.utc)
    kickoff_utc = utc.isoformat().replace("+00:00", "Z")
    return kickoff_utc, kickoff_hkt, match_date


def _team_source_id(team_obj: Any) -> str | None:
    if not isinstance(team_obj, dict):
        return None
    tid = team_obj.get("id")
    if tid is None or str(tid).strip() == "":
        return None
    return str(tid).strip()


def soccer_daily_date_bounds(db_path: Path) -> tuple[str, str] | None:
    """Return (min_match_date, max_match_date) or None when soccer_daily is empty."""
    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_DAILY_TABLE):
            return None
        row = conn.execute(
            f"SELECT MIN(match_date) AS min_d, MAX(match_date) AS max_d "
            f"FROM {SOCCER_DAILY_TABLE};"
        ).fetchone()
        if row is None or row["min_d"] is None or row["max_d"] is None:
            return None
        return str(row["min_d"]), str(row["max_d"])


def soccer_daily_distinct_dates(db_path: Path) -> list[str]:
    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_DAILY_TABLE):
            return []
        return [
            str(r["match_date"])
            for r in conn.execute(
                f"SELECT DISTINCT match_date FROM {SOCCER_DAILY_TABLE} "
                "ORDER BY match_date;"
            )
        ]


def _upsert_hkjc_history_row(
    conn: sqlite3.Connection,
    *,
    raw: dict[str, Any],
    parsed: ParsedOfficialResult,
    kickoff_utc: str,
    kickoff_hkt: str,
    match_date: str,
    stamp: str,
    voided: bool = False,
) -> str:
    """
    Insert or update one hkjc|{id} row. Returns 'inserted' or 'updated'.

    Normal confirmed-FT path: missing score fields stay NULL; ON CONFLICT does
    not clear existing score values.

    MATCHVOIDED path: clear HT/FT/2H scores and mark result_confirm_status as
    void so prior scores cannot keep showing as valid results.
    """
    source_key = hkjc_history_source_key(parsed.match_id)
    month = match_date[:7]
    tourn = raw.get("tournament") if isinstance(raw.get("tournament"), dict) else {}
    competition_id = str(tourn.get("code") or "").strip() or None
    competition_name = (
        str(tourn.get("name_ch") or "").strip()
        or str(tourn.get("name_en") or "").strip()
        or None
    )
    home_name = _team_display_name(raw.get("homeTeam"))
    away_name = _team_display_name(raw.get("awayTeam"))
    home_id = _team_source_id(raw.get("homeTeam"))
    away_id = _team_source_id(raw.get("awayTeam"))
    front_end = str(raw.get("frontEndId") or "").strip() or None
    if voided:
        score_ft_home = score_ft_away = None
        score_ht_home = score_ht_away = None
        score_2h_home = score_2h_away = None
        has_score = 0
        score_complete = 0
        confirm_status = HKJC_RESULT_VOID_STATUS
        score_update_sql = f"""
            score_ft_home=NULL,
            score_ft_away=NULL,
            score_ht_home=NULL,
            score_ht_away=NULL,
            score_2h_home=NULL,
            score_2h_away=NULL,
            has_score=0,
            score_complete=0,
        """
    else:
        score_ft_home = parsed.ft.home if parsed.ft is not None else None
        score_ft_away = parsed.ft.away if parsed.ft is not None else None
        score_ht_home = parsed.ht.home if parsed.ht is not None else None
        score_ht_away = parsed.ht.away if parsed.ht is not None else None
        score_2h_home = parsed.sh.home if parsed.sh is not None else None
        score_2h_away = parsed.sh.away if parsed.sh is not None else None
        has_score = 1 if parsed.ft is not None else 0
        score_complete = 1 if (parsed.ft is not None and parsed.ht is not None) else 0
        confirm_status = HKJC_RESULT_CONFIRM_STATUS
        score_update_sql = f"""
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
            has_score=CASE
                WHEN excluded.has_score=1 THEN 1
                ELSE {FOOTYLOGIC_MATCHES_TABLE}.has_score END,
            score_complete=CASE
                WHEN excluded.score_complete=1 THEN 1
                ELSE {FOOTYLOGIC_MATCHES_TABLE}.score_complete END,
        """
    event_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str)

    existing = conn.execute(
        f"SELECT source_key FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key = ?;",
        (source_key,),
    ).fetchone()
    action = "updated" if existing is not None else "inserted"

    t = FOOTYLOGIC_MATCHES_TABLE
    conn.execute(
        f"""
        INSERT INTO {t} (
            source_key, source, competition_id, season_id, month, competition_name,
            home_team_id, away_team_id, home_team_name, away_team_name,
            kickoff_utc, kickoff_hkt, match_date_hkt,
            score_ft_home, score_ft_away, score_ht_home, score_ht_away,
            score_2h_home, score_2h_away,
            has_score, score_complete, result_confirm_status,
            event_json, source_last_updated, created_at, updated_at,
            hkjc_match_id, hkjc_front_end_id
        ) VALUES (
            ?, ?, ?, NULL, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?
        )
        ON CONFLICT(source_key) DO UPDATE SET
            competition_id=COALESCE(excluded.competition_id, {t}.competition_id),
            competition_name=COALESCE(excluded.competition_name, {t}.competition_name),
            home_team_id=COALESCE(excluded.home_team_id, {t}.home_team_id),
            away_team_id=COALESCE(excluded.away_team_id, {t}.away_team_id),
            home_team_name=COALESCE(excluded.home_team_name, {t}.home_team_name),
            away_team_name=COALESCE(excluded.away_team_name, {t}.away_team_name),
            kickoff_utc=excluded.kickoff_utc,
            kickoff_hkt=excluded.kickoff_hkt,
            match_date_hkt=excluded.match_date_hkt,
            month=excluded.month,
            {score_update_sql}
            result_confirm_status=excluded.result_confirm_status,
            event_json=excluded.event_json,
            source_last_updated=excluded.source_last_updated,
            updated_at=excluded.updated_at,
            hkjc_match_id=COALESCE(excluded.hkjc_match_id, {t}.hkjc_match_id),
            hkjc_front_end_id=COALESCE(excluded.hkjc_front_end_id, {t}.hkjc_front_end_id)
        """,
        (
            source_key,
            HKJC_SOURCE,
            competition_id,
            month,
            competition_name,
            home_id,
            away_id,
            home_name,
            away_name,
            kickoff_utc,
            kickoff_hkt,
            match_date,
            score_ft_home,
            score_ft_away,
            score_ht_home,
            score_ht_away,
            score_2h_home,
            score_2h_away,
            has_score,
            score_complete,
            confirm_status,
            event_json,
            stamp,
            stamp,
            stamp,
            parsed.match_id,
            front_end,
        ),
    )
    return action


# history_scope values for ingest_official_results_to_history
HISTORY_SCOPE_DAILY_ONLY = "daily_only"  # task: only rows present in soccer_daily
HISTORY_SCOPE_PAYLOAD_ALL = "payload_all"  # manual: all confirmed-FT rows in payload


def ingest_official_results_to_history(
    db_path: Path,
    response_obj: Any,
    *,
    request_key: str = REQUEST_KEY_UPDATE_RESULTS,
    now_utc: str | None = None,
    variables: dict[str, Any] | None = None,
    only_daily_ids: set[str] | None = None,
    history_scope: str = HISTORY_SCOPE_DAILY_ONLY,
) -> IngestResult:
    """
    Official matchResults → upsert footylogic_matches (source=hkjc). If the
    match is in soccer_daily, DELETE + tombstone in the same per-match
    transaction.

    Eligible branches:
      1) status==MATCHVOIDED (exact): write/update void history even when
         results=[]; clear scores; result_confirm_status=hkjc_match_voided.
      2) Confirmed FT (select_official_stage_score):
         resultType==1, resultConfirmType>1, payoutConfirmed is True,
         stageId==5 (FT). HT is stageId==3 with the same confirm rules.
         Non-void source status is not a gate.

    history_scope:
      daily_only — skip ids not in soccer_daily (auto task).
      payload_all — upsert all eligible payload rows; delete daily only when
        present (manual download). Tombstone never blocks history upsert.
    """
    if history_scope not in (HISTORY_SCOPE_DAILY_ONLY, HISTORY_SCOPE_PAYLOAD_ALL):
        raise SoccerIngestError(
            "invalid_fields", f"unknown history_scope={history_scope!r}"
        )
    if not db_path.is_file():
        raise SoccerIngestError("db_missing", f"database missing: {db_path}")

    matches_raw, match_num_total, _ = _extract_match_results_envelope(response_obj)
    matches_returned = len(matches_raw)
    assert_match_results_completeness(
        matches_returned=matches_returned,
        match_num_total=match_num_total,
        variables=variables,
    )

    if matches_returned == 0:
        return IngestResult(
            code="skipped_empty",
            processed=0,
            inserted=0,
            updated=0,
            had_complete=0,
            had_incomplete=0,
            last_success_at=get_last_success_at(db_path, request_key),
            detail="empty matches",
            results_applied=False,
            request_key=request_key,
            match_num_total=match_num_total,
            matches_returned=0,
        )

    stamp = now_utc or utc_now_iso()
    inserted = 0
    updated = 0
    daily_deleted = 0
    skipped_status = 0
    skipped_unconfirmed = 0
    skipped_no_kickoff = 0
    skipped_not_in_daily = 0
    ft_applied = 0
    ht_applied = 0
    sh_applied = 0
    void_applied = 0

    with get_connection(db_path) as conn:
        if not table_exists(conn, SOCCER_DAILY_TABLE):
            raise SoccerIngestError("table_missing", "soccer_daily missing; run init")
        ensure_footylogic_schema(conn)
        if not table_exists(conn, SOCCER_DAILY_REMOVED_TABLE):
            raise SoccerIngestError(
                "table_missing",
                "soccer_daily_removed missing; run: python -m hppv.soccer_db init",
            )
        if only_daily_ids is None:
            daily_ids = {
                str(r["match_id"])
                for r in conn.execute(
                    f"SELECT match_id FROM {SOCCER_DAILY_TABLE};"
                ).fetchall()
            }
        else:
            daily_ids = set(only_daily_ids)

        seen: set[str] = set()
        for item in matches_raw:
            if not isinstance(item, dict):
                raise SoccerIngestError(
                    "structure_mismatch", "match result item is not an object"
                )
            parsed = parse_official_result_item(item)
            if parsed.match_id in seen:
                raise SoccerIngestError(
                    "duplicate_id", f"duplicate match_id {parsed.match_id}"
                )
            seen.add(parsed.match_id)

            is_void = parsed.source_status == STATUS_MATCHVOIDED
            # Void is an independent finish branch; otherwise confirmed FT is required.
            if not is_void and parsed.ft is None:
                skipped_unconfirmed += 1
                continue
            in_daily = parsed.match_id in daily_ids
            if history_scope == HISTORY_SCOPE_DAILY_ONLY and not in_daily:
                skipped_not_in_daily += 1
                continue
            try:
                kickoff_utc, kickoff_hkt, match_date = parse_kickoff_utc_and_hkt(
                    item.get("kickOffTime")
                )
            except SoccerIngestError:
                skipped_no_kickoff += 1
                continue

            tomb_reason = (
                TOMBSTONE_REASON_OFFICIAL_VOID
                if is_void
                else TOMBSTONE_REASON_OFFICIAL_FT
            )
            try:
                conn.execute("BEGIN;")
                action = _upsert_hkjc_history_row(
                    conn,
                    raw=item,
                    parsed=parsed,
                    kickoff_utc=kickoff_utc,
                    kickoff_hkt=kickoff_hkt,
                    match_date=match_date,
                    stamp=stamp,
                    voided=is_void,
                )
                if in_daily:
                    del_cur = conn.execute(
                        f"DELETE FROM {SOCCER_DAILY_TABLE} WHERE match_id = ?;",
                        (parsed.match_id,),
                    )
                    if del_cur.rowcount != 1:
                        raise SoccerIngestError(
                            "daily_delete_failed",
                            f"expected 1 deleted row match_id={parsed.match_id} "
                            f"rowcount={del_cur.rowcount}",
                        )
                    conn.execute(
                        f"""
                        INSERT INTO {SOCCER_DAILY_REMOVED_TABLE}
                            (match_id, removed_at, reason)
                        VALUES (?, ?, ?)
                        ON CONFLICT(match_id) DO UPDATE SET
                            removed_at = excluded.removed_at,
                            reason = excluded.reason;
                        """,
                        (parsed.match_id, stamp, tomb_reason),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            if action == "inserted":
                inserted += 1
            else:
                updated += 1
            if in_daily:
                daily_deleted += 1
                daily_ids.discard(parsed.match_id)
            if is_void:
                void_applied += 1
            else:
                ft_applied += 1
                if parsed.ht is not None:
                    ht_applied += 1
                if parsed.sh is not None:
                    sh_applied += 1

        if daily_deleted == 0 and inserted == 0 and updated == 0:
            return IngestResult(
                code="skipped_no_existing",
                processed=len(seen),
                inserted=0,
                updated=0,
                had_complete=0,
                had_incomplete=0,
                last_success_at=get_last_success_at(db_path, request_key),
                detail=(
                    f"no confirmed-FT/void rows moved; skipped_status={skipped_status} "
                    f"skipped_unconfirmed={skipped_unconfirmed} "
                    f"skipped_no_kickoff={skipped_no_kickoff} "
                    f"skipped_not_in_daily={skipped_not_in_daily}"
                ),
                results_applied=False,
                request_key=request_key,
                skipped_status=skipped_status,
                skipped_unconfirmed=skipped_unconfirmed,
                skipped_no_kickoff=skipped_no_kickoff,
                skipped_not_in_daily=skipped_not_in_daily,
                match_num_total=match_num_total,
                matches_returned=matches_returned,
                daily_deleted=0,
            )

        conn.execute(
            """
            INSERT INTO soccer_sync_state (request_key, last_success_at)
            VALUES (?, ?)
            ON CONFLICT(request_key) DO UPDATE SET
                last_success_at = excluded.last_success_at;
            """,
            (request_key, stamp),
        )
        conn.commit()

    detail = (
        f"history inserted={inserted} updated={updated} daily_deleted={daily_deleted}; "
        f"ht_applied={ht_applied} ft_applied={ft_applied} sh_applied={sh_applied} "
        f"void_applied={void_applied}; "
        f"skipped_status={skipped_status} skipped_unconfirmed={skipped_unconfirmed} "
        f"skipped_no_kickoff={skipped_no_kickoff} "
        f"skipped_not_in_daily={skipped_not_in_daily}"
    )
    return IngestResult(
        code="success",
        processed=len(seen),
        inserted=inserted,
        updated=updated,
        had_complete=ht_applied,
        had_incomplete=ft_applied,
        last_success_at=stamp,
        detail=detail,
        results_applied=True,
        request_key=request_key,
        ht_applied=ht_applied,
        ft_applied=ft_applied,
        sh_applied=sh_applied,
        skipped_status=skipped_status,
        skipped_unconfirmed=skipped_unconfirmed,
        skipped_no_kickoff=skipped_no_kickoff,
        skipped_not_in_daily=skipped_not_in_daily,
        match_num_total=match_num_total,
        matches_returned=matches_returned,
        daily_deleted=daily_deleted,
    )

