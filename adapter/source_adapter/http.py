"""Bounded, retrying, optionally cached HTTP client built on the standard library.

The client never raises for network conditions: it returns an :class:`HttpResult`
carrying an explicit error string with credential-looking values redacted.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from .normalize import now_iso, normalize_query_params, normalize_url, redact_secrets

DEFAULT_USER_AGENT = "source-adapter/0.1 (anonymous read-only research tool)"
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_RETRIES = 3
DEFAULT_SPACING = 2.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
RETRYABLE_STATUSES = frozenset({408, 429})


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES
    body: bytes = b""

    def header(self, name: str) -> Optional[str]:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""
    error: Optional[str] = None

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())


@dataclass(frozen=True, slots=True)
class HttpResult:
    ok: bool
    url: str
    status: Optional[int] = None
    data: Any = None
    body: bytes = b""
    attempts: int = 0
    error: Optional[str] = None
    from_cache: bool = False
    media_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "url": self.url,
            "status": self.status,
            "attempts": self.attempts,
            "error": self.error,
            "from_cache": self.from_cache,
            "media_type": self.media_type,
            "bytes": len(self.body or b""),
        }


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """Default transport: one bounded ``urllib`` request, errors as values."""
    headers = {key: value for key, value in request.headers}
    prepared = urllib.request.Request(
        request.url, data=request.body or None, method=request.method, headers=headers
    )
    try:
        with urllib.request.urlopen(prepared, timeout=request.timeout) as response:
            body = response.read(request.max_bytes + 1)
            return HttpResponse(
                status=int(getattr(response, "status", 0) or 0),
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
                url=response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(request.max_bytes + 1)
        except Exception:  # pragma: no cover - defensive only
            body = b""
        return HttpResponse(
            status=int(exc.code or 0),
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            body=body,
            url=request.url,
        )
    except Exception as exc:
        return HttpResponse(status=0, headers={}, body=b"", url=request.url, error=f"{type(exc).__name__}: {exc}")


class HttpClient:
    """Small stdlib HTTP client with retries, spacing, bounds and cache hooks."""

    def __init__(
        self,
        *,
        transport: Optional[Callable[[HttpRequest], HttpResponse]] = None,
        provider: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        retries: int = DEFAULT_RETRIES,
        spacing: float = DEFAULT_SPACING,
        backoff_base: float = BACKOFF_BASE,
        backoff_cap: float = BACKOFF_CAP,
        user_agent: str = DEFAULT_USER_AGENT,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_fn: Optional[Callable[[], datetime]] = None,
        cache: Any = None,
        offline: bool = False,
    ) -> None:
        if not 1.5 <= spacing <= 3.0 and spacing != 0.0:
            raise ValueError("spacing must be between 1.5 and 3.0 seconds (0 disables it)")
        if retries < 0:
            raise ValueError("retries must not be negative")
        self.transport = transport or urllib_transport
        self.provider = provider or "unknown"
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.spacing = spacing
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.user_agent = user_agent
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.cache = cache
        self.offline = offline
        self._last_request_start: Optional[float] = None

    # -- public API -----------------------------------------------------

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Sequence[tuple[str, str]] = (),
        max_bytes: Optional[int] = None,
    ) -> HttpResult:
        result = self._fetch(url, params=params, headers=headers, max_bytes=max_bytes)
        if not result.ok:
            return result
        try:
            data = json.loads(result.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            return HttpResult(
                ok=False,
                url=result.url,
                status=result.status,
                body=result.body,
                attempts=result.attempts,
                error=redact_secrets(f"malformed JSON body: {exc}"),
                from_cache=result.from_cache,
            )
        return HttpResult(
            ok=True,
            url=result.url,
            status=result.status,
            data=data,
            body=result.body,
            attempts=result.attempts,
            from_cache=result.from_cache,
            media_type="json",
        )

    def get_text(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Sequence[tuple[str, str]] = (),
        max_bytes: Optional[int] = None,
    ) -> HttpResult:
        result = self._fetch(url, params=params, headers=headers, max_bytes=max_bytes)
        if not result.ok:
            return result
        return HttpResult(
            ok=True,
            url=result.url,
            status=result.status,
            data=result.body.decode("utf-8", errors="replace"),
            body=result.body,
            attempts=result.attempts,
            from_cache=result.from_cache,
            media_type="text",
        )

    def post_text(
        self,
        url: str,
        *,
        body: bytes | str,
        headers: Sequence[tuple[str, str]] = (),
        max_bytes: Optional[int] = None,
        use_cache: bool = True,
    ) -> HttpResult:
        """POST a body and return the decoded response text.

        The cache key includes a digest of the body, so repeated identical
        requests (for example a paid MCP tool call) replay from disk without
        another upstream call.
        """
        payload = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        result = self._fetch(
            url,
            params=None,
            headers=headers,
            max_bytes=max_bytes,
            body=payload,
            method="POST",
            use_cache=use_cache,
        )
        if not result.ok:
            return result
        return HttpResult(
            ok=True,
            url=result.url,
            status=result.status,
            data=result.body.decode("utf-8", errors="replace"),
            body=result.body,
            attempts=result.attempts,
            from_cache=result.from_cache,
            media_type="text",
        )

    # -- internals ------------------------------------------------------

    def _fetch(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]],
        headers: Sequence[tuple[str, str]],
        max_bytes: Optional[int],
        body: bytes = b"",
        method: str = "GET",
        use_cache: bool = True,
    ) -> HttpResult:
        limit = max_bytes or self.max_bytes
        full_url = _with_params(url, params)
        cache_key = self._cache_key(full_url, limit, body) if use_cache else None
        if self.cache is not None and cache_key is not None:
            entry = self.cache.get(cache_key)
            if entry is not None:
                return HttpResult(
                    ok=True,
                    url=full_url,
                    status=200,
                    body=entry.body,
                    attempts=0,
                    from_cache=True,
                )
            if self.offline:
                return HttpResult(
                    ok=False,
                    url=full_url,
                    attempts=0,
                    error="offline cache miss: no stored response for this request",
                )
        elif self.offline:
            return HttpResult(
                ok=False,
                url=full_url,
                attempts=0,
                error=(
                    "offline mode cannot issue new requests"
                    if not use_cache
                    else "offline mode requires a cache"
                ),
            )

        request = HttpRequest(
            url=full_url,
            method=method,
            headers=(("User-Agent", self.user_agent), ("Accept", "*/*"), *tuple(headers)),
            timeout=self.timeout,
            max_bytes=limit,
            body=body,
        )
        response, attempts, error = self._request_with_retries(request)
        if response is None:
            return HttpResult(ok=False, url=full_url, attempts=attempts, error=redact_secrets(error or "request failed"))
        if response.error:
            return HttpResult(
                ok=False,
                url=full_url,
                status=response.status or None,
                attempts=attempts,
                error=redact_secrets(response.error),
            )
        if not 200 <= response.status < 300:
            return HttpResult(
                ok=False,
                url=full_url,
                status=response.status,
                attempts=attempts,
                error=f"HTTP {response.status}",
            )
        if len(response.body) > limit:
            return HttpResult(
                ok=False,
                url=full_url,
                status=response.status,
                attempts=attempts,
                error=f"response body exceeds {limit} bytes",
            )
        if self.cache is not None and cache_key is not None:
            self.cache.put(
                cache_key,
                response.body,
                {
                    "provider": self.provider,
                    "url": full_url,
                    "status": response.status,
                    "retrieved_at": now_iso(),
                },
            )
        return HttpResult(ok=True, url=full_url, status=response.status, body=response.body, attempts=attempts)

    def _cache_key(self, full_url: str, limit: int, body: bytes = b"") -> Optional[str]:
        if self.cache is None:
            return None
        params: dict[str, Any] = {"url": normalize_url(full_url), "max_bytes": limit}
        if body:
            params["body_sha256"] = hashlib.sha256(body).hexdigest()[:16]
        return self.cache.key(self.provider, "http", params)

    def _request_with_retries(
        self, request: HttpRequest
    ) -> tuple[Optional[HttpResponse], int, Optional[str]]:
        attempts = 0
        last_error: Optional[str] = None
        for attempt_index in range(self.retries + 1):
            self._respect_spacing()
            attempts += 1
            try:
                response = self.transport(request)
            except Exception as exc:  # transport contracts allow raising
                response = HttpResponse(
                    status=0,
                    headers={},
                    body=b"",
                    url=request.url,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if response.error:
                last_error = redact_secrets(response.error)
                response = None
            elif self._is_retryable(response.status):
                last_error = f"HTTP {response.status}"
            else:
                return response, attempts, None

            if attempt_index >= self.retries:
                return response, attempts, last_error
            if response is not None:
                delay = self._retry_delay(response, attempt_index)
            else:
                delay = min(self.backoff_base * (2**attempt_index), self.backoff_cap)
            self._sleeper(delay)
        return None, attempts, last_error  # pragma: no cover - loop always returns

    def _is_retryable(self, status: int) -> bool:
        return status in RETRYABLE_STATUSES or status >= 500

    def _retry_delay(self, response: HttpResponse, attempt_index: int) -> float:
        header = response.header("retry-after")
        if header:
            seconds = _parse_retry_after(header, self._now_fn())
            if seconds is not None:
                return max(0.0, seconds)
        return min(self.backoff_base * (2**attempt_index), self.backoff_cap)

    def _respect_spacing(self) -> None:
        if self.spacing <= 0:
            self._last_request_start = self._monotonic()
            return
        now = self._monotonic()
        if self._last_request_start is not None:
            elapsed = now - self._last_request_start
            if elapsed < self.spacing:
                self._sleeper(self.spacing - elapsed)
        self._last_request_start = self._monotonic()


def _parse_retry_after(value: str, now: datetime) -> Optional[float]:
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (target - now).total_seconds()


def _with_params(url: str, params: Optional[Mapping[str, Any]]) -> str:
    if not params:
        return url
    pairs = normalize_query_params(params)
    encoded = urllib.parse.urlencode(pairs)
    separator = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{separator}{encoded}"
