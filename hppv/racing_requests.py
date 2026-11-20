"""Racing GraphQL request presets — same pattern as soccer_requests."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hppv.racing_hkjc_graphql import RACE_MEETINGS_QUERY, SP4E_RACING_PMPOOL_QUERY

logger = logging.getLogger(__name__)

# Registered presets only. Frontend sends the key; never treat key as a file path.
RACING_REQUEST_PRESETS: dict[str, str] = {
    "racecard": "racecard.json",
    "update_odds": "update_odds.json",
}

REQUEST_KEY_RACECARD = "racecard"
REQUEST_KEY_UPDATE_ODDS = "update_odds"


class RacingRequestPresetError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RacingRequestPayload:
    query: str
    variables: dict[str, Any] | None
    operation_name: str | None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"query": self.query}
        if self.variables is not None:
            out["variables"] = self.variables
        if self.operation_name is not None:
            out["operationName"] = self.operation_name
        return out


def resolve_preset_path(requests_dir: Path, preset_id: str) -> Path:
    from hppv.shared_preset import SharedPresetError, resolve_preset_path as shared_resolve

    try:
        return shared_resolve(
            requests_dir, preset_id, presets=RACING_REQUEST_PRESETS
        )
    except SharedPresetError as e:
        raise RacingRequestPresetError(e.code, e.detail) from e


def _validate_payload_object(data: Any) -> RacingRequestPayload:
    from hppv.shared_preset import SharedPresetError, validate_payload_object

    try:
        shared = validate_payload_object(data)
    except SharedPresetError as e:
        raise RacingRequestPresetError(e.code, e.detail) from e
    return RacingRequestPayload(
        query=shared.query,
        variables=shared.variables,
        operation_name=shared.operation_name,
    )


def format_payload_for_request_box(payload: RacingRequestPayload) -> str:
    return json.dumps(payload.as_dict(), ensure_ascii=False, indent=2)


def expected_query_for_preset(preset_id: str) -> str:
    """Whitelist query text for a registered preset (single source)."""
    if preset_id == REQUEST_KEY_RACECARD:
        return RACE_MEETINGS_QUERY
    if preset_id == REQUEST_KEY_UPDATE_ODDS:
        return SP4E_RACING_PMPOOL_QUERY
    raise RacingRequestPresetError("unsupported_type", preset_id)


def assert_payload_matches_whitelist(
    preset_id: str, payload: RacingRequestPayload
) -> None:
    """Reject rewritten/slimmed queries — 0 hardcode alternate bodies."""
    expected = expected_query_for_preset(preset_id)
    if payload.query != expected:
        raise RacingRequestPresetError(
            "query_not_whitelisted",
            "request query must match registered whitelist body exactly",
        )
    op = payload.operation_name
    if preset_id == REQUEST_KEY_RACECARD and op not in (None, "raceMeetings"):
        raise RacingRequestPresetError(
            "invalid_fields", "operationName must be raceMeetings"
        )
    if preset_id == REQUEST_KEY_UPDATE_ODDS and op not in (
        None,
        "sp4e_racing_pmPool",
    ):
        raise RacingRequestPresetError(
            "invalid_fields", "operationName must be sp4e_racing_pmPool"
        )


def load_racing_request_preset(
    requests_dir: Path, preset_id: str
) -> tuple[RacingRequestPayload, str]:
    from hppv.shared_preset import (
        SharedPresetError,
        SharedRequestPayload,
        load_request_preset,
    )

    def post_load(
        key: str, shared: SharedRequestPayload
    ) -> SharedRequestPayload:
        if shared.query.strip() == "":
            raise SharedPresetError("empty_query", "query is empty")
        payload = RacingRequestPayload(
            query=shared.query,
            variables=shared.variables,
            operation_name=shared.operation_name,
        )
        assert_payload_matches_whitelist(key, payload)
        return shared

    try:
        shared, display = load_request_preset(
            requests_dir,
            preset_id,
            presets=RACING_REQUEST_PRESETS,
            post_load=post_load,
        )
    except SharedPresetError as e:
        raise RacingRequestPresetError(e.code, e.detail) from e
    except RacingRequestPresetError:
        raise
    return (
        RacingRequestPayload(
            query=shared.query,
            variables=shared.variables,
            operation_name=shared.operation_name,
        ),
        display,
    )


def parse_racing_send_payload(raw: str) -> RacingRequestPayload:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RacingRequestPresetError("invalid_json", str(e)) from e
    payload = _validate_payload_object(data)
    if payload.query.strip() == "":
        raise RacingRequestPresetError("empty_query", "query is empty")
    return payload


def ensure_racing_preset_files(requests_dir: Path) -> None:
    """Create whitelist preset files if missing (do not overwrite)."""
    requests_dir.mkdir(parents=True, exist_ok=True)
    specs = {
        REQUEST_KEY_RACECARD: {
            "operationName": "raceMeetings",
            "query": RACE_MEETINGS_QUERY,
            "variables": {"date": None, "venueCode": None},
        },
        REQUEST_KEY_UPDATE_ODDS: {
            "operationName": "sp4e_racing_pmPool",
            "query": SP4E_RACING_PMPOOL_QUERY,
            "variables": {
                "date": None,
                "venueCode": None,
                "oddsTypes": ["WIN"],
                "raceNo": None,
            },
        },
    }
    for preset_id, body in specs.items():
        path = resolve_preset_path(requests_dir, preset_id)
        if path.is_file():
            continue
        path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
