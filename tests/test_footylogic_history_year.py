"""Tests for FootyLogic year history download + HKJC overlay rules."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hppv.footylogic_catalog import (
    DiscoveredCompetition,
    months_for_year,
    parse_competitions_payload,
)
from hppv.footylogic_history_year import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    _compute_integrity,
    _empty_report,
    _finalize_status,
    build_year_jobs,
    discover_and_seed_year,
    run_year_download,
)
from hppv.footylogic_hkjc_overlay import (
    STATUS_HKJC_DUP_SKIP,
    STATUS_UNCERTAIN,
    filter_fl_rows_for_hkjc_skip,
    load_hkjc_by_date_for_year,
    match_fl_to_hkjc,
    run_hkjc_duplicate_skip_audit_for_year,
)
from hppv.footylogic_ingest import ensure_footylogic_schema, upsert_matches
from hppv.footylogic_parse import ParsedFootylogicMatch
from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE
from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_ingest import HKJC_RESULT_CONFIRM_STATUS, HKJC_RESULT_VOID_STATUS


def test_parse_competitions_nested_and_empty_group() -> None:
    league = parse_competitions_payload(
        {
            "data": [
                {
                    "categoryId": 10227,
                    "categoryName": "英格蘭",
                    "competitions": [
                        {"competitionId": 50071337, "competitionName": "英超"},
                    ],
                }
            ]
        },
        category_id=1,
    )
    assert league.ok
    assert league.nested_count == 1
    assert league.competitions[0].kind == "nested"
    assert league.competitions[0].browse_type == "league"

    cup = parse_competitions_payload(
        {
            "data": [
                {
                    "categoryId": 50070797,
                    "categoryName": "歐洲聯賽冠軍盃",
                    "competitions": [],
                }
            ]
        },
        category_id=2,
    )
    assert cup.ok
    assert cup.empty_group_count == 1
    assert cup.competitions[0].competition_id == "50070797"
    assert cup.competitions[0].kind == "empty_group"
    assert cup.competitions[0].browse_type == "cup"


def test_months_for_year_filters_calendar_year() -> None:
    fr = ["2024-11", "2024-12", "2025-01", "2025-05"]
    assert months_for_year(fr, 2025) == ["2025-01", "2025-05"]
    assert months_for_year(fr, 2024) == ["2024-11", "2024-12"]


def test_build_year_jobs_covers_types() -> None:
    comps = [
        DiscoveredCompetition(
            competition_id="1",
            competition_name="L",
            browse_category_id=1,
            browse_type="league",
            group_id="g1",
            group_name="G",
            kind="nested",
        ),
        DiscoveredCompetition(
            competition_id="2",
            competition_name="C",
            browse_category_id=2,
            browse_type="cup",
            group_id="2",
            group_name="C",
            kind="empty_group",
        ),
        DiscoveredCompetition(
            competition_id="3",
            competition_name="O",
            browse_category_id=4,
            browse_type="other",
            group_id="3",
            group_name="O",
            kind="empty_group",
        ),
    ]
    seasons = {
        "1": [{"seasonId": "s1", "seasonName": "25", "fixtureRange": ["2025-01", "2025-02"]}],
        "2": [{"seasonId": "s2", "seasonName": "25", "fixtureRange": ["2025-03"]}],
        "3": [{"seasonId": "s3", "seasonName": "25", "fixtureRange": ["2024-12", "2025-08"]}],
    }
    jobs, per = build_year_jobs(comps, seasons, 2025)
    assert len(jobs) == 4
    types = {p["browse_type"] for p in per}
    assert types == {"league", "cup", "other"}
    assert all(j["month"].startswith("2025-") for j in jobs)


class _FakeHttpResult:
    def __init__(self, body: dict[str, Any] | None, *, ok: bool = True, raw: str = "{}") -> None:
        self.body = body
        self.business_ok = ok
        self.http_status = 200 if ok else 500
        self.message = None if ok else "err"
        self.raw_text = raw if body is None else json.dumps(body, ensure_ascii=False)


class _FakeClient:
    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, path: str, params: dict[str, Any]) -> _FakeHttpResult:
        self.calls.append((path, dict(params)))
        key = (path, str(params.get("categoryId") or params.get("competitionId") or params.get("date") or ""))
        # Fallback keys
        for k, v in self.routes.items():
            if k[0] == path:
                if k[1] == "*" or k[1] == key[1]:
                    if callable(v):
                        return v(params)
                    return v
        raise AssertionError(f"unexpected call {path} {params}")


def _competitions_route(cat: int) -> _FakeHttpResult:
    if cat == 1:
        return _FakeHttpResult(
            {
                "data": [
                    {
                        "categoryId": 10,
                        "categoryName": "A",
                        "competitions": [
                            {"competitionId": 100, "competitionName": "League A"},
                        ],
                    }
                ]
            }
        )
    if cat == 2:
        return _FakeHttpResult(
            {
                "data": [
                    {
                        "categoryId": 200,
                        "categoryName": "Cup B",
                        "competitions": [],
                    }
                ]
            }
        )
    if cat == 3:
        return _FakeHttpResult(
            {
                "data": [
                    {
                        "categoryId": 300,
                        "categoryName": "Intl",
                        "competitions": [
                            {"competitionId": 301, "competitionName": "Intl Nested"},
                        ],
                    }
                ]
            }
        )
    if cat == 4:
        # Duplicate of cup empty group — should dedupe
        return _FakeHttpResult(
            {
                "data": [
                    {
                        "categoryId": 200,
                        "categoryName": "Cup B",
                        "competitions": [],
                    }
                ]
            }
        )
    return _FakeHttpResult({"data": []})


def test_discover_and_seed_year_traverses_all_types(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)

    def router(params: dict[str, Any]) -> _FakeHttpResult:
        if "categoryId" in params:
            return _competitions_route(int(params["categoryId"]))
        cid = str(params.get("competitionId"))
        return _FakeHttpResult(
            {
                "data": [
                    {
                        "seasonId": f"s-{cid}",
                        "seasonName": "2025",
                        "fixtureRange": ["2025-01"],
                    }
                ]
            }
        )

    client = _FakeClient(
        {
            ("/tournament/competitions", "*"): router,
            ("/tournament/fixtures-seasons", "*"): router,
        }
    )
    report = discover_and_seed_year(db, 2025, client=client, run_id="run-test")
    assert report["catalog"]["ok"] is True
    assert report["catalog"]["by_type"]["league"] >= 1
    assert report["catalog"]["by_type"]["cup"] >= 1
    assert report["catalog"]["by_type"]["other"] >= 1
    # category 4 deduped with cup 200
    assert report["catalog"]["competition_count"] == 3
    assert report["jobs"]["total"] == 3
    for cid in ("1", "2", "3", "4"):
        assert report["catalog"]["by_category"][cid]["ok"] is True


def test_catalog_failure_not_success(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)

    def fail_cat(params: dict[str, Any]) -> _FakeHttpResult:
        if int(params.get("categoryId") or 0) == 2:
            return _FakeHttpResult(None, ok=False, raw="boom")
        return _competitions_route(int(params["categoryId"]))

    client = _FakeClient({("/tournament/competitions", "*"): fail_cat})
    report = discover_and_seed_year(db, 2025, client=client, run_id="run-fail")
    assert report["status"] == STATUS_FAILED
    assert report["catalog"]["ok"] is False
    assert report["integrity"]["can_claim_complete"] is False


def _insert_match(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    source: str,
    date: str,
    home: str,
    away: str,
    competition: str,
    ft_h: int | None,
    ft_a: int | None,
    confirm: str,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
            source_key, source, competition_id, season_id, month, competition_name,
            home_team_id, away_team_id, home_team_name, away_team_name,
            kickoff_utc, kickoff_hkt, match_date_hkt,
            score_ft_home, score_ft_away, has_score, score_complete,
            result_confirm_status, event_json, created_at, updated_at
        ) VALUES (
            ?, ?, 'c1', 's1', ?, ?,
            'h1', 'a1', ?, ?,
            '2025-01-01T12:00:00.000Z', '2025-01-01T20:00:00+08:00', ?,
            ?, ?, ?, ?,
            ?, '{{}}', 't', 't'
        )
        """,
        (
            source_key,
            source,
            date[:7],
            competition,
            home,
            away,
            date,
            ft_h,
            ft_a,
            1 if ft_h is not None else 0,
            1 if ft_h is not None else 0,
            confirm,
        ),
    )


