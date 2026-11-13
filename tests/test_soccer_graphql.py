from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hppv.soccer_graphql import (
    SoccerGraphQLSendError,
    parse_and_validate_send_payload,
    reset_rate_limit_for_tests,
    send_soccer_graphql,
)


VALID_QUERY = "query Ping { __typename }\n"


def test_parse_rejects_invalid_and_mutation() -> None:
    with pytest.raises(SoccerGraphQLSendError) as e1:
        parse_and_validate_send_payload("{")
    assert e1.value.code == "invalid_json"

    with pytest.raises(SoccerGraphQLSendError) as e2:
        parse_and_validate_send_payload(json.dumps({"query": ""}))
    assert e2.value.code == "empty_query"

    with pytest.raises(SoccerGraphQLSendError) as e3:
        parse_and_validate_send_payload(
            json.dumps({"query": VALID_QUERY, "variables": []})
        )
    assert e3.value.code == "invalid_fields"

    with pytest.raises(SoccerGraphQLSendError) as e4:
        parse_and_validate_send_payload(
            json.dumps({"query": "mutation X { y }", "url": "http://evil"})
        )
    assert e4.value.code in {"invalid_fields", "operation_not_allowed"}

    with pytest.raises(SoccerGraphQLSendError) as e5:
        parse_and_validate_send_payload(
            json.dumps({"query": "mutation Do { x }"})
        )
    assert e5.value.code == "operation_not_allowed"

    with pytest.raises(SoccerGraphQLSendError) as e6:
        parse_and_validate_send_payload(
            json.dumps({"query": "subscription S { x }"})
        )
    assert e6.value.code == "operation_not_allowed"

    ok = parse_and_validate_send_payload(
        json.dumps({"query": VALID_QUERY, "variables": {}, "operationName": "Ping"})
    )
    assert ok.query == VALID_QUERY


