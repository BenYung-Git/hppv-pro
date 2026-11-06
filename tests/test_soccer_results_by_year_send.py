from __future__ import annotations

import json
from pathlib import Path

import pytest

from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_graphql import reset_rate_limit_for_tests
from hppv.soccer_ingest import REQUEST_KEY_MATCH_RESULTS_BY_YEAR


def _seed(db: Path, match_id: str = "50079013") -> None:
    init_soccer_db(db)
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO soccer_daily (
              match_id, match_code, match_date, kickoff_at,
              competition_code, competition_name_zh, home_team, away_team,
              match_status, created_at, updated_at
            ) VALUES (?, 'FB8287', '2026-11-05', '2026-11-05T20:00:00+08:00',
                      'UCL', '聯賽', '主隊', '客隊', 'PREEVENT',
                      '2026-11-04T00:00:00+00:00', '2026-11-04T00:00:00+00:00');
            """,
            (match_id,),
        )
        conn.commit()


def test_send_uses_request_box_dates_not_preset_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One send: outbound body must match Request-box payload (user-edited dates)."""
    from hppv import web_app as wa

    db = tmp_path / "soccer.sqlite3"
    _seed(db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)
    monkeypatch.setattr(wa, "SOCCER_REQUESTS_DIR", tmp_path)

    query = (
        "query matchResults($startDate: String, $endDate: String, "
        "$startIndex: Int,$endIndex: Int,$teamId: String) { __typename }"
    )
    (tmp_path / "match_results.json").write_text(
        json.dumps(
            {
                "operationName": "matchResults",
                "variables": {
                    "startDate": "2025-01-01",
                    "endDate": "2025-12-31",
                    "startIndex": 0,
                    "endIndex": 0,
                    "teamId": None,
                },
                "query": query,
            }
        ),
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_send(payload, **_kwargs):
        from hppv.soccer_graphql import SoccerGraphQLSendResult

        captured["variables"] = payload.variables
        captured["operation"] = payload.operation_name
        # Empty success response → ingest skipped_empty (no auto multi-request).
        body = json.dumps(
            {"data": {"matchNumByDate": {"total": 0}, "matches": []}},
            ensure_ascii=False,
        )
        return SoccerGraphQLSendResult(
            outcome="success",
            http_status=200,
            elapsed_ms=1,
            body_text=body,
            truncated=False,
            match_count=0,
            has_had_odds=False,
        )

    monkeypatch.setattr(wa, "send_soccer_graphql", fake_send)
    reset_rate_limit_for_tests()
    client = wa.app.test_client()

    user_payload = {
        "operationName": "matchResults",
        "variables": {
            "startDate": "2026-11-04",
            "endDate": "2026-11-05",
            "startIndex": 0,
            "endIndex": 0,
            "teamId": None,
        },
        "query": query,
    }
    resp = client.post(
        "/api/soccer/graphql/send",
        json={
            "request_key": REQUEST_KEY_MATCH_RESULTS_BY_YEAR,
            "payload": json.dumps(user_payload, ensure_ascii=False),
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert captured["variables"]["startDate"] == "2026-11-04"
    assert captured["variables"]["endDate"] == "2026-11-05"
    assert captured["operation"] == "matchResults"
    assert data["ingest"]["request_key"] == REQUEST_KEY_MATCH_RESULTS_BY_YEAR
    assert data["ingest"]["code"] == "skipped_empty"


def test_by_year_creates_unknown_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa
    from hppv.soccer_graphql import SoccerGraphQLSendResult
    from hppv.soccer_db import get_connection

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)

    def fake_send(payload, **_kwargs):
        body = {
            "data": {
                "matchNumByDate": {"total": 1},
                "matches": [
                    {
                        "id": "unknown-only",
                        "status": "MATCHENDED",
                        "frontEndId": "FB9999",
                        "kickOffTime": "2015-11-04T19:00:00.000+08:00",
                        "homeTeam": {"name_ch": "主隊"},
                        "awayTeam": {"name_ch": "客隊"},
                        "tournament": {"code": "EPL", "name_ch": "英超"},
                        "results": [
                            {
                                "homeResult": 1,
                                "awayResult": 0,
                                "ttlCornerResult": -1,
                                "resultConfirmType": 5,
                                "payoutConfirmed": True,
                                "stageId": 3,
                                "resultType": 1,
                                "sequence": 1,
                            },
                            {
                                "homeResult": 2,
                                "awayResult": 0,
                                "ttlCornerResult": -1,
                                "resultConfirmType": 5,
                                "payoutConfirmed": True,
                                "stageId": 5,
                                "resultType": 1,
                                "sequence": 2,
                            },
                        ],
                    }
                ],
            }
        }
        return SoccerGraphQLSendResult(
            outcome="success",
            http_status=200,
            elapsed_ms=1,
            body_text=json.dumps(body, ensure_ascii=False),
            truncated=False,
            match_count=1,
            has_had_odds=False,
        )

    monkeypatch.setattr(wa, "send_soccer_graphql", fake_send)
    reset_rate_limit_for_tests()
    client = wa.app.test_client()
    payload = {
        "operationName": "matchResults",
        "variables": {
            "startDate": "2026-11-04",
            "endDate": "2026-11-05",
            "startIndex": 0,
            "endIndex": 0,
            "teamId": None,
        },
        "query": "query matchResults { x }",
    }
    resp = client.post(
        "/api/soccer/graphql/send",
        json={
            "request_key": REQUEST_KEY_MATCH_RESULTS_BY_YEAR,
            "payload": json.dumps(payload),
        },
    )
    assert resp.status_code == 200
    ing = resp.get_json()["ingest"]
    assert ing["code"] == "success"
    assert ing["inserted"] == 1
    assert ing["updated"] == 0
    assert ing["results_applied"] is True
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT home_team, score_ft_home FROM soccer_daily WHERE match_id='unknown-only';"
        ).fetchone()
        assert row["home_team"] == "主隊"
        assert row["score_ft_home"] == 2


def test_short_match_results_still_skips_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa
    from hppv.soccer_graphql import SoccerGraphQLSendResult
    from hppv.soccer_db import get_connection
    from hppv.soccer_ingest import REQUEST_KEY_MATCH_RESULTS

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)

    def fake_send(payload, **_kwargs):
        body = {
            "data": {
                "matchNumByDate": {"total": 1},
                "matches": [
                    {
                        "id": "unknown-short",
                        "status": "MATCHENDED",
                        "frontEndId": "FB1",
                        "kickOffTime": "2015-11-04T19:00:00.000+08:00",
                        "homeTeam": {"name_ch": "主"},
                        "awayTeam": {"name_ch": "客"},
                        "results": [
                            {
                                "homeResult": 0,
                                "awayResult": 0,
                                "ttlCornerResult": -1,
                                "resultConfirmType": 5,
                                "payoutConfirmed": True,
                                "stageId": 3,
                                "resultType": 1,
                                "sequence": 1,
                            },
                            {
                                "homeResult": 1,
                                "awayResult": 0,
                                "ttlCornerResult": -1,
                                "resultConfirmType": 5,
                                "payoutConfirmed": True,
                                "stageId": 5,
                                "resultType": 1,
                                "sequence": 2,
                            },
                        ],
                    }
                ],
            }
        }
        return SoccerGraphQLSendResult(
            outcome="success",
            http_status=200,
            elapsed_ms=1,
            body_text=json.dumps(body, ensure_ascii=False),
            truncated=False,
            match_count=1,
            has_had_odds=False,
        )

    monkeypatch.setattr(wa, "send_soccer_graphql", fake_send)
    reset_rate_limit_for_tests()
    client = wa.app.test_client()
    payload = {
        "operationName": "matchResults",
        "variables": {
            "startDate": "2026-11-04",
            "endDate": "2026-11-05",
            "startIndex": 0,
            "endIndex": 0,
            "teamId": None,
        },
        "query": "query matchResults { x }",
    }
    resp = client.post(
        "/api/soccer/graphql/send",
        json={
            "request_key": REQUEST_KEY_MATCH_RESULTS,
            "payload": json.dumps(payload),
        },
    )
    assert resp.status_code == 200
    ing = resp.get_json()["ingest"]
    assert ing["code"] == "skipped_no_existing"
    assert ing["skipped_missing"] == 1
    assert ing["inserted"] == 0
    with get_connection(db) as conn:
        assert conn.execute("SELECT count(*) AS c FROM soccer_daily").fetchone()["c"] == 0
