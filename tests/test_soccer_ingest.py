from __future__ import annotations

import json
from pathlib import Path

import pytest

from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_ingest import (
    REQUEST_KEY_UPDATE_ODDS_AND_RESULTS,
    SoccerIngestError,
    extract_had_from_match,
    format_hkt_display,
    get_last_success_at,
    ingest_matches_and_odds,
    parse_kickoff_hkt,
    parse_matches_payload,
    update_odds_for_existing_matches,
)


def _match(
    *,
    mid: str = "100",
    front: str = "FB1",
    kick: str = "2026-11-04T20:00:00.000+08:00",
    code: str = "AC2",
    name: str = "亞冠2",
    home: str = "主隊",
    away: str = "客隊",
    had_order: tuple[str, str, str] = ("H", "A", "D"),
    odds: tuple[str, str, str] = ("1.90", "3.20", "2.50"),
    multi_had: bool = False,
) -> dict:
    combos = []
    mapping = {"H": odds[0], "A": odds[1], "D": odds[2]}
    for i, s in enumerate(had_order, start=1):
        combos.append(
            {
                "combId": str(i),
                "str": s,
                "status": "AVAILABLE",
                "currentOdds": mapping[s],
                "selections": [{"selId": str(i), "str": s, "name_en": s, "name_ch": s}],
            }
        )
    pool = {
        "id": "p1",
        "status": "SELLINGSTARTED",
        "oddsType": "HAD",
        "instNo": 0,
        "inplay": False,
        "lines": [
            {
                "lineId": "0",
                "status": "AVAILABLE",
                "condition": "0.0",
                "main": True,
                "combinations": combos,
            }
        ],
    }
    pools = [pool, dict(pool, id="p2")] if multi_had else [pool]
    return {
        "id": mid,
        "frontEndId": front,
        "kickOffTime": kick,
        "status": "PreEvent",
        "homeTeam": {"name_ch": home, "name_en": "H"},
        "awayTeam": {"name_ch": away, "name_en": "A"},
        "tournament": {"code": code, "name_ch": name, "name_en": "ACL2"},
        "foPools": pools,
    }


def test_had_mapping_ignores_order() -> None:
    # odds tuple is (H, A, D); array order D/A/H must still map correctly.
    m = _match(had_order=("D", "A", "H"), odds=("1.85", "3.10", "2.80"))
    odds_map, status = extract_had_from_match(m)
    assert status == "SELLINGSTARTED"
    assert odds_map == {"home": 1.85, "draw": 2.80, "away": 3.10}


def test_had_ambiguous_multi_pool() -> None:
    m = _match(multi_had=True)
    with pytest.raises(SoccerIngestError) as exc:
        extract_had_from_match(m)
    assert exc.value.code == "had_ambiguous"


def test_kickoff_hkt_and_ac2_preserved(tmp_path: Path) -> None:
    kickoff_at, match_date = parse_kickoff_hkt("2026-11-04T23:30:00.000+00:00")
    assert match_date == "2026-11-05"
    assert "+08:00" in kickoff_at

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    payload = {"data": {"matches": [_match(code="AC2")]}}
    result = ingest_matches_and_odds(db, payload, now_utc="2026-11-04T10:00:00+00:00")
    assert result.code == "success"
    assert result.inserted == 1
    with get_connection(db) as conn:
        row = conn.execute("SELECT * FROM soccer_daily").fetchone()
        assert row["competition_code"] == "AC2"
        assert row["match_code"] == "FB1"
        assert row["home_odds"] == 1.9
        assert get_last_success_at(db) == "2026-11-04T10:00:00+00:00"
        assert format_hkt_display(get_last_success_at(db)) == "2026-11-04 18:00:00"


