"""Tests for the MCP HTTP client: protocol, caching, budget and secret safety."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from source_adapter.cache import FileCache
from source_adapter.http import HttpClient
from source_adapter.mcp import McpHttpClient, parse_sse_messages

try:  # `python3 -m unittest tests.test_mcp`
    from tests.helpers import (
        FakeTransport,
        bytes_response,
        json_rpc_response,
        sse_response,
        tool_result,
    )
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import (
        FakeTransport,
        bytes_response,
        json_rpc_response,
        sse_response,
        tool_result,
    )

URL = "https://mcp.example.test/mcp"
TOKEN = "test-token-value-that-must-never-leak"
INIT_RESULT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {"listChanged": True}},
    "serverInfo": {"name": "justoneapi-mcp", "version": "2.0.0"},
}
TOOLS = [
    {"name": "search_endpoints", "description": "发现端点", "inputSchema": {"type": "object"}},
    {"name": "call_endpoint", "description": "调用端点", "inputSchema": {"type": "object"}},
]


def make_client(responses, *, cache=None, token=TOKEN, max_calls=None, env=None):
    transport = FakeTransport(responses)
    http = HttpClient(
        transport=transport,
        provider="justone-mcp",
        spacing=0.0,
        retries=0,
        cache=cache,
    )
    client = McpHttpClient(URL, token=token, http=http, env=env or {}, max_calls=max_calls)
    return client, transport


def init_sequence(*extra):
    return [sse_response([json_rpc_response(INIT_RESULT)]), bytes_response(b"", status=202), *extra]


class ProtocolTests(unittest.TestCase):
    def test_initialize_parses_sse_and_sends_initialized_notification(self):
        client, transport = make_client(init_sequence())
        info = client.initialize()
        self.assertEqual(info["serverInfo"]["name"], "justoneapi-mcp")
        self.assertEqual(transport.call_count, 2)
        init_body = json.loads(transport.calls[0].body.decode("utf-8"))
        self.assertEqual(init_body["method"], "initialize")
        self.assertEqual(init_body["params"]["clientInfo"]["name"], "source-adapter")
        notification = json.loads(transport.calls[1].body.decode("utf-8"))
        self.assertEqual(notification["method"], "notifications/initialized")
        self.assertNotIn("id", notification)
        sent = dict(transport.calls[0].headers)
        self.assertEqual(sent["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(sent["Content-Type"], "application/json")

    def test_initialize_is_sent_only_once(self):
        client, transport = make_client(init_sequence())
        client.initialize()
        client.initialize()
        self.assertEqual(transport.call_count, 2)

    def test_list_tools_returns_metadata(self):
        client, _ = make_client(init_sequence(sse_response([json_rpc_response({"tools": TOOLS})])))
        tools = client.list_tools()
        self.assertEqual([tool["name"] for tool in tools], ["search_endpoints", "call_endpoint"])
        self.assertEqual(client.list_tools(), tools)

    def test_call_tool_parses_json_payload_from_content_text(self):
        payload = {"success": True, "data": {"endpoints": [{"endpoint_id": "bilibili.search_video_v2"}]}}
        client, _ = make_client(init_sequence(sse_response([json_rpc_response(tool_result(payload))])))
        result = client.call_tool("search_endpoints", {"query": "视频搜索", "platform": "bilibili"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["data"]["endpoints"][0]["endpoint_id"], "bilibili.search_video_v2")
        self.assertFalse(result.from_cache)

    def test_plain_json_response_body_is_supported(self):
        payload = tool_result({"success": True})
        client, _ = make_client(init_sequence(bytes_response(json.dumps(json_rpc_response(payload)).encode())))
        result = client.call_tool("list_platforms", {})
        self.assertTrue(result.ok)

    def test_is_error_result_is_reported(self):
        client, _ = make_client(
            init_sequence(sse_response([json_rpc_response(tool_result("boom", as_json=False, is_error=True))]))
        )
        result = client.call_tool("call_endpoint", {"endpoint_id": "x"})
        self.assertFalse(result.ok)
        self.assertTrue(result.is_error)
        self.assertIn("boom", result.error)

    def test_jsonrpc_error_object_is_reported(self):
        client, _ = make_client(
            init_sequence(sse_response([json_rpc_response(None, error={"code": -32602, "message": "bad params"})]))
        )
        result = client.call_tool("call_endpoint", {})
        self.assertFalse(result.ok)
        self.assertIn("bad params", result.error)

    def test_parse_sse_ignores_keepalive_blocks(self):
        body = "event: ping\ndata: not-json\n\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1}\n\n"
        messages = parse_sse_messages(body)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 1)


class SafetyTests(unittest.TestCase):
    def test_missing_token_fails_without_touching_the_network(self):
        client, transport = make_client([], token=None, env={})
        self.assertFalse(client.token_present)
        result = client.call_tool("list_platforms", {})
        self.assertFalse(result.ok)
        self.assertIn("JUSTONEAPI_TOKEN", result.error)
        self.assertEqual(transport.call_count, 0)

    def test_token_never_appears_in_outputs(self):
        client, _ = make_client(init_sequence(sse_response([json_rpc_response(tool_result({"ok": True}))])))
        result = client.call_tool("get_account_balance", {})
        serialized = json.dumps(
            {"client": client.to_dict(), "result": result.to_dict(), "repr": repr(client)},
            ensure_ascii=False,
        )
        self.assertNotIn(TOKEN, serialized)
        self.assertTrue(client.to_dict()["token_present"])

    def test_http_error_is_redacted(self):
        client, _ = make_client(
            init_sequence(bytes_response(f"unauthorized {TOKEN}".encode(), status=401))
        )
        result = client.call_tool("call_endpoint", {"endpoint_id": "x"})
        self.assertFalse(result.ok)
        self.assertIn("401", result.error or "")
        self.assertNotIn(TOKEN, result.error or "")

    def test_transport_failures_are_not_retried(self):
        client, transport = make_client(
            init_sequence(bytes_response(b"", status=503), bytes_response(b"", status=503))
        )
        result = client.call_tool("call_endpoint", {"endpoint_id": "x"})
        self.assertFalse(result.ok)
        # init + notification + exactly one tools/call attempt (no double charge)
        self.assertEqual(transport.call_count, 3)


class BudgetAndCacheTests(unittest.TestCase):
    def test_budget_guard_refuses_extra_calls(self):
        payload = tool_result({"success": True})
        client, transport = make_client(
            init_sequence(
                sse_response([json_rpc_response(payload)]),
                sse_response([json_rpc_response(payload)]),
            ),
            max_calls=1,
        )
        first = client.call_tool("call_endpoint", {"endpoint_id": "a"})
        second = client.call_tool("call_endpoint", {"endpoint_id": "b"})
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertIn("预算", second.error)
        self.assertEqual(transport.call_count, 3)

    def test_identical_paid_calls_replay_from_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            payload = tool_result({"success": True, "data": {"balance": 18.4}})
            client, transport = make_client(
                init_sequence(sse_response([json_rpc_response(payload)])),
                cache=cache,
            )
            first = client.call_tool("search_endpoints", {"query": "q", "platform": "zhihu"})
            second = client.call_tool("search_endpoints", {"query": "q", "platform": "zhihu"})
            self.assertTrue(first.ok)
            self.assertFalse(first.from_cache)
            self.assertTrue(second.ok)
            self.assertTrue(second.from_cache)
            self.assertEqual(transport.call_count, 3)
            self.assertEqual(client.stats.to_dict()["from_cache"], 1)

    def test_live_state_tools_bypass_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            payload = tool_result({"success": True, "data": {"balance": 1.0}})
            client, transport = make_client(
                init_sequence(
                    sse_response([json_rpc_response(payload)]),
                    sse_response([json_rpc_response(payload)]),
                ),
                cache=cache,
            )
            client.call_tool("get_account_balance", {})
            client.call_tool("get_account_balance", {})
            self.assertEqual(transport.call_count, 4)


if __name__ == "__main__":
    unittest.main()
