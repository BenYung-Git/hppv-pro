from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from graphql import GraphQLError, OperationType, parse
from graphql.language.ast import OperationDefinitionNode

from hppv.soccer_requests import SoccerRequestPayload, SoccerRequestPresetError

logger = logging.getLogger(__name__)

# Fixed upstream endpoint (not user-overridable).
SOCCER_GRAPHQL_ENDPOINT = "https://info.cld.hkjc.com/graphql/base/"
SOCCER_GRAPHQL_TIMEOUT_SEC = 30
SOCCER_GRAPHQL_MAX_REQUEST_BYTES = 100_000
SOCCER_GRAPHQL_MAX_RESPONSE_BYTES = 2_000_000
SOCCER_GRAPHQL_MIN_INTERVAL_SEC = 3.0

SOCCER_GRAPHQL_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Origin": "https://bet.hkjc.com",
    "Referer": "https://bet.hkjc.com/ch/football/home",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "zh-HK,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

_ALLOWED_PAYLOAD_KEYS = frozenset({"query", "variables", "operationName"})
_FORBIDDEN_OVERRIDE_KEYS = frozenset(
    {
        "url",
        "endpoint",
        "headers",
        "timeout",
        "host",
        "proxy",
        "cookies",
        "cookie",
        "authorization",
        "auth",
    }
)

_rate_lock = threading.Lock()
_rate_last_monotonic = 0.0


class SoccerGraphQLSendError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SoccerGraphQLSendResult:
    outcome: str  # success | partial_errors | graphql_errors | http_error | ...
    http_status: int | None
    elapsed_ms: int
    body_text: str
    truncated: bool
    match_count: int | None
    has_had_odds: bool | None


def parse_and_validate_send_payload(raw_text: str) -> SoccerRequestPayload:
    """Validate Request-box JSON for outbound send (query-only)."""
    if len(raw_text.encode("utf-8")) > SOCCER_GRAPHQL_MAX_REQUEST_BYTES:
        raise SoccerGraphQLSendError("payload_too_large", "request body too large")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise SoccerGraphQLSendError("invalid_json", str(e)) from e

    if not isinstance(data, dict):
        raise SoccerGraphQLSendError("invalid_fields", "payload root must be object")

    extra = set(data.keys()) - _ALLOWED_PAYLOAD_KEYS
    if extra & _FORBIDDEN_OVERRIDE_KEYS:
        raise SoccerGraphQLSendError("invalid_fields", "connection override fields are not allowed")
    if extra:
        # Reject unknown keys so clients cannot smuggle overrides.
        raise SoccerGraphQLSendError("invalid_fields", f"unexpected fields: {sorted(extra)}")

    if "query" not in data or not isinstance(data.get("query"), str):
        raise SoccerGraphQLSendError("invalid_fields", "query must be a string")
    query = data["query"]
    if query.strip() == "":
        raise SoccerGraphQLSendError("empty_query", "query is empty")

    variables: dict[str, Any] | None
    if "variables" not in data:
        variables = None
    elif data["variables"] is None:
        variables = None
    elif isinstance(data["variables"], dict):
        variables = data["variables"]
    else:
        raise SoccerGraphQLSendError("invalid_fields", "variables must be object or null")

    operation_name: str | None
    if "operationName" not in data:
        operation_name = None
    elif data["operationName"] is None:
        operation_name = None
    elif isinstance(data["operationName"], str) and data["operationName"].strip() != "":
        operation_name = data["operationName"]
    else:
        raise SoccerGraphQLSendError(
            "invalid_fields",
            "operationName must be null or non-empty string",
        )

    _assert_query_operations_only(query)
    return SoccerRequestPayload(query=query, variables=variables, operation_name=operation_name)


def _assert_query_operations_only(query: str) -> None:
    try:
        document = parse(query)
    except GraphQLError as e:
        raise SoccerGraphQLSendError("invalid_graphql", str(e)) from e

    ops = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if not ops:
        raise SoccerGraphQLSendError("invalid_graphql", "no operation definition")
    for op in ops:
        if op.operation != OperationType.QUERY:
            raise SoccerGraphQLSendError(
                "operation_not_allowed",
                f"only query is allowed, got {op.operation.value}",
            )


def _acquire_rate_slot(*, wait: bool = False) -> None:
    """
    Enforce min interval between GraphQL sends (process-local).

    wait=False (manual API): raise rate_limited if too soon.
    wait=True (schedulers): sleep until the slot is free, then take it.
    """
    global _rate_last_monotonic
    while True:
        with _rate_lock:
            now = time.monotonic()
            elapsed = now - _rate_last_monotonic
            if _rate_last_monotonic <= 0 or elapsed >= SOCCER_GRAPHQL_MIN_INTERVAL_SEC:
                _rate_last_monotonic = now
                return
            delay = SOCCER_GRAPHQL_MIN_INTERVAL_SEC - elapsed
            if not wait:
                raise SoccerGraphQLSendError("rate_limited", "too frequent")
        time.sleep(max(delay, 0.01))