def test_upsert_preserves_created_and_odds_when_incomplete(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    first = {"data": {"matches": [_match(mid="M1")]}}
    ingest_matches_and_odds(db, first, now_utc="2026-11-04T01:00:00+00:00")

    # Incomplete HAD (missing D) keeps old odds
    m = _match(mid="M1", home="主隊改")
    m["foPools"][0]["lines"][0]["combinations"] = [
        c for c in m["foPools"][0]["lines"][0]["combinations"] if c["str"] != "D"
    ]
    second = {"data": {"matches": [m]}}
    r2 = ingest_matches_and_odds(db, second, now_utc="2026-11-04T02:00:00+00:00")
    assert r2.updated == 1
    assert r2.had_incomplete == 1
    with get_connection(db) as conn:
        row = conn.execute("SELECT * FROM soccer_daily WHERE match_id='M1'").fetchone()
        assert row["home_team"] == "主隊改"
        assert row["created_at"] == "2026-11-04T01:00:00+00:00"
        assert row["home_odds"] == 1.9
        assert row["odds_fetched_at"] == "2026-11-04T01:00:00+00:00"


def test_empty_and_duplicate_and_errors_skip_sync_time(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    empty = ingest_matches_and_odds(db, {"data": {"matches": []}})
    assert empty.code == "skipped_empty"
    assert get_last_success_at(db) is None

    with pytest.raises(SoccerIngestError) as e1:
        parse_matches_payload({"errors": [{"message": "x"}], "data": {"matches": []}})
    assert e1.value.code == "graphql_errors"

    with pytest.raises(SoccerIngestError) as e2:
        parse_matches_payload(
            {"data": {"matches": [_match(mid="X"), _match(mid="X")]}}
        )
    assert e2.value.code == "duplicate_id"

    # No sync time after failed parse attempt via ingest
    with pytest.raises(SoccerIngestError):
        ingest_matches_and_odds(
            db, {"data": {"matches": [_match(mid="A"), _match(mid="A")]}}
        )
    assert get_last_success_at(db) is None


def test_old_matches_not_deleted(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    ingest_matches_and_odds(db, {"data": {"matches": [_match(mid="KEEP")]}})
    ingest_matches_and_odds(db, {"data": {"matches": [_match(mid="NEW")]}})
    with get_connection(db) as conn:
        ids = {
            r["match_id"]
            for r in conn.execute("SELECT match_id FROM soccer_daily").fetchall()
        }
    assert ids == {"KEEP", "NEW"}


def test_update_odds_existing_only_skips_unknown(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    ingest_matches_and_odds(db, {"data": {"matches": [_match(mid="M1", odds=("1.50", "3.50", "2.50"))]}})
    payload = {
        "data": {
            "matches": [
                _match(mid="M1", odds=("1.80", "3.20", "2.10")),
                _match(mid="MISSING", odds=("2.00", "3.00", "2.20")),
            ]
        }
    }
    r = update_odds_for_existing_matches(
        db, payload, now_utc="2026-11-04T12:00:00+00:00"
    )
    assert r.code == "success"
    assert r.updated == 1
    assert r.inserted == 0
    assert r.skipped_missing == 1
    assert r.results_applied is False
    assert get_last_success_at(db, REQUEST_KEY_UPDATE_ODDS_AND_RESULTS) == "2026-11-04T12:00:00+00:00"
    with get_connection(db) as conn:
        row = conn.execute("SELECT home_odds, match_code FROM soccer_daily WHERE match_id='M1'").fetchone()
        assert row["home_odds"] == 1.8
        assert row["match_code"] == "FB1"
        missing = conn.execute("SELECT 1 FROM soccer_daily WHERE match_id='MISSING'").fetchone()
        assert missing is None


def test_update_odds_all_missing_does_not_update_sync(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    r = update_odds_for_existing_matches(db, {"data": {"matches": [_match(mid="X")]}})
    assert r.code == "skipped_no_existing"
    assert get_last_success_at(db, REQUEST_KEY_UPDATE_ODDS_AND_RESULTS) is None


def test_odds_paths_never_set_result_fetched_at(tmp_path: Path) -> None:
    """result_fetched_at stays NULL until official result ingest commits (not yet wired)."""
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    ingest_matches_and_odds(
        db,
        {"data": {"matches": [_match(mid="R1", odds=("1.90", "3.20", "4.00"))]}},
        now_utc="2026-11-04T01:00:00+00:00",
    )
    update_odds_for_existing_matches(
        db,
        {"data": {"matches": [_match(mid="R1", odds=("2.10", "3.00", "3.80"))]}},
        now_utc="2026-11-04T02:00:00+00:00",
    )
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT odds_fetched_at, result_fetched_at, score_ft_home, result_status "
            "FROM soccer_daily WHERE match_id='R1'"
        ).fetchone()
    assert row["odds_fetched_at"] == "2026-11-04T02:00:00+00:00"
    assert row["result_fetched_at"] is None
    assert row["score_ft_home"] is None
    assert row["result_status"] is None

