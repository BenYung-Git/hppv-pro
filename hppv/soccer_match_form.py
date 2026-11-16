"""Head-to-head / recent-form panel for daily + history (read-only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE
from hppv.soccer_db import get_connection, table_exists
from hppv.soccer_history import (
    HISTORY_TEAM_RESULT_DRAW,
    HISTORY_TEAM_RESULT_LOSE,
    HISTORY_TEAM_RESULT_NEUTRAL,
    HISTORY_TEAM_RESULT_WIN,
    HISTORY_VOID_SCORE_DISPLAY,
    format_name_or_id,
)

# Distinct from neutral (missing FT): void rows show「失效」in the recent-form result column.
FORM_TEAM_RESULT_VOID = "void"
from hppv.soccer_ingest import HKJC_RESULT_CONFIRM_STATUS, HKJC_RESULT_VOID_STATUS
from hppv.soccer_matches import (
    EMPTY_DISPLAY,
    SoccerDataError,
    format_kickoff_hhmm,
    format_score_pair,
    format_text_field,
)


def _parse_valid_ft_goal(raw: Any) -> int | None:
    """Accept a real non-negative int goal count only. Never coerce NULL/blank to 0."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw).strip()
    if text == "" or text == EMPTY_DISPLAY:
        return None
    if text.isdigit():
        return int(text)
    return None

logger = logging.getLogger(__name__)

MATCH_FORM_LIMITS: frozenset[int] = frozenset({5, 10, 20})
DEFAULT_MATCH_FORM_LIMIT = 10

VenueFilter = Literal["all", "home", "away"]
H2HVenueFilter = Literal["all", "same"]
CompetitionFilter = Literal["all", "same"]

# Fetch buffer before dedupe so cross-source duplicates do not shrink the page.
_FETCH_BUFFER = 4


class MatchFormError(SoccerDataError):
    """Base error for match-form queries (maps to API error codes)."""

    code = "read_error"


class TeamUnresolvedError(MatchFormError):
    """Team cannot be resolved to a single verified team_id."""

    code = "team_unresolved"


class MatchFormValidationError(MatchFormError):
    """Invalid request parameters."""

    code = "invalid_request"


@dataclass(frozen=True, slots=True)
class TeamRef:
    team_id: str
    team_name: str


@dataclass(frozen=True, slots=True)
class MatchFormAnchor:
    home: TeamRef
    away: TeamRef
    kickoff_hkt: str
    competition_id: str | None
    competition_name: str | None
    exclude_source_key: str | None = None
    exclude_hkjc_match_id: str | None = None


@dataclass(frozen=True, slots=True)
class MatchFormFilters:
    limit: int = DEFAULT_MATCH_FORM_LIMIT
    h2h_venue: H2HVenueFilter = "all"
    h2h_competition: CompetitionFilter = "all"
    home_venue: VenueFilter = "all"
    away_venue: VenueFilter = "all"
    home_competition: CompetitionFilter = "all"
    away_competition: CompetitionFilter = "all"


