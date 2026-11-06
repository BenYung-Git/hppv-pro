"""Footylogic HTTP client (read-only GET, sequential, rate-limited)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

FOOTYLOGIC_API_BASE = "https://api.footylogic.com"
FOOTYLOGIC_LANGUAGE_ID = 18
FOOTYLOGIC_CHANNEL_ID = 1
FOOTYLOGIC_TZ_OFFSET_HKT_MIN = 480
DEFAULT_MIN_INTERVAL_SEC = 1.05
DEFAULT_TIMEOUT_SEC = 60
DEFAULT_MAX_RETRIES = 4

FOOTYLOGIC_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://footylogic.com",
    "Referer": "https://footylogic.com/",
}


class FootylogicClientError(RuntimeError):
    def __init__(self, code: str, detail: str = "", *, http_status: int | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class FootylogicHttpResult:
    http_status: int
    elapsed_ms: int
    body: dict[str, Any] | None
    raw_text: str
    business_ok: bool
    message: str | None


class FootylogicClient:
    """Sequential GET client with min-interval, retries, Retry-After."""

    def __init__(
        self,
        *,
        min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: requests.Session | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        get_func: Callable[..., requests.Response] | None = None,
    ) -> None:
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.timeout_sec = float(timeout_sec)
        self.max_retries = max(0, int(max_retries))
        self._session = session
        self._sleep = sleep_func
        self._get = get_func
        self._last_monotonic = 0.0

    def _throttle(self) -> None:
        if self.min_interval_sec <= 0:
            return
        now = time.monotonic()
        wait = self.min_interval_sec - (now - self._last_monotonic)
        if wait > 0:
            self._sleep(wait)

    def get_json(self, path: str, params: dict[str, Any]) -> FootylogicHttpResult:
        url = f"{FOOTYLOGIC_API_BASE.rstrip('/')}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            started = time.perf_counter()
            try:
                if self._get is not None:
                    resp = self._get(
                        url,
                        params=params,
                        headers=FOOTYLOGIC_HEADERS,
                        timeout=self.timeout_sec,
                    )
                else:
                    sess = self._session or requests
                    resp = sess.get(
                        url,
                        params=params,
                        headers=FOOTYLOGIC_HEADERS,
                        timeout=self.timeout_sec,
                    )
            except requests.Timeout as e:
                last_exc = e
                self._last_monotonic = time.monotonic()
                if attempt >= self.max_retries:
                    raise FootylogicClientError("timeout", str(e)) from e
                self._sleep(1.5 * (attempt + 1))
                continue
            except requests.RequestException as e:
                last_exc = e
                self._last_monotonic = time.monotonic()
                if attempt >= self.max_retries:
                    raise FootylogicClientError("connection_failed", str(e)) from e
                self._sleep(1.5 * (attempt + 1))
                continue

            self._last_monotonic = time.monotonic()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            raw = resp.text or ""

            if resp.status_code == 429:
                if attempt >= self.max_retries:
                    raise FootylogicClientError(
                        "rate_limited",
                        raw[:300],
                        http_status=429,
                    )
                ra = resp.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra is not None else 2.0 ** attempt
                except ValueError:
                    wait = 2.0 ** attempt
                self._sleep(max(0.5, wait))
                continue

            if resp.status_code >= 500:
                if attempt >= self.max_retries:
                    raise FootylogicClientError(
                        "http_5xx",
                        raw[:300],
                        http_status=resp.status_code,
                    )
                self._sleep(1.5 * (attempt + 1))
                continue

            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError as e:
                raise FootylogicClientError(
                    "invalid_json",
                    str(e),
                    http_status=resp.status_code,
                ) from e

            if not isinstance(body, dict):
                raise FootylogicClientError(
                    "invalid_json",
                    "response root is not object",
                    http_status=resp.status_code,
                )

            status_code = body.get("statusCode")
            business_ok = status_code in (None, 200, "200") and resp.status_code == 200
            return FootylogicHttpResult(
                http_status=resp.status_code,
                elapsed_ms=elapsed_ms,
                body=body,
                raw_text=raw,
                business_ok=business_ok,
                message=str(body.get("message")) if body.get("message") is not None else None,
            )

        raise FootylogicClientError("retries_exhausted", str(last_exc or ""))


def default_fixtures_results_params(
    *,
    competition_id: Any,
    season_id: Any,
    month: str,
) -> dict[str, Any]:
    return {
        "languageId": FOOTYLOGIC_LANGUAGE_ID,
        "channelId": FOOTYLOGIC_CHANNEL_ID,
        "competitionId": competition_id,
        "seasonId": season_id,
        "date": month,
        "tzOffSet": FOOTYLOGIC_TZ_OFFSET_HKT_MIN,
    }
