from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")

# Registered presets only. Frontend sends the key; never treat key as a file path.
# update_odds_and_results reuses the verified matchList/HAD query file (no unverified result query).
# match_results is the verified official matchResults query (separate from odds update).
# match_results_by_year reuses the same verified query file; startDate/endDate are filled at load
# with the previous complete calendar year (no template engine).
SOCCER_REQUEST_PRESETS: dict[str, str] = {
    "matches_and_odds": "matches_and_odds.json",
    "update_odds_and_results": "matches_and_odds.json",
    "match_results": "match_results.json",
    "match_results_by_year": "match_results.json",
}

PRESET_MATCH_RESULTS_BY_YEAR = "match_results_by_year"


class SoccerRequestPresetError(Exception):
    """Preset load failure with a stable error code for UI mapping."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SoccerRequestPayload:
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
    """Map registered preset id to a file under requests_dir (path-traversal safe)."""
    filename = SOCCER_REQUEST_PRESETS.get(preset_id)
    if filename is None:
        raise SoccerRequestPresetError("unsupported_type", f"unknown preset_id={preset_id!r}")
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise SoccerRequestPresetError("unsupported_type", f"invalid registered filename={filename!r}")

    base = requests_dir.resolve()
    path = (base / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError as e:
        raise SoccerRequestPresetError("unsupported_type", "path escapes requests_dir") from e
    return path


def _validate_payload_object(data: Any) -> SoccerRequestPayload:
    if not isinstance(data, dict):
        raise SoccerRequestPresetError("invalid_fields", "payload root must be object")

    if "query" not in data:
        raise SoccerRequestPresetError("invalid_fields", "query is required")
    query = data["query"]
    if not isinstance(query, str):
        raise SoccerRequestPresetError("invalid_fields", "query must be string")

    variables: dict[str, Any] | None
    if "variables" not in data:
        variables = None
    else:
        raw_vars = data["variables"]
        if raw_vars is None:
            variables = None
        elif isinstance(raw_vars, dict):
            variables = raw_vars
        else:
            raise SoccerRequestPresetError("invalid_fields", "variables must be object or null")

    operation_name: str | None
    if "operationName" not in data:
        operation_name = None
    else:
        raw_op = data["operationName"]
        if raw_op is None:
            operation_name = None
        elif isinstance(raw_op, str) and raw_op.strip() != "":
            operation_name = raw_op
        else:
            raise SoccerRequestPresetError(
                "invalid_fields",
                "operationName must be null or non-empty string",
            )

    # Extra keys are ignored for forward-compat; do not rewrite query text.
    return SoccerRequestPayload(query=query, variables=variables, operation_name=operation_name)


def format_payload_for_request_box(payload: SoccerRequestPayload) -> str:
    """Pretty-print outer JSON; query string content is not rewritten."""
    return json.dumps(payload.as_dict(), ensure_ascii=False, indent=2)


def previous_complete_calendar_year_bounds(
    today: date | None = None,
) -> tuple[str, str]:
    """
    Previous complete calendar year as inclusive YYYY-MM-DD bounds (HKT date).

    Verified API rule: endDate is inclusive (same-day startDate==endDate returns that day).
    Example: on 2026-11-05 → ("2025-01-01", "2025-12-31").
    """
    if today is None:
        today = datetime.now(HKT).date()
    year = today.year - 1
    return f"{year}-01-01", f"{year}-12-31"


def apply_previous_calendar_year_variables(
    payload: SoccerRequestPayload,
    *,
    today: date | None = None,
) -> SoccerRequestPayload:
    """Copy payload variables and set startDate/endDate to previous complete calendar year."""
    start, end = previous_complete_calendar_year_bounds(today)
    base = dict(payload.variables) if isinstance(payload.variables, dict) else {}
    base["startDate"] = start
    base["endDate"] = end
    # Keep other required variables from the verified preset (startIndex/endIndex/teamId).
    if "startIndex" not in base:
        base["startIndex"] = 0
    if "endIndex" not in base:
        base["endIndex"] = 0
    if "teamId" not in base:
        base["teamId"] = None
    return SoccerRequestPayload(
        query=payload.query,
        variables=base,
        operation_name=payload.operation_name,
    )


def load_soccer_request_preset(
    requests_dir: Path,
    preset_id: str,
    *,
    today: date | None = None,
) -> tuple[SoccerRequestPayload, str]:
    """
    Load a registered preset payload.

    For match_results_by_year: reuse verified match_results.json query, then set
    startDate/endDate to the previous complete calendar year at load time.

    Returns (payload, display_text).
    Raises SoccerRequestPresetError with codes:
      unsupported_type | file_missing | invalid_json | invalid_fields | empty_query | read_failed
    """
    try:
        path = resolve_preset_path(requests_dir, preset_id)
    except SoccerRequestPresetError:
        raise

    if not path.is_file():
        raise SoccerRequestPresetError("file_missing", f"missing file for preset_id={preset_id}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.exception("failed reading soccer request preset %s: %s", preset_id, e)
        raise SoccerRequestPresetError("read_failed", str(e)) from e

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.warning("invalid JSON for soccer request preset %s: %s", preset_id, e)
        raise SoccerRequestPresetError("invalid_json", str(e)) from e

    try:
        payload = _validate_payload_object(data)
    except SoccerRequestPresetError:
        raise
    except Exception as e:
        logger.exception("unexpected preset validation error %s: %s", preset_id, e)
        raise SoccerRequestPresetError("read_failed", str(e)) from e

    if payload.query.strip() == "":
        raise SoccerRequestPresetError("empty_query", "query is empty")

    if preset_id == PRESET_MATCH_RESULTS_BY_YEAR:
        payload = apply_previous_calendar_year_variables(payload, today=today)

    return payload, format_payload_for_request_box(payload)


def ensure_placeholder_preset_files(requests_dir: Path) -> None:
    """
    Create the non-executable placeholder file if missing.
    Does not overwrite an existing file.
    """
    requests_dir.mkdir(parents=True, exist_ok=True)
    path = resolve_preset_path(requests_dir, "matches_and_odds")
    if path.is_file():
        return
    path.write_text(
        json.dumps({"query": "", "variables": {}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

