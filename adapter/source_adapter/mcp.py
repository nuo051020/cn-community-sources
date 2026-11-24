"""Minimal MCP (Model Context Protocol) HTTP client.

Just One API is exposed as an MCP server over streamable HTTP. The adapter talks
to it directly with the standard library so the same discovery → schema → call
workflow the Codex session uses is reproducible, auditable and testable here.

The bearer token is read from the environment by name only. It is never stored
on the client, never written to a report and never echoed in an error message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .http import HttpClient, HttpResult
from .normalize import now_iso, redact_secrets

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_MAX_BYTES = 8_000_000
DEFAULT_USER_AGENT = "source-adapter/0.1 (anonymous read-only research)"
CLIENT_INFO = {"name": "source-adapter", "version": "0.1.0"}

# Tool-level calls whose answer is live account state must never be cached.
LIVE_STATE_TOOLS = frozenset({"get_account_balance", "get_usage_summary"})


class McpError(RuntimeError):
    """Raised for protocol-level problems that are not provider failures."""


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """Outcome of one MCP tool call."""

    ok: bool
    tool: str
    data: Any = None
    text: str = ""
    error: Optional[str] = None
    is_error: bool = False
    from_cache: bool = False
    raw: bytes = b""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "payload_type": type(self.data).__name__,
            "is_error": self.is_error,
            "from_cache": self.from_cache,
            "error": self.error,
            "bytes": len(self.raw or b""),
        }


@dataclass(frozen=True, slots=True)
class _PostOutcome:
    messages: tuple[dict[str, Any], ...] = ()
    from_cache: bool = False
    ok: bool = True
    error: str = ""


@dataclass
class McpStats:
    requests: int = 0
    tool_calls: int = 0
    errors: int = 0
    from_cache: int = 0
    tools: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "tool_calls": self.tool_calls,
            "errors": self.errors,
            "from_cache": self.from_cache,
            "tools": dict(self.tools),
        }


def parse_sse_messages(body: str) -> list[dict[str, Any]]:
    """Extract JSON-RPC messages from an SSE body (or a plain JSON body)."""
    text = str(body or "").strip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else [parsed]
    messages: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        payload = "".join(
            line[5:].strip() for line in block.splitlines() if line.startswith("data:")
        )
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        messages.append(parsed)
    return messages


class McpHttpClient:
    """Streamable-HTTP MCP client with discovery, budget and cache guards."""

    def __init__(
        self,
        url: str,
        *,
        token: Optional[str] = None,
        token_env: str = "JUSTONEAPI_TOKEN",
        http: Optional[HttpClient] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: float = 60.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], str] = now_iso,
        max_calls: Optional[int] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        cache: Any = None,
        retries: int = 0,
    ) -> None:
        self.url = url
        self.token_env = token_env
        self.env = dict(env) if env is not None else dict(os.environ)
        self._token = token if token is not None else (self.env.get(token_env) or "").strip()
        # ``retries=0`` is deliberate: a charged endpoint call must never be
        # replayed by the transport layer. Cached successes give free replays.
        self.http = http or HttpClient(
            provider="justone-mcp",
            timeout=timeout,
            max_bytes=max_bytes,
            spacing=0.0,
            retries=retries,
            cache=cache,
            user_agent=user_agent,
        )
        self.clock = clock
        self.max_calls = max_calls
        self.stats = McpStats()
        self._initialized = False
        self._server_info: dict[str, Any] = {}
        self._tools: list[dict[str, Any]] = []

    # -- capability -----------------------------------------------------

    @property
    def token_present(self) -> bool:
        return bool(self._token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "token_env": self.token_env,
            "token_present": self.token_present,
            "initialized": self._initialized,
            "max_calls": self.max_calls,
            "stats": self.stats.to_dict(),
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return f"McpHttpClient(url={self.url!r}, token_present={self.token_present})"

    # -- protocol -------------------------------------------------------

    def initialize(self, *, protocol_version: str = DEFAULT_PROTOCOL_VERSION) -> dict[str, Any]:
        if self._initialized:
            return self._server_info
        message, _outcome = self._rpc(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
            use_cache=False,
        )
        result = message.get("result") if isinstance(message, dict) else None
        self._server_info = result if isinstance(result, dict) else {}
        self.notify("notifications/initialized")
        self._initialized = True
        return self._server_info

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": dict(params or {})}, use_cache=False)

    def list_tools(self) -> list[dict[str, Any]]:
        if self._tools:
            return self._tools
        self.initialize()
        message, _outcome = self._rpc("tools/list", {}, use_cache=True)
        result = message.get("result") if isinstance(message, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        self._tools = [tool for tool in (tools or []) if isinstance(tool, Mapping)]
        return self._tools

    def call_tool(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        use_cache: Optional[bool] = None,
    ) -> McpCallResult:
        if not self.token_present:
            return McpCallResult(
                ok=False,
                tool=name,
                error=f"{self.token_env} 未设置：无法调用 {self.url}",
            )
        if self.max_calls is not None and self.stats.tool_calls >= self.max_calls:
            return McpCallResult(
                ok=False,
                tool=name,
                error=f"已达本次运行的调用预算 max_calls={self.max_calls}，拒绝继续调用 {name}",
            )
        if use_cache is None:
            use_cache = name not in LIVE_STATE_TOOLS
        self.initialize()
        self.stats.tool_calls += 1
        self.stats.tools[name] = self.stats.tools.get(name, 0) + 1
        message, outcome = self._rpc(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
            use_cache=use_cache,
        )
        from_cache = outcome.from_cache
        if message is None:
            self.stats.errors += 1
            return McpCallResult(
                ok=False,
                tool=name,
                error=outcome.error or "MCP 无响应或响应无法解析",
                from_cache=from_cache,
            )
        error = message.get("error") if isinstance(message, dict) else None
        if error:
            self.stats.errors += 1
            return McpCallResult(
                ok=False,
                tool=name,
                error=redact_secrets(f"MCP error {error.get('code')}: {error.get('message')}"),
            )
        result = message.get("result") or {}
        if not isinstance(result, Mapping):
            self.stats.errors += 1
            return McpCallResult(ok=False, tool=name, error="MCP result 结构异常")
        texts: list[str] = []
        for item in result.get("content") or ():
            if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
        text = "\n".join(texts)
        is_error = bool(result.get("isError"))
        data: Any = None
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                data = None
        if data is None and not text:
            data = result
        if is_error:
            self.stats.errors += 1
            return McpCallResult(
                ok=False,
                tool=name,
                text=text,
                data=data,
                is_error=True,
                error=redact_secrets(f"{name} 返回错误：{text[:300]}"),
            )
        return McpCallResult(ok=True, tool=name, data=data, text=text, from_cache=from_cache)

    # -- internals ------------------------------------------------------

    def _rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        use_cache: bool,
        request_id: int = 1,
    ) -> tuple[Optional[dict[str, Any]], "_PostOutcome"]:
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        outcome = self._post(body, use_cache=use_cache)
        for message in outcome.messages:
            if isinstance(message, dict) and message.get("id") == request_id:
                return message, outcome
        if outcome.messages:
            return outcome.messages[0], outcome
        return None, outcome

    def _post(self, body: Mapping[str, Any], *, use_cache: bool) -> "_PostOutcome":
        headers = (
            ("Content-Type", "application/json"),
            ("Accept", "application/json, text/event-stream"),
            ("Authorization", f"Bearer {self._token}"),
            ("MCP-Protocol-Version", DEFAULT_PROTOCOL_VERSION),
        )
        self.stats.requests += 1
        result: HttpResult = self.http.post_text(
            self.url,
            body=json.dumps(body, ensure_ascii=False),
            headers=headers,
            max_bytes=self.http.max_bytes,
            use_cache=use_cache,
        )
        if not result.ok:
            self.stats.errors += 1
            return _PostOutcome(ok=False, error=redact_secrets(result.error or "MCP 请求失败"))
        if result.from_cache:
            self.stats.from_cache += 1
        return _PostOutcome(
            messages=tuple(parse_sse_messages(result.data or "")),
            from_cache=result.from_cache,
        )

    def close(self) -> None:  # pragma: no cover - symmetry helper
        return None


__all__ = [
    "CLIENT_INFO",
    "DEFAULT_PROTOCOL_VERSION",
    "LIVE_STATE_TOOLS",
    "McpCallResult",
    "McpError",
    "McpHttpClient",
    "McpStats",
    "parse_sse_messages",
]