def normalize_match_form_limit(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MATCH_FORM_LIMIT
    if value in MATCH_FORM_LIMITS:
        return value
    return DEFAULT_MATCH_FORM_LIMIT


def normalize_venue_filter(raw: Any) -> VenueFilter:
    text = str(raw or "all").strip().lower()
    if text in ("all", "home", "away"):
        return text  # type: ignore[return-value]
    return "all"


def normalize_h2h_venue_filter(raw: Any) -> H2HVenueFilter:
    text = str(raw or "all").strip().lower()
    if text in ("all", "same"):
        return text  # type: ignore[return-value]
    return "all"


def normalize_competition_filter(raw: Any) -> CompetitionFilter:
    text = str(raw or "all").strip().lower()
    if text in ("all", "same"):
        return text  # type: ignore[return-value]
    return "all"


def _norm_id(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _norm_name(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _kickoff_dedupe_key(kickoff_hkt: Any) -> str:
    text = str(kickoff_hkt or "").strip()
    # Minute precision is enough to collapse HKJC vs Footylogic twins.
    return text[:16]


def _row_priority(row: Any) -> tuple[int, int, str]:
    confirm = str(row["result_confirm_status"] or "")
    if confirm == HKJC_RESULT_CONFIRM_STATUS:
        confirm_rank = 0
    elif confirm == HKJC_RESULT_VOID_STATUS:
        confirm_rank = 1
    else:
        confirm_rank = 2
    source = str(row["source"] or "")
    source_rank = 0 if source == "footylogic" else 1
    return (confirm_rank, source_rank, str(row["source_key"] or ""))


def dedupe_match_rows(rows: list[Any]) -> list[Any]:
    """
    Collapse cross-source duplicates of the same fixture.

    Key: home_team_id + away_team_id + kickoff_hkt[:16].
    Prefer confirmed FT, then void, then unconfirmed; prefer footylogic over hkjc.
    """
    best: dict[tuple[str, str, str], Any] = {}
    for row in rows:
        home_id = _norm_id(row["home_team_id"])
        away_id = _norm_id(row["away_team_id"])
        if not home_id or not away_id:
            continue
        key = (home_id, away_id, _kickoff_dedupe_key(row["kickoff_hkt"]))
        prev = best.get(key)
        if prev is None or _row_priority(row) < _row_priority(prev):
            best[key] = row
    # Preserve kickoff descending order from SQL.
    ordered = sorted(
        best.values(),
        key=lambda r: (str(r["kickoff_hkt"] or ""), str(r["source_key"] or "")),
        reverse=True,
    )
    return ordered


def resolve_team_id_by_exact_name(conn: Any, team_name: str) -> str:
    """
    Exact name → single team_id only. Never LIKE / contains / fuzzy.
    Ambiguous or missing → TeamUnresolvedError.
    """
    name = _norm_name(team_name)
    if not name:
        raise TeamUnresolvedError("empty_team_name")
    rows = conn.execute(
        f"""
        SELECT DISTINCT tid FROM (
          SELECT home_team_id AS tid
            FROM {FOOTYLOGIC_MATCHES_TABLE}
           WHERE home_team_name = ?
             AND home_team_id IS NOT NULL AND trim(home_team_id) != ''
          UNION
          SELECT away_team_id AS tid
            FROM {FOOTYLOGIC_MATCHES_TABLE}
           WHERE away_team_name = ?
             AND away_team_id IS NOT NULL AND trim(away_team_id) != ''
        )
        """,
        (name, name),
    ).fetchall()
    ids = [str(r[0]).strip() for r in rows if r[0] is not None and str(r[0]).strip()]
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise TeamUnresolvedError(f"team_name_not_found:{name}")
    raise TeamUnresolvedError(f"team_name_ambiguous:{name}")


def resolve_anchor_from_history_source_key(conn: Any, source_key: str) -> MatchFormAnchor:
    key = _norm_id(source_key)
    if not key:
        raise MatchFormValidationError("missing_source_key")
    row = conn.execute(
        f"SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key = ? LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        raise MatchFormValidationError("source_key_not_found")
    home_id = _norm_id(row["home_team_id"])
    away_id = _norm_id(row["away_team_id"])
    if not home_id or not away_id:
        raise TeamUnresolvedError("history_missing_team_id")
    kickoff = _norm_id(row["kickoff_hkt"])
    if not kickoff:
        raise MatchFormValidationError("missing_kickoff")
    return MatchFormAnchor(
        home=TeamRef(
            team_id=home_id,
            team_name=format_name_or_id(row["home_team_name"], home_id),
        ),
        away=TeamRef(
            team_id=away_id,
            team_name=format_name_or_id(row["away_team_name"], away_id),
        ),
        kickoff_hkt=kickoff,
        competition_id=_norm_id(row["competition_id"]),
        competition_name=_norm_name(row["competition_name"]),
        exclude_source_key=key,
        exclude_hkjc_match_id=_norm_id(row["hkjc_match_id"])
        if "hkjc_match_id" in row.keys()
        else None,
    )


def resolve_anchor_from_daily_match_id(conn: Any, match_id: str) -> MatchFormAnchor:
    mid = _norm_id(match_id)
    if not mid:
        raise MatchFormValidationError("missing_match_id")
    # Prefer HKJC history twin when already ingested (team ids confirmed there).
    hist = conn.execute(
        f"""
        SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE}
         WHERE hkjc_match_id = ?
         LIMIT 1
        """,
        (mid,),
    ).fetchone()
    if hist is not None:
        return resolve_anchor_from_history_source_key(conn, str(hist["source_key"]))

    if not table_exists(conn, "soccer_daily"):
        raise MatchFormValidationError("daily_table_missing")
    daily = conn.execute(
        """
        SELECT match_id, home_team, away_team, kickoff_at,
               competition_code, competition_name_zh
          FROM soccer_daily
         WHERE match_id = ?
         LIMIT 1
        """,
        (mid,),
    ).fetchone()
    if daily is None:
        raise MatchFormValidationError("match_id_not_found")
    home_name = _norm_name(daily["home_team"])
    away_name = _norm_name(daily["away_team"])
    kickoff = _norm_id(daily["kickoff_at"])
    if not home_name or not away_name or not kickoff:
        raise MatchFormValidationError("daily_incomplete")
    home_id = resolve_team_id_by_exact_name(conn, home_name)
    away_id = resolve_team_id_by_exact_name(conn, away_name)
    return MatchFormAnchor(
        home=TeamRef(team_id=home_id, team_name=home_name),
        away=TeamRef(team_id=away_id, team_name=away_name),
        kickoff_hkt=kickoff,
        competition_id=_norm_id(daily["competition_code"]),
        competition_name=_norm_name(daily["competition_name_zh"]),
        exclude_source_key=None,
        exclude_hkjc_match_id=mid,
    )


def resolve_anchor_from_ids(
    conn: Any,
    *,
    home_team_id: str,
    away_team_id: str,
    kickoff_hkt: str,
    competition_id: str | None = None,
    competition_name: str | None = None,
    exclude_source_key: str | None = None,
    exclude_hkjc_match_id: str | None = None,
    home_team_name: str | None = None,
    away_team_name: str | None = None,
) -> MatchFormAnchor:
    hid = _norm_id(home_team_id)
    aid = _norm_id(away_team_id)
    ko = _norm_id(kickoff_hkt)
    if not hid or not aid or not ko:
        raise MatchFormValidationError("missing_team_or_kickoff")
    return MatchFormAnchor(
        home=TeamRef(
            team_id=hid,
            team_name=format_name_or_id(home_team_name, hid),
        ),
        away=TeamRef(
            team_id=aid,
            team_name=format_name_or_id(away_team_name, aid),
        ),
        kickoff_hkt=ko,
        competition_id=_norm_id(competition_id),
        competition_name=_norm_name(competition_name),
        exclude_source_key=_norm_id(exclude_source_key),
        exclude_hkjc_match_id=_norm_id(exclude_hkjc_match_id),
    )


def is_void_row(row: Any) -> bool:
    return str(row["result_confirm_status"] or "") == HKJC_RESULT_VOID_STATUS


def is_stat_eligible(row: Any) -> bool:
    """
    Stats use confirmed HKJC FT, or Footylogic rows with complete FT.

    Limitation: Footylogic stores result_confirm_status=source_score_unconfirmed
    for the entire corpus; complete FT is the only usable form signal there.
    Void and missing FT never count.
    """
    if is_void_row(row):
        return False
    home = _parse_valid_ft_goal(row["score_ft_home"])
    away = _parse_valid_ft_goal(row["score_ft_away"])
    if home is None or away is None:
        return False
    confirm = str(row["result_confirm_status"] or "")
    if confirm == HKJC_RESULT_CONFIRM_STATUS:
        return True
    source = str(row["source"] or "")
    if source == "footylogic":
        complete = row["score_complete"] if "score_complete" in row.keys() else 0
        try:
            return int(complete) == 1
        except (TypeError, ValueError):
            return False
    return False


def form_team_result_for_subject(
    row: Any, *, subject_team_id: str
) -> str:
    """Outcome from subject team's perspective: win/draw/lose/void/neutral."""
    if is_void_row(row):
        return FORM_TEAM_RESULT_VOID
    home_g = _parse_valid_ft_goal(row["score_ft_home"])
    away_g = _parse_valid_ft_goal(row["score_ft_away"])
    if home_g is None or away_g is None:
        return HISTORY_TEAM_RESULT_NEUTRAL
    home_id = _norm_id(row["home_team_id"])
    away_id = _norm_id(row["away_team_id"])
    if subject_team_id == home_id:
        gf, ga = home_g, away_g
    elif subject_team_id == away_id:
        gf, ga = away_g, home_g
    else:
        return HISTORY_TEAM_RESULT_NEUTRAL
    if gf > ga:
        return HISTORY_TEAM_RESULT_WIN
    if gf < ga:
        return HISTORY_TEAM_RESULT_LOSE
    return HISTORY_TEAM_RESULT_DRAW


def form_display_result_classes(row: Any) -> tuple[str, str]:
    """Highlight for listed matches: FT when present; void/missing → neutral."""
    if is_void_row(row):
        return HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL
    home = _parse_valid_ft_goal(row["score_ft_home"])
    away = _parse_valid_ft_goal(row["score_ft_away"])
    if home is None or away is None:
        return HISTORY_TEAM_RESULT_NEUTRAL, HISTORY_TEAM_RESULT_NEUTRAL
    if home > away:
        return HISTORY_TEAM_RESULT_WIN, HISTORY_TEAM_RESULT_LOSE
    if home < away:
        return HISTORY_TEAM_RESULT_LOSE, HISTORY_TEAM_RESULT_WIN
    return HISTORY_TEAM_RESULT_DRAW, HISTORY_TEAM_RESULT_DRAW


def summarize_for_team(rows: list[Any], *, subject_team_id: str) -> dict[str, int]:
    played = len(rows)
    wins = draws = losses = 0
    goals_for = goals_against = 0
    valid = 0
    for row in rows:
        if not is_stat_eligible(row):
            continue
        home_g = _parse_valid_ft_goal(row["score_ft_home"])
        away_g = _parse_valid_ft_goal(row["score_ft_away"])
        if home_g is None or away_g is None:
            continue
        home_id = _norm_id(row["home_team_id"])
        away_id = _norm_id(row["away_team_id"])
        if subject_team_id == home_id:
            gf, ga = home_g, away_g
        elif subject_team_id == away_id:
            gf, ga = away_g, home_g
        else:
            continue
        valid += 1
        goals_for += gf
        goals_against += ga
        if gf > ga:
            wins += 1
        elif gf < ga:
            losses += 1
        else:
            draws += 1
    return {
        "played": played,
        "valid_played": valid,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_for": goals_for,
        "goals_against": goals_against,
    }


def _same_competition_sql(
    *,
    competition_id: str | None,
    competition_name: str | None,
    params: list[Any],
) -> str | None:
    clauses: list[str] = []
    if competition_id:
        clauses.append("competition_id = ?")
        params.append(competition_id)
    if competition_name:
        clauses.append("trim(ifnull(competition_name,'')) = ?")
        params.append(competition_name)
    if not clauses:
        return None
    return "(" + " OR ".join(clauses) + ")"


def _exclude_anchor_sql(anchor: MatchFormAnchor, params: list[Any]) -> str:
    parts: list[str] = []
    if anchor.exclude_source_key:
        parts.append("source_key != ?")
        params.append(anchor.exclude_source_key)
    if anchor.exclude_hkjc_match_id:
        parts.append("(hkjc_match_id IS NULL OR hkjc_match_id != ?)")
        params.append(anchor.exclude_hkjc_match_id)
    return (" AND " + " AND ".join(parts)) if parts else ""


def _query_candidates(
    conn: Any,
    *,
    where_sql: str,
    params: list[Any],
    limit: int,
) -> list[Any]:
    fetch_n = max(limit * _FETCH_BUFFER, limit)
    sql = f"""
        SELECT *
          FROM {FOOTYLOGIC_MATCHES_TABLE}
         WHERE {where_sql}
         ORDER BY kickoff_hkt DESC, source_key DESC
         LIMIT ?
    """
    params_with_limit = list(params) + [fetch_n]
    return list(conn.execute(sql, params_with_limit).fetchall())


def _row_to_h2h_view(row: Any) -> dict[str, str]:
    confirm = str(row["result_confirm_status"] or "")
    is_void = confirm == HKJC_RESULT_VOID_STATUS
    source_key = str(row["source_key"] or "")
    if is_void:
        score_ht = HISTORY_VOID_SCORE_DISPLAY
        score_ft = HISTORY_VOID_SCORE_DISPLAY
    else:
        score_ht = format_score_pair(
            row["score_ht_home"], row["score_ht_away"], match_id=source_key, field="score_ht"
        )
        score_ft = format_score_pair(
            row["score_ft_home"], row["score_ft_away"], match_id=source_key, field="score_ft"
        )
    home_res, away_res = form_display_result_classes(row)
    return {
        "source_key": source_key or EMPTY_DISPLAY,
        "match_date": format_text_field(row["match_date_hkt"]),
        "kickoff_hhmm": format_kickoff_hhmm(row["kickoff_hkt"]),
        "kickoff_hkt": format_text_field(row["kickoff_hkt"]),
        "competition_id": format_text_field(row["competition_id"]),
        "competition_name": format_name_or_id(row["competition_name"], row["competition_id"]),
        "home_team": format_name_or_id(row["home_team_name"], row["home_team_id"]),
        "away_team": format_name_or_id(row["away_team_name"], row["away_team_id"]),
        "home_team_id": format_text_field(row["home_team_id"]),
        "away_team_id": format_text_field(row["away_team_id"]),
        "score_ht": score_ht,
        "score_ft": score_ft,
        "home_team_result": home_res,
        "away_team_result": away_res,
        "result_confirm_status": confirm or EMPTY_DISPLAY,
        "source": format_text_field(row["source"]),
    }


def _row_to_recent_view(row: Any, *, subject_team_id: str) -> dict[str, str]:
    base = _row_to_h2h_view(row)
    home_id = _norm_id(row["home_team_id"])
    away_id = _norm_id(row["away_team_id"])
    if subject_team_id == home_id:
        venue = "home"
        opponent = format_name_or_id(row["away_team_name"], row["away_team_id"])
        opponent_id = format_text_field(row["away_team_id"])
    else:
        venue = "away"
        opponent = format_name_or_id(row["home_team_name"], row["home_team_id"])
        opponent_id = format_text_field(row["home_team_id"])
    subject_result = form_team_result_for_subject(row, subject_team_id=subject_team_id)
    base.update(
        {
            "venue": venue,
            "opponent": opponent,
            "opponent_id": opponent_id,
            "subject_result": subject_result,
        }
    )
    return base


def fetch_head_to_head(
    conn: Any,
    anchor: MatchFormAnchor,
    *,
    limit: int,
    venue: H2HVenueFilter,
    competition: CompetitionFilter,
) -> list[Any]:
    params: list[Any] = [
        anchor.home.team_id,
        anchor.away.team_id,
        anchor.away.team_id,
        anchor.home.team_id,
        anchor.kickoff_hkt,
    ]
    where = """
        (
          (home_team_id = ? AND away_team_id = ?)
          OR (home_team_id = ? AND away_team_id = ?)
        )
        AND kickoff_hkt IS NOT NULL
        AND kickoff_hkt < ?
    """
    where += _exclude_anchor_sql(anchor, params)
    if venue == "same":
        where += " AND home_team_id = ? AND away_team_id = ?"
        params.extend([anchor.home.team_id, anchor.away.team_id])
    if competition == "same":
        same = _same_competition_sql(
            competition_id=anchor.competition_id,
            competition_name=anchor.competition_name,
            params=params,
        )
        if same:
            where += " AND " + same
    rows = _query_candidates(conn, where_sql=where, params=params, limit=limit)
    return dedupe_match_rows(rows)[:limit]


def fetch_team_recent(
    conn: Any,
    anchor: MatchFormAnchor,
    *,
    subject_team_id: str,
    limit: int,
    venue: VenueFilter,
    competition: CompetitionFilter,
) -> list[Any]:
    params: list[Any] = [subject_team_id, subject_team_id, anchor.kickoff_hkt]
    where = """
        (home_team_id = ? OR away_team_id = ?)
        AND kickoff_hkt IS NOT NULL
        AND kickoff_hkt < ?
    """
    where += _exclude_anchor_sql(anchor, params)
    if venue == "home":
        where += " AND home_team_id = ?"
        params.append(subject_team_id)
    elif venue == "away":
        where += " AND away_team_id = ?"
        params.append(subject_team_id)
    if competition == "same":
        same = _same_competition_sql(
            competition_id=anchor.competition_id,
            competition_name=anchor.competition_name,
            params=params,
        )
        if same:
            where += " AND " + same
    rows = _query_candidates(conn, where_sql=where, params=params, limit=limit)
    return dedupe_match_rows(rows)[:limit]


def build_match_form_payload(
    conn: Any,
    anchor: MatchFormAnchor,
    filters: MatchFormFilters,
) -> dict[str, Any]:
    h2h_rows = fetch_head_to_head(
        conn,
        anchor,
        limit=filters.limit,
        venue=filters.h2h_venue,
        competition=filters.h2h_competition,
    )
    home_rows = fetch_team_recent(
        conn,
        anchor,
        subject_team_id=anchor.home.team_id,
        limit=filters.limit,
        venue=filters.home_venue,
        competition=filters.home_competition,
    )
    away_rows = fetch_team_recent(
        conn,
        anchor,
        subject_team_id=anchor.away.team_id,
        limit=filters.limit,
        venue=filters.away_venue,
        competition=filters.away_competition,
    )
    return {
        "cutoff": "before_kickoff",
        "cutoff_label_key": "soccer.match_form.cutoff",
        "kickoff_hkt": anchor.kickoff_hkt,
        "limit": filters.limit,
        "home_team": {
            "team_id": anchor.home.team_id,
            "team_name": anchor.home.team_name,
        },
        "away_team": {
            "team_id": anchor.away.team_id,
            "team_name": anchor.away.team_name,
        },
        "competition_id": anchor.competition_id or "",
        "competition_name": anchor.competition_name or "",
        "filters": {
            "h2h_venue": filters.h2h_venue,
            "h2h_competition": filters.h2h_competition,
            "home_venue": filters.home_venue,
            "away_venue": filters.away_venue,
            "home_competition": filters.home_competition,
            "away_competition": filters.away_competition,
        },
        "dedupe": {
            "rule": "home_team_id+away_team_id+kickoff_hkt[:16]",
            "preference": "confirmed>void>unconfirmed; footylogic>hkjc",
        },
        "stats_eligibility": {
            "hkjc": "hkjc_official_ft_confirmed with FT",
            "footylogic": "score_complete=1 with FT (source has no official confirm flag)",
            "excluded": ["hkjc_match_voided", "missing_ft"],
        },
        "head_to_head": [_row_to_h2h_view(r) for r in h2h_rows],
        # H2H header W/D/L: same filtered mutual meetings, subject = this fixture's home/away.
        "h2h_home_summary": summarize_for_team(
            h2h_rows, subject_team_id=anchor.home.team_id
        ),
        "h2h_away_summary": summarize_for_team(
            h2h_rows, subject_team_id=anchor.away.team_id
        ),
        "home_recent": [
            _row_to_recent_view(r, subject_team_id=anchor.home.team_id) for r in home_rows
        ],
        "away_recent": [
            _row_to_recent_view(r, subject_team_id=anchor.away.team_id) for r in away_rows
        ],
        "home_summary": summarize_for_team(home_rows, subject_team_id=anchor.home.team_id),
        "away_summary": summarize_for_team(away_rows, subject_team_id=anchor.away.team_id),
    }


def fetch_match_form(
    db_path: Path,
    *,
    source_key: str | None = None,
    match_id: str | None = None,
    home_team_id: str | None = None,
    away_team_id: str | None = None,
    kickoff_hkt: str | None = None,
    competition_id: str | None = None,
    competition_name: str | None = None,
    home_team_name: str | None = None,
    away_team_name: str | None = None,
    exclude_source_key: str | None = None,
    exclude_hkjc_match_id: str | None = None,
    filters: MatchFormFilters | None = None,
) -> dict[str, Any]:
    """Resolve anchor + return one payload for all three panel sections."""
    filt = filters or MatchFormFilters()
    try:
        with get_connection(db_path) as conn:
            if not table_exists(conn, FOOTYLOGIC_MATCHES_TABLE):
                raise MatchFormError("footylogic_matches_missing")
            if source_key:
                anchor = resolve_anchor_from_history_source_key(conn, source_key)
            elif match_id:
                anchor = resolve_anchor_from_daily_match_id(conn, match_id)
            elif home_team_id and away_team_id and kickoff_hkt:
                anchor = resolve_anchor_from_ids(
                    conn,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    kickoff_hkt=kickoff_hkt,
                    competition_id=competition_id,
                    competition_name=competition_name,
                    exclude_source_key=exclude_source_key,
                    exclude_hkjc_match_id=exclude_hkjc_match_id,
                    home_team_name=home_team_name,
                    away_team_name=away_team_name,
                )
            else:
                raise MatchFormValidationError("missing_anchor")
            return build_match_form_payload(conn, anchor, filt)
    except MatchFormError:
        raise
    except SoccerDataError:
        raise
    except Exception as e:
        logger.exception("match form query failed: %s", e)
        raise MatchFormError(str(e)) from e
