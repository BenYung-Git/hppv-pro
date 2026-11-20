"""Shared request-preset load / validate / path resolve.

Sport modules provide only the preset registry and optional post-load hooks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class SharedPresetError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True, slots=True)
class SharedRequestPayload:
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


def resolve_preset_path(
    requests_dir: Path,
    preset_id: str,
    *,
    presets: dict[str, str],
) -> Path:
    filename = presets.get(preset_id)
    if filename is None:
        raise SharedPresetError("unsupported_type", f"unknown preset_id={preset_id!r}")
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise SharedPresetError(
            "unsupported_type", f"invalid registered filename={filename!r}"
        )
    base = requests_dir.resolve()
    path = (base / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError as e:
        raise SharedPresetError("unsupported_type", "path escapes requests_dir") from e
    return path


def validate_payload_object(data: Any) -> SharedRequestPayload:
    if not isinstance(data, dict):
        raise SharedPresetError("invalid_fields", "payload root must be object")
    if "query" not in data:
        raise SharedPresetError("invalid_fields", "query is required")
    query = data["query"]
    if not isinstance(query, str):
        raise SharedPresetError("invalid_fields", "query must be string")

    if "variables" not in data:
        variables = None
    else:
        raw_vars = data["variables"]
        if raw_vars is None:
            variables = None
        elif isinstance(raw_vars, dict):
            variables = raw_vars
        else:
            raise SharedPresetError("invalid_fields", "variables must be object or null")

    if "operationName" not in data:
        operation_name = None
    else:
        raw_op = data["operationName"]
        if raw_op is None:
            operation_name = None
        elif isinstance(raw_op, str) and raw_op.strip() != "":
            operation_name = raw_op
        else:
            raise SharedPresetError(
                "invalid_fields", "operationName must be non-empty string or null"
            )
    return SharedRequestPayload(
        query=query, variables=variables, operation_name=operation_name
    )


def load_request_preset(
    requests_dir: Path,
    preset_id: str,
    *,
    presets: dict[str, str],
    post_load: Callable[[str, SharedRequestPayload], SharedRequestPayload] | None = None,
) -> tuple[SharedRequestPayload, str]:
    """
    Shared preset load:
      resolve path → read JSON → validate → optional sport post_load → display JSON.
    """
    path = resolve_preset_path(requests_dir, preset_id, presets=presets)
    if not path.is_file():
        raise SharedPresetError("file_missing", f"preset file missing: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise SharedPresetError("invalid_json", f"invalid JSON in {path.name}") from e
    except OSError as e:
        raise SharedPresetError("read_failed", f"cannot read {path}") from e
    payload = validate_payload_object(data)
    if post_load is not None:
        payload = post_load(preset_id, payload)
    display = json.dumps(payload.as_dict(), ensure_ascii=False, indent=2)
    return payload, display
