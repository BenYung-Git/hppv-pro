"""Parse Footylogic fixtures-results payloads into normalized rows."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
UTC = timezone.utc

# Inclusive HKT calendar window from user confirmation.
DEFAULT_RANGE_START_HKT = datetime(2021, 11, 5, 0, 0, 0, tzinfo=HKT)
DEFAULT_RANGE_END_EXCLUSIVE_HKT = datetime(2026, 11, 6, 0, 0, 0, tzinfo=HKT)


@dataclass(frozen=True, slots=True)
class ParsedFootylogicMatch:
    source_key: str
    competition_id: str
    season_id: str
    month: str
    competition_name: str | None
    home_team_id: str | None
    away_team_id: str | None
    home_team_name: str | None
    away_team_name: str | None
    kickoff_utc: str
    kickoff_hkt: str
    match_date_hkt: str
    score_ft_home: int | None
    score_ft_away: int | None
    score_ht_home: int | None
    score_ht_away: int | None
    has_score: bool
    score_complete: bool
    result_confirm_status: str  # always source_score_unconfirmed when has_score
    identity_ok: bool
    quarantine_reason: str | None
    event_json: str
    source_last_updated: str | None


def parse_mautcko_to_hkt(raw: Any) -> tuple[datetime | None, str]:
    """Return (hkt datetime, error_or_empty)."""
    text = str(raw or "").strip()
    if not text:
        return None, "missing_maUTCKO"
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return None, "maUTCKO_missing_timezone"
        return dt.astimezone(HKT), ""
    except ValueError:
        return None, "maUTCKO_unparseable"


def _norm_team_id(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text.lower() in {"null", "none"}:
        return None
    return text


def _norm_team_name(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text else None


def parse_score_value(raw: Any) -> tuple[int | None, str | None]:
    """
    Return (value, error).
    None input -> (None, None) meaning missing, not zero.
    """
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, "score_bool_invalid"
    if isinstance(raw, int):
        if raw < 0:
            return None, "score_negative"
        return raw, None
    if isinstance(raw, float):
        if not raw.is_integer() or raw < 0:
            return None, "score_non_int"
        return int(raw), None
    text = str(raw).strip()
    if text == "" or text.lower() in {"null", "none", "-"}:
        return None, None
    if re.fullmatch(r"\d+", text):
        return int(text), None
    return None, "score_unparseable"


def build_source_key(
    *,
    competition_id: str,
    season_id: str,
    home_team_id: str | None,
    away_team_id: str | None,
    kickoff_utc: str,
) -> str:
    """Stable internal id. Not an HKJC match_id. Not Racing EPL-* style."""
    home = home_team_id if home_team_id is not None else "-"
    away = away_team_id if away_team_id is not None else "-"
    return f"fl|{competition_id}|{season_id}|{home}|{away}|{kickoff_utc}"


def identity_status(
    *,
    home_team_id: str | None,
    away_team_id: str | None,
    home_team_name: str | None,
    away_team_name: str | None,
) -> tuple[bool, str | None]:
    """Require at least one side team id; names alone are insufficient."""
    if home_team_id is None and away_team_id is None:
        return False, "both_team_ids_missing"
    if home_team_id is None and not home_team_name:
        return False, "home_id_and_name_missing"
    if away_team_id is None and not away_team_name:
        return False, "away_id_and_name_missing"
    return True, None


def parse_fixtures_results_payload(
    body: dict[str, Any],
    *,
    competition_id: Any,
    season_id: Any,
    month: str,
    range_start_hkt: datetime = DEFAULT_RANGE_START_HKT,
    range_end_exclusive_hkt: datetime = DEFAULT_RANGE_END_EXCLUSIVE_HKT,
    now_hkt: datetime | None = None,
) -> tuple[list[ParsedFootylogicMatch], list[ParsedFootylogicMatch], dict[str, int]]:
    """
    Parse one fixtures-results JSON object.
    Returns (accepted_or_scoreless_in_range, quarantined, stats).
    """
    stats = {
        "events_raw": 0,
        "in_range": 0,
        "out_of_range": 0,
        "future_excluded": 0,
        "has_score": 0,
        "scoreless": 0,
        "quarantine": 0,
        "score_incomplete": 0,
    }
    accepted: list[ParsedFootylogicMatch] = []
    quarantined: list[ParsedFootylogicMatch] = []

    if now_hkt is None:
        now_hkt = datetime.now(HKT)

    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("fixtures-results data missing")
    competition_name = data.get("competitionName")
    if competition_name is not None:
        competition_name = str(competition_name)
    source_last_updated = data.get("lastUpdated")
    if source_last_updated is not None:
        source_last_updated = str(source_last_updated)

    cid = str(competition_id)
    sid = str(season_id)
    info = data.get("info") or []
    if not isinstance(info, list):
        raise ValueError("fixtures-results info not list")

    for day in info:
        if not isinstance(day, dict):
            continue
        events = day.get("events") or []
        if not isinstance(events, list):
            continue
        for ev in events:
            if not isinstance(ev, dict):
                continue
            stats["events_raw"] += 1
            mautcko = ev.get("maUTCKO")
            kick_hkt, err = parse_mautcko_to_hkt(mautcko)
            home_id = _norm_team_id(ev.get("homeTeamId"))
            away_id = _norm_team_id(ev.get("awayTeamId"))
            home_name = _norm_team_name(ev.get("homeTeamName"))
            away_name = _norm_team_name(ev.get("awayTeamName"))
            ft_h, ft_h_err = parse_score_value(ev.get("homeTeamGoals"))
            ft_a, ft_a_err = parse_score_value(ev.get("awayTeamGoals"))
            ht_h, ht_h_err = parse_score_value(ev.get("homeTeamHTGoals"))
            ht_a, ht_a_err = parse_score_value(ev.get("awayTeamHTGoals"))

            event_json = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))

            if kick_hkt is None:
                q = ParsedFootylogicMatch(
                    source_key="",
                    competition_id=cid,
                    season_id=sid,
                    month=month,
                    competition_name=competition_name,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    home_team_name=home_name,
                    away_team_name=away_name,
                    kickoff_utc=str(mautcko or ""),
                    kickoff_hkt="",
                    match_date_hkt="",
                    score_ft_home=ft_h,
                    score_ft_away=ft_a,
                    score_ht_home=ht_h,
                    score_ht_away=ht_a,
                    has_score=False,
                    score_complete=False,
                    result_confirm_status="source_score_unconfirmed",
                    identity_ok=False,
                    quarantine_reason=err or "missing_kickoff",
                    event_json=event_json,
                    source_last_updated=source_last_updated,
                )
                quarantined.append(q)
                stats["quarantine"] += 1
                continue

            if kick_hkt >= now_hkt:
                stats["future_excluded"] += 1
                continue
            if kick_hkt < range_start_hkt or kick_hkt >= range_end_exclusive_hkt:
                stats["out_of_range"] += 1
                continue
            stats["in_range"] += 1

            kickoff_utc = kick_hkt.astimezone(UTC).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
            # Prefer original Z string when already UTC Z
            if isinstance(mautcko, str) and mautcko.endswith("Z"):
                kickoff_utc = mautcko

            id_ok, id_reason = identity_status(
                home_team_id=home_id,
                away_team_id=away_id,
                home_team_name=home_name,
                away_team_name=away_name,
            )
            source_key = (
                build_source_key(
                    competition_id=cid,
                    season_id=sid,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    kickoff_utc=kickoff_utc,
                )
                if id_ok
                else ""
            )

            score_errs = [e for e in (ft_h_err, ft_a_err, ht_h_err, ht_a_err) if e]
            ft_present = ft_h is not None or ft_a is not None
            ht_present = ht_h is not None or ht_a is not None
            has_score = ft_present or ht_present
            score_complete = ft_h is not None and ft_a is not None
            if has_score and not score_complete:
                stats["score_incomplete"] += 1
            if has_score:
                stats["has_score"] += 1
            else:
                stats["scoreless"] += 1

            quarantine_reason = None
            if not id_ok:
                quarantine_reason = id_reason
            elif score_errs:
                quarantine_reason = ",".join(score_errs)
            elif ft_present and (ft_h is None or ft_a is None):
                quarantine_reason = "ft_score_one_sided"
            elif ht_present and (ht_h is None or ht_a is None) and not score_complete:
                # HT one-sided with no complete FT -> quarantine; if FT complete keep HT nullable sides? 
                # Spec: one-sided scores are anomalies.
                if ht_h is None or ht_a is None:
                    quarantine_reason = "ht_score_one_sided"

            row = ParsedFootylogicMatch(
                source_key=source_key
                or (
                    "fl|quarantine|"
                    + hashlib.sha1(
                        f"{cid}|{sid}|{kickoff_utc}|{home_name}|{away_name}|{event_json}".encode(
                            "utf-8"
                        )
                    ).hexdigest()[:24]
                ),
                competition_id=cid,
                season_id=sid,
                month=month,
                competition_name=competition_name,
                home_team_id=home_id,
                away_team_id=away_id,
                home_team_name=home_name,
                away_team_name=away_name,
                kickoff_utc=kickoff_utc,
                kickoff_hkt=kick_hkt.isoformat(timespec="seconds"),
                match_date_hkt=kick_hkt.strftime("%Y-%m-%d"),
                score_ft_home=ft_h,
                score_ft_away=ft_a,
                score_ht_home=ht_h,
                score_ht_away=ht_a,
                has_score=has_score and quarantine_reason is None and score_complete,
                score_complete=score_complete,
                result_confirm_status="source_score_unconfirmed",
                identity_ok=id_ok and quarantine_reason is None,
                quarantine_reason=quarantine_reason,
                event_json=event_json,
                source_last_updated=source_last_updated,
            )
            if quarantine_reason:
                quarantined.append(row)
                stats["quarantine"] += 1
            else:
                # Scoreless in-range kept for trace but has_score False
                accepted.append(row)

    return accepted, quarantined, stats
