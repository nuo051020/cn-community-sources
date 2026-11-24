"""Shared, deterministic test doubles for the source adapter test suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from source_adapter.http import HttpRequest, HttpResponse

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_path(name: str) -> Path:
    return FIXTURES / name


def load_fixture(name: str) -> Any:
    with open(fixture_path(name), encoding="utf-8") as handle:
        return json.load(handle)


def read_fixture_bytes(name: str) -> bytes:
    return fixture_path(name).read_bytes()


def json_response(
    payload: Any,
    status: int = 200,
    headers: Optional[Mapping[str, str]] = None,
    url: str = "https://example.test/resource",
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        url=url,
    )


def bytes_response(
    body: bytes,
    status: int = 200,
    headers: Optional[Mapping[str, str]] = None,
    url: str = "https://example.test/resource",
    error: Optional[str] = None,
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        body=body,
        url=url,
        error=error,
    )


def json_rpc_response(result: Any, request_id: int = 1, *, error: Any = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    return message


def tool_result(payload: Any, *, as_json: bool = True, is_error: bool = False) -> dict:
    text = json.dumps(payload, ensure_ascii=False) if as_json else str(payload)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def sse_response(messages: Iterable[dict], status: int = 200, url: str = "https://mcp.example.test/mcp") -> HttpResponse:
    blocks = []
    for message in messages:
        blocks.append("event: message\ndata: " + json.dumps(message, ensure_ascii=False))
    body = ("\n\n".join(blocks) + "\n\n").encode("utf-8") if blocks else b""
    return HttpResponse(status=status, headers={"content-type": "text/event-stream"}, body=body, url=url)


class FakeTransport:
    """Scripted transport that records every request it receives."""

    def __init__(self, responses: Iterable[Any] = ()) -> None:
        self.responses: list[Any] = list(responses)
        self.calls: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.calls.append(request)
        if not self.responses:
            return bytes_response(b"", status=599, error="no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request)
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeClock:
    """Monotonic-ish clock whose sleeps are recorded instead of performed."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.value += seconds

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ScriptedRunner:
    """Stand-in for ``subprocess.run`` that records argv and returns canned output."""

    def __init__(self, results: Sequence[Any] = ()) -> None:
        self.results: list[Any] = list(results)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        self.calls.append((list(argv), dict(kwargs)))
        if not self.results:
            return CompletedProcessLike(argv, returncode=1, stdout="", stderr="no scripted result")
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(argv, kwargs)
        return item


class CompletedProcessLike:
    def __init__(
        self,
        argv: Sequence[str] = (),
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.args = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_factory(sequence: Iterable[Any]) -> Callable[[], Any]:
    """Return a zero-argument factory that yields scripted values in order."""
    items = list(sequence)

    def factory() -> Any:
        if not items:
            raise AssertionError("scripted factory exhausted")
        return items.pop(0)

    return factory
