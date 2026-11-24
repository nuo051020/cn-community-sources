"""Tests for normalization helpers: urls, time, text, secret redaction."""

from __future__ import annotations

import unittest

from source_adapter.normalize import (
    make_excerpt,
    normalize_object_id,
    normalize_query_params,
    normalize_url,
    normalize_whitespace,
    now_iso,
    parse_time,
    redact_secrets,
    slugify,
    strip_html,
    truncate_text,
)


class UrlTests(unittest.TestCase):
    def test_drops_tracking_fragment_and_sorts_query(self):
        self.assertEqual(
            normalize_url("https://Example.com/a/?utm_source=x&b=2&a=1#frag"),
            "https://example.com/a?a=1&b=2",
        )

    def test_keeps_root_path_empty(self):
        self.assertEqual(normalize_url("https://tieba.baidu.com/"), "https://tieba.baidu.com")

    def test_drops_default_ports_and_userinfo(self):
        self.assertEqual(
            normalize_url("https://user:pw@Example.com:443/p?b=1"),
            "https://example.com/p?b=1",
        )

    def test_handles_blank_input(self):
        self.assertEqual(normalize_url(""), "")


class ObjectIdTests(unittest.TestCase):
    def test_bilibili_prefix_normalization(self):
        self.assertEqual(normalize_object_id("bilibili", "bv1Xx411c7mD"), "BV1Xx411c7mD")
        self.assertEqual(normalize_object_id("bilibili", "AV12345"), "av12345")

    def test_strips_platform_prefix(self):
        self.assertEqual(normalize_object_id("tieba", "tieba:123"), "123")
        self.assertEqual(normalize_object_id("zhihu", "answer:456"), "456")

    def test_trims_whitespace(self):
        self.assertEqual(normalize_object_id("tieba", " 789 "), "789")


class TimeTests(unittest.TestCase):
    def test_reads_unix_seconds(self):
        self.assertEqual(parse_time(1790097516), "2026-09-22T17:18:36Z")
        self.assertEqual(parse_time("1790097516"), "2026-09-22T17:18:36Z")

    def test_reads_unix_milliseconds(self):
        self.assertEqual(parse_time(1790097516000), "2026-09-22T17:18:36Z")

    def test_reads_iso_strings(self):
        self.assertEqual(parse_time("2026-09-22T17:18:36Z"), "2026-09-22T17:18:36Z")
        self.assertEqual(parse_time("2026-09-22T17:18:36+08:00"), "2026-09-22T09:18:36Z")
        self.assertEqual(parse_time("2026-09-22 17:18:36"), "2026-09-22T17:18:36Z")
        self.assertEqual(parse_time("2026-09-22"), "2026-09-22T00:00:00Z")

    def test_reads_english_month_dates_from_search_engines(self):
        self.assertEqual(parse_time("Nov 9, 2023"), "2023-11-09T00:00:00Z")
        self.assertEqual(parse_time("January 7, 2026"), "2026-01-07T00:00:00Z")

    def test_returns_none_for_relative_or_blank_values(self):
        self.assertIsNone(parse_time("11 years ago"))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(True))

    def test_now_iso_is_zulu_seconds(self):
        value = now_iso()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TextTests(unittest.TestCase):
    def test_normalize_whitespace_collapses_runs(self):
        self.assertEqual(normalize_whitespace("  甲乙\n\n 丙\t丁 "), "甲乙 丙 丁")

    def test_make_excerpt_truncates_with_ellipsis(self):
        self.assertEqual(make_excerpt("短句", limit=10), "短句")
        self.assertEqual(make_excerpt("一二三四五六", limit=4), "一二三四…")

    def test_truncate_text_marks_truncation(self):
        self.assertEqual(truncate_text("abcdef", 3), "abc…")
        self.assertEqual(truncate_text("ab", 3), "ab")

    def test_strip_html_keeps_line_breaks(self):
        html = "<p>第一段</p><p>第二段<br>换行<b>&amp;</b></p>"
        self.assertEqual(strip_html(html), "第一段\n第二段\n换行&")

    def test_slugify_keeps_cjk_and_drops_punctuation(self):
        self.assertEqual(slugify("高中 暗恋/story?"), "高中-暗恋-story")
        self.assertEqual(slugify("   "), "run")
        self.assertEqual(slugify("a" * 80, limit=5), "a" * 5)


class QueryParamTests(unittest.TestCase):
    def test_normalizes_to_sorted_pairs(self):
        self.assertEqual(
            normalize_query_params({"b": 2, "a": "1", "c": None, "d": ""}),
            (("a", "1"), ("b", "2"), ("c", ""), ("d", "")),
        )


class RedactionTests(unittest.TestCase):
    def test_redacts_bearer_headers(self):
        self.assertEqual(
            redact_secrets("Authorization: Bearer abc123def456"),
            "Authorization: Bearer <redacted>",
        )

    def test_redacts_key_value_pairs(self):
        # The fake credential is assembled at runtime so no token-shaped literal
        # is ever persisted in this repository.
        fake = "JUSTONEAPI_TOKEN=" + "dead" + "beefcafe"
        self.assertEqual(redact_secrets(fake), "JUSTONEAPI_TOKEN=<redacted>")
        self.assertEqual(
            redact_secrets('{"api_key": "sk-live-abcdef123456"}'),
            '{"api_key": "<redacted>"}',
        )

    def test_redacts_bare_hex_and_prefixed_tokens(self):
        self.assertNotIn("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", redact_secrets("id a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6 end"))
        self.assertNotIn("sk-liveabcdef123456", redact_secrets("key sk-liveabcdef123456"))

    def test_leaves_ordinary_text_alone(self):
        message = "tieba search failed: HTTP 503 after 3 attempts for https://tieba.baidu.com/p/1"
        self.assertEqual(redact_secrets(message), message)


if __name__ == "__main__":
    unittest.main()