def reset_rate_limit_for_tests() -> None:
    global _rate_last_monotonic
    with _rate_lock:
        _rate_last_monotonic = 0.0


def _summarize_matches(data_obj: Any) -> tuple[int | None, bool | None]:
    if not isinstance(data_obj, dict):
        return None, None
    matches = data_obj.get("matches")
    if not isinstance(matches, list):
        return None, None
    has_had = False
    for m in matches:
        if not isinstance(m, dict):
            continue
        pools = m.get("foPools") or []
        if not isinstance(pools, list):
            continue
        for p in pools:
            if isinstance(p, dict) and p.get("oddsType") == "HAD":
                has_had = True
                break
        if has_had:
            break
    return len(matches), has_had


def format_status_text(
    *,
    result_label: str,
    http_label: str,
    elapsed_label: str,
    body_label: str,
    outcome_text: str,
    http_status: int | None,
    elapsed_ms: int,
    body_text: str,
    truncated_note: str | None = None,
) -> str:
    http_disp = "—" if http_status is None else str(http_status)
    parts = [
        f"{result_label}：{outcome_text}",
        f"{http_label}：{http_disp}",
        f"{elapsed_label}：{elapsed_ms} ms",
        "",
        f"{body_label}：",
        body_text,
    ]
    if truncated_note:
        parts.append("")
        parts.append(truncated_note)
    return "\n".join(parts)


def send_soccer_graphql(
    payload: SoccerRequestPayload,
    *,
    session: requests.Session | None = None,
    post_func: Callable[..., requests.Response] | None = None,
    wait_for_rate_limit: bool = False,
) -> SoccerGraphQLSendResult:
    """
    Send a validated payload to the fixed HKJC GraphQL endpoint.
    post_func/session are injectable for tests.

    wait_for_rate_limit: schedulers should pass True so back-to-back tasks
    wait for SOCCER_GRAPHQL_MIN_INTERVAL_SEC instead of failing.
    """
    _acquire_rate_slot(wait=wait_for_rate_limit)

    body_obj: dict[str, Any] = {"query": payload.query}
    if payload.variables is not None:
        body_obj["variables"] = payload.variables
    if payload.operation_name is not None:
        body_obj["operationName"] = payload.operation_name
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))

    started = time.perf_counter()
    try:
        if post_func is not None:
            resp = post_func(
                SOCCER_GRAPHQL_ENDPOINT,
                headers=SOCCER_GRAPHQL_HEADERS,
                data=body,
                timeout=SOCCER_GRAPHQL_TIMEOUT_SEC,
            )
        else:
            sess = session or requests
            resp = sess.post(
                SOCCER_GRAPHQL_ENDPOINT,
                headers=SOCCER_GRAPHQL_HEADERS,
                data=body,
                timeout=SOCCER_GRAPHQL_TIMEOUT_SEC,
                allow_redirects=False,
            )
    except requests.Timeout as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise SoccerGraphQLSendError("timeout", str(e)) from e
    except requests.RequestException as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.warning("soccer graphql connection error: %s", e)
        raise SoccerGraphQLSendError("connection_failed", str(e)) from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    raw = resp.content or b""
    truncated = False
    if len(raw) > SOCCER_GRAPHQL_MAX_RESPONSE_BYTES:
        raw = raw[:SOCCER_GRAPHQL_MAX_RESPONSE_BYTES]
        truncated = True
    try:
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")

    if resp.status_code != 200:
        return SoccerGraphQLSendResult(
            outcome="http_error",
            http_status=resp.status_code,
            elapsed_ms=elapsed_ms,
            body_text=text,
            truncated=truncated,
            match_count=None,
            has_had_odds=None,
        )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return SoccerGraphQLSendResult(
            outcome="non_json",
            http_status=resp.status_code,
            elapsed_ms=elapsed_ms,
            body_text=text,
            truncated=truncated,
            match_count=None,
            has_had_odds=None,
        )

    pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    data_obj = parsed.get("data") if isinstance(parsed, dict) else None
    match_count, has_had = _summarize_matches(data_obj)

    if errors and data_obj is not None:
        outcome = "partial_errors"
    elif errors:
        outcome = "graphql_errors"
    else:
        outcome = "success"

    return SoccerGraphQLSendResult(
        outcome=outcome,
        http_status=resp.status_code,
        elapsed_ms=elapsed_ms,
        body_text=pretty,
        truncated=truncated,
        match_count=match_count,
        has_had_odds=has_had,
    )


def preset_error_to_send_compatible(err: SoccerRequestPresetError) -> SoccerGraphQLSendError:
    return SoccerGraphQLSendError(err.code, err.detail)