def _parsed(
    *,
    source_key: str,
    date: str,
    home: str,
    away: str,
    competition: str,
    ft_h: int | None = 1,
    ft_a: int | None = 0,
) -> ParsedFootylogicMatch:
    return ParsedFootylogicMatch(
        source_key=source_key,
        competition_id="c1",
        season_id="s1",
        month=date[:7],
        competition_name=competition,
        home_team_id="h1",
        away_team_id="a1",
        home_team_name=home,
        away_team_name=away,
        kickoff_utc="2025-01-01T12:00:00.000Z",
        kickoff_hkt="2025-01-01T20:00:00+08:00",
        match_date_hkt=date,
        score_ft_home=ft_h,
        score_ft_away=ft_a,
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
        has_score=ft_h is not None and ft_a is not None,
        score_complete=ft_h is not None and ft_a is not None,
        result_confirm_status="source_score_unconfirmed",
        identity_ok=True,
        quarantine_reason=None,
        event_json="{}",
        source_last_updated=None,
    )


def test_hkjc_duplicate_is_skipped_not_overlaid(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        ensure_footylogic_schema(conn)
        _insert_match(
            conn,
            source_key="hkjc|1",
            source="hkjc",
            date="2025-03-01",
            home="甲隊",
            away="乙隊",
            competition="聯賽A",
            ft_h=2,
            ft_a=2,
            confirm=HKJC_RESULT_CONFIRM_STATUS,
        )
        # Pre-existing FL row with different score — skip must not overwrite
        _insert_match(
            conn,
            source_key="fl|1",
            source="footylogic",
            date="2025-03-01",
            home="甲隊",
            away="乙隊",
            competition="聯賽A",
            ft_h=1,
            ft_a=0,
            confirm="source_score_unconfirmed",
        )
        by_date = load_hkjc_by_date_for_year(conn, year=2025)
        incoming = [
            _parsed(
                source_key="fl|1",
                date="2025-03-01",
                home="甲隊",
                away="乙隊",
                competition="聯賽A",
                ft_h=9,
                ft_a=9,
            ),
            _parsed(
                source_key="fl|only",
                date="2025-03-02",
                home="丙隊",
                away="丁隊",
                competition="盃賽B",
                ft_h=3,
                ft_a=1,
            ),
            _parsed(
                source_key="fl|swap",
                date="2025-03-03",
                home="戊隊",
                away="己隊",
                competition="聯賽A",
            ),
        ]
        # HKJC twin for swap (reversed)
        _insert_match(
            conn,
            source_key="hkjc|swap",
            source="hkjc",
            date="2025-03-03",
            home="己隊",
            away="戊隊",
            competition="聯賽A",
            ft_h=0,
            ft_a=1,
            confirm=HKJC_RESULT_CONFIRM_STATUS,
        )
        by_date = load_hkjc_by_date_for_year(conn, year=2025)
        to_upsert, stats = filter_fl_rows_for_hkjc_skip(
            conn, incoming, hkjc_by_date=by_date
        )
        upsert_matches(conn, to_upsert)
        conn.commit()

        assert stats["hkjc_duplicate_skip"] == 1
        assert stats["fl_only"] == 1
        assert stats["uncertain"] == 1
        keys = {r.source_key for r in to_upsert}
        assert "fl|1" not in keys
        assert "fl|only" in keys
        assert "fl|swap" in keys

        # Existing FL duplicate NOT updated (still 1-0, not 9-9 and not HKJC 2-2)
        fl = conn.execute(
            f"SELECT score_ft_home, score_ft_away, result_confirm_status "
            f"FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key='fl|1'"
        ).fetchone()
        assert fl["score_ft_home"] == 1
        assert fl["score_ft_away"] == 0
        assert fl["result_confirm_status"] == "source_score_unconfirmed"

        only = conn.execute(
            f"SELECT score_ft_home FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key='fl|only'"
        ).fetchone()
        assert only["score_ft_home"] == 3

        dec = conn.execute(
            "SELECT status FROM footylogic_hkjc_overlay WHERE fl_source_key='fl|1'"
        ).fetchone()
        assert dec["status"] == STATUS_HKJC_DUP_SKIP


def test_hkjc_void_still_skips_fl_duplicate(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        ensure_footylogic_schema(conn)
        _insert_match(
            conn,
            source_key="hkjc|v",
            source="hkjc",
            date="2025-04-01",
            home="A",
            away="B",
            competition="X",
            ft_h=None,
            ft_a=None,
            confirm=HKJC_RESULT_VOID_STATUS,
        )
        by_date = load_hkjc_by_date_for_year(conn, year=2025)
        to_upsert, stats = filter_fl_rows_for_hkjc_skip(
            conn,
            [_parsed(source_key="fl|v", date="2025-04-01", home="A", away="B", competition="X")],
            hkjc_by_date=by_date,
        )
        assert stats["hkjc_duplicate_skip"] == 1
        assert to_upsert == []
        upsert_matches(conn, to_upsert)
        conn.commit()
        n = conn.execute(
            f"SELECT COUNT(*) c FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key='fl|v'"
        ).fetchone()["c"]
        assert n == 0


def test_audit_does_not_overwrite_existing_fl(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        ensure_footylogic_schema(conn)
        _insert_match(
            conn,
            source_key="fl|1",
            source="footylogic",
            date="2025-03-01",
            home="甲隊",
            away="乙隊",
            competition="聯賽A",
            ft_h=1,
            ft_a=0,
            confirm="source_score_unconfirmed",
        )
        _insert_match(
            conn,
            source_key="hkjc|1",
            source="hkjc",
            date="2025-03-01",
            home="甲隊",
            away="乙隊",
            competition="聯賽A",
            ft_h=2,
            ft_a=2,
            confirm=HKJC_RESULT_CONFIRM_STATUS,
        )
        stats = run_hkjc_duplicate_skip_audit_for_year(conn, year=2025)
        conn.commit()
        assert stats["hkjc_duplicate_skip"] == 1
        fl = conn.execute(
            f"SELECT score_ft_home, score_ft_away FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key='fl|1'"
        ).fetchone()
        assert (fl["score_ft_home"], fl["score_ft_away"]) == (1, 0)


def test_upsert_null_score_does_not_clear_existing(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        ensure_footylogic_schema(conn)
        _insert_match(
            conn,
            source_key="fl|score",
            source="footylogic",
            date="2025-05-01",
            home="甲",
            away="乙",
            competition="聯賽",
            ft_h=2,
            ft_a=1,
            confirm="source_score_unconfirmed",
        )
        row = _parsed(
            source_key="fl|score",
            date="2025-05-01",
            home="甲",
            away="乙",
            competition="聯賽",
            ft_h=None,
            ft_a=None,
        )
        upsert_matches(conn, [row])
        conn.commit()
        got = conn.execute(
            f"SELECT score_ft_home, score_ft_away FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key='fl|score'"
        ).fetchone()
        assert got["score_ft_home"] == 2
        assert got["score_ft_away"] == 1


def test_different_fl_source_keys_both_kept(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        ensure_footylogic_schema(conn)
        a = _parsed(source_key="fl|a", date="2025-06-01", home="甲", away="乙", competition="盃")
        b = _parsed(source_key="fl|b", date="2025-06-01", home="甲", away="乙", competition="盃")
        upsert_matches(conn, [a, b])
        conn.commit()
        n = conn.execute(
            f"SELECT COUNT(*) c FROM {FOOTYLOGIC_MATCHES_TABLE} "
            f"WHERE source='footylogic' AND match_date_hkt='2025-06-01' "
            f"AND home_team_name='甲' AND away_team_name='乙'"
        ).fetchone()["c"]
        assert n == 2


def test_integrity_blocks_complete_on_failure_and_uncertain() -> None:
    report = _empty_report(2025)
    report["catalog"]["ok"] = True
    report["catalog"]["by_category"] = {
        "1": {"ok": True},
        "2": {"ok": True},
        "3": {"ok": True},
        "4": {"ok": True},
    }
    report["jobs"] = {"total": 2, "pending": 0, "failed": 0, "running": 0, "completed": 2}
    report["matches"] = {"uncertain": 1}
    integrity = _compute_integrity(report)
    assert integrity["can_claim_complete"] is False
    assert _finalize_status(report) == STATUS_PARTIAL

    report2 = dict(report)
    report2["matches"] = {"uncertain": 0}
    report2["failures"] = []
    assert _compute_integrity(report2)["can_claim_complete"] is True
    assert _finalize_status(report2) == STATUS_SUCCESS


def test_year_download_resume_skips_completed(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)

    calls = {"results": 0}

    def router(params: dict[str, Any]) -> _FakeHttpResult:
        if "categoryId" in params:
            return _competitions_route(int(params["categoryId"]))
        if "date" in params:
            calls["results"] += 1
            return _FakeHttpResult(
                {
                    "statusCode": 200,
                    "data": {
                        "matches": [],
                        "events": [],
                    },
                }
            )
        cid = str(params.get("competitionId"))
        return _FakeHttpResult(
            {
                "data": [
                    {
                        "seasonId": f"s-{cid}",
                        "seasonName": "2025",
                        "fixtureRange": ["2025-01"],
                    }
                ]
            }
        )

    client = _FakeClient(
        {
            ("/tournament/competitions", "*"): router,
            ("/tournament/fixtures-seasons", "*"): router,
            ("/tournament/fixtures-results", "*"): router,
        }
    )
    # First run may fail parse on empty events — ensure parse accepts empty.
    # Use payload shape that parse_fixtures_results_payload accepts.
    from hppv import footylogic_parse as fp

    def fake_parse(body, **kwargs):  # type: ignore[no-untyped-def]
        return [], [], {"events_raw": 0, "in_range": 0}

    # Patch parse for empty stub
    import hppv.footylogic_history_year as hy

    original = hy.parse_fixtures_results_payload if hasattr(hy, "parse_fixtures_results_payload") else None
    # run_year_download imports parse inside function — patch module used there
    import hppv.footylogic_parse as parse_mod

    monkey_parse = fake_parse
    old = parse_mod.parse_fixtures_results_payload
    parse_mod.parse_fixtures_results_payload = monkey_parse  # type: ignore[assignment]
    try:
        r1 = run_year_download(db, 2025, client=client, run_id="r1")
        first_calls = calls["results"]
        assert first_calls == 3
        r2 = run_year_download(db, 2025, client=client, run_id="r2", skip_discover=False)
        # Second discover reseeds; completed jobs not re-fetched
        assert calls["results"] == first_calls
        assert r1["jobs"]["completed"] == 3
        assert r2["jobs"]["completed"] == 3
    finally:
        parse_mod.parse_fixtures_results_payload = old  # type: ignore[assignment]
        _ = original
        _ = fp


def test_api_history_year_start_and_reject_graphql_by_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)

    started: dict[str, Any] = {}

    def fake_start(db_path, year, resume=False, limit=None):  # type: ignore[no-untyped-def]
        started["year"] = year
        started["resume"] = resume
        return {"ok": True, "run_id": "x", "year": year, "status": "loading", "resume": resume}

    monkeypatch.setattr(wa, "start_year_download_background", fake_start)
    client = wa.app.test_client()
    r = client.post(
        "/api/soccer/history-year/start",
        json={"year": 2024, "resume": False},
    )
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert started["year"] == 2024

    bad = client.post(
        "/api/soccer/graphql/send",
        json={
            "request_key": "match_results_by_year",
            "payload": json.dumps(
                {"query": "query matchResults { x }", "variables": {}, "operationName": "matchResults"}
            ),
        },
    )
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "use_footylogic_year_api"

    st = client.get("/api/soccer/history-year/status?year=2024")
    assert st.status_code == 200
    assert st.get_json()["ok"] is True


def test_match_helper_competition_mismatch_uncertain() -> None:
    class R(dict):
        def __getitem__(self, k):  # type: ignore[no-untyped-def]
            return dict.get(self, k)

        def keys(self):  # type: ignore[no-untyped-def]
            return dict.keys(self)

    fl = R(
        match_date_hkt="2025-01-01",
        home_team_name="甲",
        away_team_name="乙",
        competition_name="聯賽A",
        source_key="fl",
    )
    hk = R(
        match_date_hkt="2025-01-01",
        home_team_name="甲",
        away_team_name="乙",
        competition_name="聯賽B",
        source_key="hk",
    )
    status, row, reason = match_fl_to_hkjc(fl, [hk])  # type: ignore[arg-type]
    assert status == STATUS_UNCERTAIN
    assert row is None
    assert reason == "competition_mismatch"
    # Must not auto-skip on uncertain
    assert status != STATUS_HKJC_DUP_SKIP
