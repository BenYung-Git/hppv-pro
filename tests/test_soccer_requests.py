from __future__ import annotations

import json
from pathlib import Path

import pytest

from datetime import date

from hppv.config import default_config
from hppv.soccer_requests import (
    PRESET_MATCH_RESULTS_BY_YEAR,
    SoccerRequestPresetError,
    ensure_placeholder_preset_files,
    load_soccer_request_preset,
    previous_complete_calendar_year_bounds,
    resolve_preset_path,
)


def test_requests_dir_from_central_config_not_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = default_config()
    assert cfg.soccer_requests_dir == cfg.data_dir / "soccer_requests"
    assert cfg.soccer_requests_dir.is_absolute()
    # Changing cwd must not change configured path.
    monkeypatch.chdir(tmp_path)
    cfg2 = default_config()
    assert cfg2.soccer_requests_dir == cfg.soccer_requests_dir


def test_resolve_registered_and_reject_unknown(tmp_path: Path) -> None:
    path = resolve_preset_path(tmp_path, "matches_and_odds")
    assert path == (tmp_path / "matches_and_odds.json").resolve()
    with pytest.raises(SoccerRequestPresetError) as exc:
        resolve_preset_path(tmp_path, "../secret")
    assert exc.value.code == "unsupported_type"
    with pytest.raises(SoccerRequestPresetError) as exc2:
        resolve_preset_path(tmp_path, "not_registered")
    assert exc2.value.code == "unsupported_type"


def test_load_valid_payload_preserves_query(tmp_path: Path) -> None:
    query = "query Demo {\n  field\n}\n"
    (tmp_path / "matches_and_odds.json").write_text(
        json.dumps({"query": query, "variables": {"a": 1}, "operationName": "Demo"}, ensure_ascii=False),
        encoding="utf-8",
    )
    payload, display = load_soccer_request_preset(tmp_path, "matches_and_odds")
    assert payload.query == query
    assert payload.variables == {"a": 1}
    assert payload.operation_name == "Demo"
    assert json.loads(display)["query"] == query


def test_empty_query_is_not_success(tmp_path: Path) -> None:
    (tmp_path / "matches_and_odds.json").write_text(
        json.dumps({"query": "", "variables": {}}),
        encoding="utf-8",
    )
    with pytest.raises(SoccerRequestPresetError) as exc:
        load_soccer_request_preset(tmp_path, "matches_and_odds")
    assert exc.value.code == "empty_query"


def test_missing_file_and_invalid_json_and_fields(tmp_path: Path) -> None:
    with pytest.raises(SoccerRequestPresetError) as e1:
        load_soccer_request_preset(tmp_path, "matches_and_odds")
    assert e1.value.code == "file_missing"

    (tmp_path / "matches_and_odds.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(SoccerRequestPresetError) as e2:
        load_soccer_request_preset(tmp_path, "matches_and_odds")
    assert e2.value.code == "invalid_json"

    (tmp_path / "matches_and_odds.json").write_text(
        json.dumps({"query": "ok", "variables": ["bad"]}),
        encoding="utf-8",
    )
    with pytest.raises(SoccerRequestPresetError) as e3:
        load_soccer_request_preset(tmp_path, "matches_and_odds")
    assert e3.value.code == "invalid_fields"


def test_placeholder_does_not_overwrite(tmp_path: Path) -> None:
    ensure_placeholder_preset_files(tmp_path)
    path = tmp_path / "matches_and_odds.json"
    assert path.is_file()
    path.write_text(json.dumps({"query": "KEEP", "variables": {}}), encoding="utf-8")
    ensure_placeholder_preset_files(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["query"] == "KEEP"


def test_api_empty_query_and_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hppv import web_app as wa

    monkeypatch.setattr(wa, "SOCCER_REQUESTS_DIR", tmp_path)
    client = wa.app.test_client()

    (tmp_path / "matches_and_odds.json").write_text(
        json.dumps({"query": "", "variables": {}}),
        encoding="utf-8",
    )
    r = client.get("/api/soccer/request-presets/matches_and_odds")
    assert r.status_code == 400
    assert r.get_json()["error"] == "empty_query"

    query = "query X { y }"
    (tmp_path / "matches_and_odds.json").write_text(
        json.dumps({"query": query, "variables": {}}),
        encoding="utf-8",
    )
    r2 = client.get("/api/soccer/request-presets/matches_and_odds")
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["ok"] is True
    assert json.loads(body["display"])["query"] == query

    r3 = client.get("/api/soccer/request-presets/not_registered")
    assert r3.status_code == 400
    assert r3.get_json()["error"] == "unsupported_type"


def test_previous_complete_calendar_year_bounds() -> None:
    assert previous_complete_calendar_year_bounds(date(2026, 11, 5)) == (
        "2025-01-01",
        "2025-12-31",
    )
    assert previous_complete_calendar_year_bounds(date(2025, 1, 1)) == (
        "2024-01-01",
        "2024-12-31",
    )


def test_match_results_by_year_loads_dynamic_previous_year(tmp_path: Path) -> None:
    query = "query matchResults($startDate: String, $endDate: String) {\n  x\n}\n"
    (tmp_path / "match_results.json").write_text(
        json.dumps(
            {
                "operationName": "matchResults",
                "variables": {
                    "startDate": "2026-11-04",
                    "endDate": "2026-11-05",
                    "startIndex": 0,
                    "endIndex": 0,
                    "teamId": None,
                },
                "query": query,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload, display = load_soccer_request_preset(
        tmp_path,
        PRESET_MATCH_RESULTS_BY_YEAR,
        today=date(2026, 11, 5),
    )
    assert payload.query == query
    assert payload.operation_name == "matchResults"
    assert payload.variables == {
        "startDate": "2025-01-01",
        "endDate": "2025-12-31",
        "startIndex": 0,
        "endIndex": 0,
        "teamId": None,
    }
    shown = json.loads(display)
    assert shown["variables"]["startDate"] == "2025-01-01"
    assert shown["variables"]["endDate"] == "2025-12-31"
    # Static short-range preset remains unchanged.
    payload2, _ = load_soccer_request_preset(tmp_path, "match_results")
    assert payload2.variables["startDate"] == "2026-11-04"


def test_api_match_results_by_year_display_not_hardcoded_2025(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa
    from hppv import soccer_requests as sr

    monkeypatch.setattr(wa, "SOCCER_REQUESTS_DIR", tmp_path)
    query = "query matchResults { x }"
    (tmp_path / "match_results.json").write_text(
        json.dumps(
            {
                "operationName": "matchResults",
                "variables": {
                    "startDate": "2099-01-01",
                    "endDate": "2099-12-31",
                    "startIndex": 0,
                    "endIndex": 0,
                    "teamId": None,
                },
                "query": query,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sr,
        "previous_complete_calendar_year_bounds",
        lambda today=None: ("2030-01-01", "2030-12-31"),
    )
    client = wa.app.test_client()
    r = client.get("/api/soccer/request-presets/match_results_by_year")
    assert r.status_code == 200
    body = json.loads(r.get_json()["display"])
    assert body["variables"]["startDate"] == "2030-01-01"
    assert body["variables"]["endDate"] == "2030-12-31"
    assert body["query"] == query