def test_send_http_success_and_graphql_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_rate_limit_for_tests()

    def post_ok(url, headers=None, data=None, timeout=None, allow_redirects=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = json.dumps(
            {"data": {"matches": [{"id": "1", "foPools": [{"oddsType": "HAD"}]}]}}
        ).encode("utf-8")
        resp.encoding = "utf-8"
        return resp

    payload = parse_and_validate_send_payload(json.dumps({"query": VALID_QUERY}))
    result = send_soccer_graphql(payload, post_func=post_ok)
    assert result.outcome == "success"
    assert result.http_status == 200
    assert result.match_count == 1
    assert result.has_had_odds is True

    reset_rate_limit_for_tests()

    def post_gql_err(url, headers=None, data=None, timeout=None, allow_redirects=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = json.dumps({"errors": [{"message": "x"}]}).encode("utf-8")
        resp.encoding = "utf-8"
        return resp

    result2 = send_soccer_graphql(payload, post_func=post_gql_err)
    assert result2.outcome == "graphql_errors"

    reset_rate_limit_for_tests()

    def post_partial(url, headers=None, data=None, timeout=None, allow_redirects=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = json.dumps(
            {"data": {"matches": []}, "errors": [{"message": "partial"}]}
        ).encode("utf-8")
        resp.encoding = "utf-8"
        return resp

    result3 = send_soccer_graphql(payload, post_func=post_partial)
    assert result3.outcome == "partial_errors"
    assert result3.match_count == 0


def test_send_http_error_non_json_and_rate_limit() -> None:
    reset_rate_limit_for_tests()
    payload = parse_and_validate_send_payload(json.dumps({"query": VALID_QUERY}))

    def post_500(url, headers=None, data=None, timeout=None, allow_redirects=None):
        resp = MagicMock()
        resp.status_code = 500
        resp.content = b"oops"
        resp.encoding = "utf-8"
        return resp

    r1 = send_soccer_graphql(payload, post_func=post_500)
    assert r1.outcome == "http_error"

    reset_rate_limit_for_tests()

    def post_html(url, headers=None, data=None, timeout=None, allow_redirects=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"<html>nope</html>"
        resp.encoding = "utf-8"
        return resp

    r2 = send_soccer_graphql(payload, post_func=post_html)
    assert r2.outcome == "non_json"

    reset_rate_limit_for_tests()
    send_soccer_graphql(payload, post_func=post_500)
    with pytest.raises(SoccerGraphQLSendError) as exc:
        send_soccer_graphql(payload, post_func=post_500)
    assert exc.value.code == "rate_limited"

    # Scheduler path waits instead of failing.
    reset_rate_limit_for_tests()
    send_soccer_graphql(payload, post_func=post_500)
    r_wait = send_soccer_graphql(
        payload, post_func=post_500, wait_for_rate_limit=True
    )
    assert r_wait.outcome == "http_error"


def test_api_send_rejects_mutation_and_does_not_write_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa

    reset_rate_limit_for_tests()
    preset = tmp_path / "matches_and_odds.json"
    preset.write_text(
        json.dumps({"query": VALID_QUERY, "variables": {}}),
        encoding="utf-8",
    )
    before = preset.read_text(encoding="utf-8")
    monkeypatch.setattr(wa, "SOCCER_REQUESTS_DIR", tmp_path)
    client = wa.app.test_client()

    r = client.post(
        "/api/soccer/graphql/send",
        json={"payload": json.dumps({"query": "mutation M { x }"})},
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "operation_not_allowed"
    assert preset.read_text(encoding="utf-8") == before

    def post_ok(url, headers=None, data=None, timeout=None, allow_redirects=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = json.dumps({"data": {"matches": []}}).encode("utf-8")
        resp.encoding = "utf-8"
        return resp

    monkeypatch.setattr(
        wa,
        "send_soccer_graphql",
        lambda payload, **kwargs: send_soccer_graphql(payload, post_func=post_ok),
    )
    reset_rate_limit_for_tests()
    r2 = client.post(
        "/api/soccer/graphql/send",
        json={"payload": json.dumps({"query": VALID_QUERY, "variables": {}})},
    )
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["ok"] is True
    assert body["outcome"] == "success"
    assert preset.read_text(encoding="utf-8") == before


def test_page_has_send_not_download_icon() -> None:
    from hppv.web_app import app

    client = app.test_client()
    html = client.get("/soccer/daily").get_data(as_text=True)
    assert 'id="btn-send-request"' in html
    assert "btn-download-soccer" not in html
    assert "icon_download_soccer" not in html


def test_soccer_history_route_redirects_to_tab() -> None:
    import re

    from hppv.web_app import app

    client = app.test_client()
    resp = client.get("/soccer/history", follow_redirects=False)
    assert resp.status_code in (301, 302, 303, 307, 308)
    loc = resp.headers.get("Location") or ""
    assert "/soccer/daily" in loc
    assert "tab=history" in loc

    html = client.get("/soccer/daily?tab=history").get_data(as_text=True)
    assert 'id="tab-btn-history"' in html
    assert 'id="tab-panel-history"' in html
    assert 'data-tab="history"' in html
    assert re.search(
        r'class="settings-tab active"[^>]*id="tab-btn-history"',
        html,
        flags=re.S,
    )
    assert re.search(
        r'class="tab-panel active"[^>]*id="tab-panel-history"',
        html,
        flags=re.S,
    )


def test_page_has_tasks_tab_ui_defaults() -> None:
    import re

    from hppv.web_app import app

    client = app.test_client()
    html = client.get("/soccer/daily").get_data(as_text=True)

    assert 'id="tab-btn-matches"' in html
    assert 'id="tab-btn-history"' in html
    assert 'id="tab-btn-download"' in html
    assert 'id="tab-btn-tasks"' in html
    assert 'id="tab-btn-layout"' not in html
    assert 'data-tab="layout"' not in html
    assert 'id="tab-panel-layout"' not in html
    assert 'id="btn-open-layout"' in html
    assert 'id="matches-layout-mode"' in html
    assert 'id="matches-list-mode"' in html
    assert 'id="matches-layout-panel"' not in html
    assert 'id="tab-panel-history"' in html
    assert 'id="tab-panel-tasks"' in html
    assert 'data-tab="history"' in html
    assert 'data-tab="tasks"' in html
    # Top tabs: matches → history → download → tasks (no layout tab)
    m = html.find('id="tab-btn-matches"')
    h = html.find('id="tab-btn-history"')
    d = html.find('id="tab-btn-download"')
    t = html.find('id="tab-btn-tasks"')
    assert 0 <= m < h < d < t
    # Layout entry only inside daily matches toolbar (above pager)
    btn = html.find('id="btn-open-layout"')
    pager = html.find('id="matches-pager"')
    hist = html.find('id="tab-panel-history"')
    assert 0 <= btn < pager < hist
    # Default: list mode class; layout mode markup present but exclusive
    assert "matches-mode-list" in html
    assert 'id="layout-mode-toolbar"' in html
    assert 'id="btn-layout-save"' in html
    assert 'id="btn-layout-cancel"' in html
    assert "function setLayoutMode(" in html
    # Status filter UI hidden until source mapping is connected.
    assert 'id="match-status-filter"' not in html
    assert "status-mapping-note" not in html
    assert "update_odds_and_results" in html
    assert "match_results" in html
    assert "match_results_by_year" in html
    assert 'value="matches_and_odds" selected' in html

    # Backend is source of truth: HTML must not hard-check enabled.
    assert 'id="task-odds-enabled"' in html
    assert not re.search(r'id="task-odds-enabled"[^>]*\bchecked\b', html)
    assert not re.search(r'id="task-odds-enabled"[^>]*\bdisabled\b', html)
    assert 'id="task-matches-enabled"' in html
    assert not re.search(r'id="task-matches-enabled"[^>]*\bchecked\b', html)
    assert 'id="task-results-enabled"' in html
    assert not re.search(r'id="task-results-enabled"[^>]*\bchecked\b', html)
    assert 'id="task-odds-interval"' in html
    assert 'id="task-odds-unit"' in html
    assert 'id="task-odds-status"' in html
    assert 'id="task-matches-interval"' in html
    assert 'id="task-matches-unit"' in html
    assert 'id="task-results-interval"' in html
    assert 'id="task-results-unit"' in html
    assert 'id="task-results-status"' in html
    assert "/api/soccer/tasks/update-odds" in html
    assert "/api/soccer/tasks/update-matches" in html
    assert "/api/soccer/tasks/update-results" in html
    for n in range(1, 11):
        assert f'value="{n}"' in html
    assert re.search(r'id="task-odds-interval"[\s\S]*?<option value="1" selected>', html)
    assert re.search(r'id="task-odds-unit"[\s\S]*?<option value="mins" selected>', html)
    assert 'value="hour"' in html
    assert 'value="day"' in html
    assert 'id="task-odds-last"' in html
    assert 'id="task-odds-next"' in html
    assert 'id="task-matches-last"' in html
    assert 'id="task-matches-next"' in html
    assert 'id="task-results-last"' in html
    assert 'id="task-results-next"' in html
    assert "尚未排程" in html or "Not scheduled" in html
    assert "onchange=\"fetch" not in html
    assert "onclick=\"fetch" not in html


def test_task_translation_keys_seeded(tmp_path) -> None:
    from hppv.soccer_db import get_connection, init_soccer_db

    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    keys = {
        "soccer.daily.tab.tasks",
        "soccer.daily.task.update_odds",
        "soccer.daily.task.update_matches",
        "soccer.daily.task.update_results",
        "soccer.daily.task.results_scope_note",
        "soccer.daily.task.last_updated",
        "soccer.daily.task.next_update",
        "soccer.daily.task.not_scheduled",
        "soccer.daily.task.unit.mins",
        "soccer.daily.task.unit.hour",
        "soccer.daily.task.unit.day",
    }
    with get_connection(db_path) as conn:
        found = {
            str(r["text_key"])
            for r in conn.execute(
                "SELECT text_key FROM translations WHERE text_key LIKE 'soccer.daily.tab.tasks'"
                " OR text_key LIKE 'soccer.daily.task.%'"
            ).fetchall()
        }
    assert keys <= found

