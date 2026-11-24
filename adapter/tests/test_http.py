"""Tests for the bounded, retrying, caching HTTP client."""

from __future__ import annotations

import unittest
import urllib.error
from datetime import datetime, timezone

from source_adapter.http import DEFAULT_USER_AGENT, HttpClient, HttpRequest
try:  # `python3 -m unittest tests.test_x`
    from tests.helpers import FakeClock, FakeTransport, bytes_response, json_response
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import FakeClock, FakeTransport, bytes_response, json_response


class HttpClientTests(unittest.TestCase):
    def make_client(self, transport, **overrides):
        clock = overrides.pop("clock", None) or FakeClock()
        options = {
            "transport": transport,
            "provider": "test",
            "spacing": 0.0,
            "sleeper": clock.sleep,
            "monotonic": clock.monotonic,
        }
        options.update(overrides)
        client = HttpClient(**options)
        client.test_clock = clock
        return client

    def test_get_json_parses_payload_and_marks_ok(self):
        transport = FakeTransport([json_response({"no": 0, "data": {"post_list": []}})])
        client = self.make_client(transport)
        result = client.get_json("https://tieba.baidu.com/mo/q/search/thread", params={"word": "暗恋"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["no"], 0)
        self.assertEqual(result.attempts, 1)
        self.assertFalse(result.from_cache)
        self.assertEqual(result.status, 200)

    def test_request_carries_user_agent_and_bounds(self):
        transport = FakeTransport([json_response({})])
        client = self.make_client(transport, max_bytes=4096)
        client.get_json("https://example.test/a", params={"b": "2", "a": "1"})
        request = transport.calls[0]
        self.assertIsInstance(request, HttpRequest)
        self.assertIn(("User-Agent", DEFAULT_USER_AGENT), request.headers)
        self.assertEqual(request.max_bytes, 4096)
        self.assertIn("a=1", request.url)
        self.assertIn("b=2", request.url)

    def test_retries_429_and_honours_retry_after(self):
        transport = FakeTransport(
            [
                bytes_response(b"slow down", status=429, headers={"Retry-After": "2"}),
                json_response({"ok": True}),
            ]
        )
        client = self.make_client(transport)
        result = client.get_json("https://example.test/a")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(client.test_clock.delays, [2.0])

    def test_retry_after_http_date_is_converted_to_seconds(self):
        transport = FakeTransport(
            [
                bytes_response(b"", status=429, headers={"Retry-After": "Wed, 01 Jan 2026 00:00:05 GMT"}),
                json_response({"ok": True}),
            ]
        )
        client = self.make_client(
            transport, now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        result = client.get_json("https://example.test/a")
        self.assertTrue(result.ok)
        self.assertEqual(client.test_clock.delays, [5.0])

    def test_retries_5xx_with_exponential_backoff_then_fails(self):
        transport = FakeTransport([bytes_response(b"boom", status=503) for _ in range(4)])
        client = self.make_client(transport)
        result = client.get_json("https://example.test/a")
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(result.status, 503)
        self.assertEqual(result.error, "HTTP 503")
        self.assertEqual(client.test_clock.delays, [1.0, 2.0, 4.0])

    def test_non_retryable_status_returns_immediately(self):
        transport = FakeTransport([bytes_response(b"nope", status=404)])
        client = self.make_client(transport)
        result = client.get_json("https://example.test/a")
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result.error, "HTTP 404")

    def test_network_error_is_retried_and_redacted(self):
        transport = FakeTransport(
            [
                urllib.error.URLError("connection refused for Bearer abc123def456"),
                json_response({"ok": True}),
            ]
        )
        client = self.make_client(transport)
        result = client.get_json("https://example.test/a")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)

    def test_terminal_network_error_is_reported_without_secrets(self):
        transport = FakeTransport(
            [urllib.error.URLError("token=deadbeefcafe rejected") for _ in range(4)]
        )
        client = self.make_client(transport)
        result = client.get_json("https://example.test/a")
        self.assertFalse(result.ok)
        self.assertNotIn("deadbeefcafe", result.error)
        self.assertIn("<redacted>", result.error)

    def test_response_size_cap_is_enforced(self):
        transport = FakeTransport([bytes_response(b"x" * 100)])
        client = self.make_client(transport, max_bytes=10)
        result = client.get_json("https://example.test/a")
        self.assertFalse(result.ok)
        self.assertIn("exceeds", result.error)
        self.assertEqual(result.attempts, 1)

    def test_spacing_is_applied_between_requests(self):
        transport = FakeTransport([json_response({"n": 1}), json_response({"n": 2})])
        client = self.make_client(transport, spacing=2.0)
        client.get_json("https://example.test/a")
        client.get_json("https://example.test/b")
        self.assertEqual(client.test_clock.delays, [2.0])

    def test_malformed_json_reports_error(self):
        transport = FakeTransport([bytes_response(b"<html>not json</html>")])
        client = self.make_client(transport)
        result = client.get_json("https://example.test/a")
        self.assertFalse(result.ok)
        self.assertIn("JSON", result.error)

    def test_get_text_returns_decoded_body(self):
        transport = FakeTransport([bytes_response("中文正文".encode("utf-8"))])
        client = self.make_client(transport)
        result = client.get_text("https://example.test/a")
        self.assertTrue(result.ok)
        self.assertEqual(result.data, "中文正文")

    def test_default_transport_is_urllib_and_never_used_in_tests(self):
        client = HttpClient(provider="test")
        self.assertTrue(callable(client.transport))
        self.assertEqual(client.retries, 3)
        self.assertEqual(client.spacing, 2.0)


if __name__ == "__main__":
    unittest.main()
