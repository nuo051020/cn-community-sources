"""Tests for the evidence model: validation, stable ids, merging, comments."""

from __future__ import annotations

import json
import unittest

from source_adapter.models import (
    CommentRecord,
    Coverage,
    ProviderStatus,
    SourceRecord,
    dedupe_records,
    merge_records,
    stable_source_id,
)


def make_record(**overrides):
    base = {
        "source_id": "tieba:11045552764",
        "platform": "tieba",
        "record_kind": "thread",
        "source_type": "community",
        "title": "想了想还是来这里记录一下高中两年的暗恋",
        "url": "https://tieba.baidu.com/p/11045552764",
        "author_display_name": "你回复就不能",
        "published_at": "2026-09-22T10:38:36Z",
        "retrieved_at": "2026-09-24T12:00:00Z",
        "excerpt": "起因是今天他第一次点赞了我的朋友圈",
        "full_text": "起因是今天他第一次点赞了我的朋友圈，我以为我早就不喜欢他了。",
        "collection_method": "tieba_public_search_json",
        "coverage": Coverage(level="partial", discovered_pages=1),
        "raw_ref": "raw/tieba-public-search-p1.json",
    }
    base.update(overrides)
    return SourceRecord(**base)


class CoverageTests(unittest.TestCase):
    def test_default_level_is_unknown(self):
        coverage = Coverage()
        self.assertEqual(coverage.level, "unknown")
        self.assertEqual(coverage.rank(), 1)

    def test_rejects_unknown_level(self):
        with self.assertRaises(ValueError) as ctx:
            Coverage(level="done")
        self.assertIn("coverage level", str(ctx.exception))

    def test_rejects_negative_counters(self):
        with self.assertRaises(ValueError):
            Coverage(level="partial", discovered_pages=-1)

    def test_rank_orders_levels(self):
        self.assertLess(Coverage(level="failed").rank(), Coverage(level="unknown").rank())
        self.assertLess(Coverage(level="unknown").rank(), Coverage(level="partial").rank())
        self.assertLess(Coverage(level="partial").rank(), Coverage(level="complete").rank())

    def test_round_trips_through_dict(self):
        coverage = Coverage(
            level="partial",
            discovered_pages=2,
            content_pages=1,
            comment_pages=3,
            includes_nested_comments=True,
            upstream_reports_more=True,
            notes="达到页数上限",
        )
        self.assertEqual(Coverage.from_dict(coverage.to_dict()), coverage)


class StableIdTests(unittest.TestCase):
    def test_prefers_platform_object_id(self):
        self.assertEqual(stable_source_id("tieba", object_id="11045552764"), "tieba:11045552764")
        self.assertEqual(stable_source_id("tieba", object_id=11045552764), "tieba:11045552764")
        self.assertEqual(stable_source_id("tieba", object_id=" 11045552764 "), "tieba:11045552764")

    def test_normalizes_bilibili_prefix_only(self):
        # BV ids are case-sensitive beyond the two-character prefix.
        self.assertEqual(stable_source_id("bilibili", object_id="bv1Xx411c7mD"), "bilibili:BV1Xx411c7mD")
        self.assertEqual(stable_source_id("bilibili", object_id="AV12345"), "bilibili:av12345")

    def test_falls_back_to_normalized_url(self):
        self.assertEqual(
            stable_source_id("anysearch", url="https://Example.com/a/?utm_source=x&b=2&a=1#frag"),
            "anysearch:url:051029b6a13fc668",
        )

    def test_last_resort_hash_is_stable(self):
        first = stable_source_id(
            "tieba", title="标题", author="作者", published_at="2026-01-01T00:00:00Z"
        )
        second = stable_source_id(
            "tieba", title="标题", author="作者", published_at="2026-01-01T00:00:00Z"
        )
        self.assertEqual(first, "tieba:h:0228932f6dd4f4a5")
        self.assertEqual(first, second)

    def test_requires_some_identity(self):
        with self.assertRaises(ValueError):
            stable_source_id("tieba")


class SourceRecordTests(unittest.TestCase):
    def test_rejects_blank_platform(self):
        with self.assertRaises(ValueError) as ctx:
            make_record(platform="   ")
        self.assertIn("platform", str(ctx.exception))

    def test_rejects_blank_title_for_non_comment_records(self):
        with self.assertRaises(ValueError) as ctx:
            make_record(title="")
        self.assertIn("title", str(ctx.exception))

    def test_allows_blank_title_for_comments(self):
        record = make_record(record_kind="comment", title="", source_type="community")
        self.assertEqual(record.title, "")

    def test_rejects_unknown_record_kind_and_source_type(self):
        with self.assertRaises(ValueError):
            make_record(record_kind="diary")
        with self.assertRaises(ValueError):
            make_record(source_type="blog")

    def test_rejects_bad_status(self):
        with self.assertRaises(ValueError):
            make_record(status="maybe")

    def test_allows_title_only_records(self):
        # A search result may expose only a title; the title itself is observed
        # evidence and the exporter falls back to it for the excerpt.
        record = make_record(excerpt="", full_text="", url="")
        self.assertEqual(record.title, "想了想还是来这里记录一下高中两年的暗恋")

    def test_rejects_comment_without_any_text_or_url(self):
        with self.assertRaises(ValueError):
            make_record(
                record_kind="comment", title="", excerpt="", full_text="", url="", status="ok"
            )

    def test_failed_record_may_carry_only_an_error(self):
        record = make_record(
            excerpt="", full_text="", url="", status="failed", error="upstream returned 503"
        )
        self.assertEqual(record.error, "upstream returned 503")

    def test_to_dict_is_json_serializable(self):
        payload = make_record().to_dict()
        self.assertEqual(payload["coverage"]["level"], "partial")
        self.assertEqual(payload["raw_ref"], "raw/tieba-public-search-p1.json")
        self.assertIsInstance(json.dumps(payload), str)

    def test_round_trips_through_dict(self):
        record = make_record()
        self.assertEqual(SourceRecord.from_dict(record.to_dict()), record)


class CommentRecordTests(unittest.TestCase):
    def test_keeps_parent_and_floor_relationship(self):
        thread = stable_source_id("tieba", object_id="11045552764")
        comment = CommentRecord(
            source_id="tieba:153960692835",
            platform="tieba",
            thread_id=thread,
            parent_id=thread,
            floor_number=2,
            url="https://tieba.baidu.com/p/11045552764?pid=153960692835",
            author_display_name="空白231",
            published_at="2026-09-22T11:00:00Z",
            retrieved_at="2026-09-24T12:05:00Z",
            full_text="楼主加油，我也有类似的经历。",
            collection_method="aiotieba_get_posts",
            coverage=Coverage(level="complete", content_pages=1),
            raw_ref="raw/tieba-thread-11045552764-p1.json",
        )
        self.assertEqual(comment.parent_id, thread)
        record = comment.to_source_record()
        self.assertEqual(record.record_kind, "comment")
        self.assertEqual(record.thread_id, thread)
        self.assertEqual(record.parent_id, thread)
        self.assertEqual(record.floor_number, 2)
        self.assertEqual(record.source_type, "community")
        self.assertEqual(record.excerpt, "楼主加油，我也有类似的经历。")

    def test_requires_thread_id(self):
        with self.assertRaises(ValueError):
            CommentRecord(
                source_id="tieba:1",
                platform="tieba",
                thread_id="",
                full_text="hi",
            )


class MergeTests(unittest.TestCase):
    def test_keeps_richer_text_and_higher_coverage(self):
        thin = make_record(
            source_id="tieba:11045552764",
            excerpt="起因是今天他第一次点赞了我的朋友圈",
            full_text="起因是今天他第一次点赞了我的朋友圈",
            coverage=Coverage(level="partial", discovered_pages=1),
            raw_ref="raw/tieba-public-search-p1.json",
        )
        rich = make_record(
            source_id="tieba:11045552764",
            excerpt="起因是今天他第一次点赞了我的朋友圈，我以为我早就不喜欢他了。",
            full_text="起因是今天他第一次点赞了我的朋友圈，我以为我早就不喜欢他了。" * 3,
            coverage=Coverage(level="complete", content_pages=1, comment_pages=2),
            raw_ref="raw/tieba-thread-11045552764-p1.json",
            collection_method="aiotieba_get_posts",
        )
        merged = merge_records(thin, rich)
        self.assertEqual(merged.source_id, "tieba:11045552764")
        self.assertEqual(len(merged.full_text), len(rich.full_text))
        self.assertEqual(merged.coverage.level, "complete")
        self.assertEqual(merged.coverage.discovered_pages, 1)
        self.assertEqual(merged.coverage.comment_pages, 2)
        self.assertEqual(merged.collection_method, "aiotieba_get_posts")
        self.assertIn("raw/tieba-public-search-p1.json", merged.raw_ref)
        self.assertIn("raw/tieba-thread-11045552764-p1.json", merged.raw_ref)

    def test_merge_is_order_independent_for_text(self):
        thin = make_record(full_text="短", excerpt="短")
        rich = make_record(full_text="很长的正文" * 5, excerpt="很长的正文")
        self.assertEqual(merge_records(thin, rich).full_text, merge_records(rich, thin).full_text)

    def test_survives_a_failed_side(self):
        ok = make_record()
        failed = make_record(
            source_id="tieba:11045552764",
            excerpt="",
            full_text="",
            status="failed",
            error="read timeout",
            coverage=Coverage(level="failed"),
        )
        merged = merge_records(ok, failed)
        self.assertEqual(merged.status, "ok")
        self.assertIsNone(merged.error)
        self.assertEqual(merged.coverage.level, "partial")

    def test_merges_engagement_and_extra_without_losing_keys(self):
        first = make_record(engagement={"replies": 14, "likes": 0}, extra={"forum_name": "暗恋"})
        second = make_record(engagement={"replies": 21, "shares": 3}, extra={"page": 2})
        merged = merge_records(first, second)
        self.assertEqual(merged.engagement["replies"], 21)
        self.assertEqual(merged.engagement["shares"], 3)
        self.assertEqual(merged.extra["forum_name"], "暗恋")
        self.assertEqual(merged.extra["page"], 2)

    def test_rejects_mismatched_source_ids(self):
        with self.assertRaises(ValueError):
            merge_records(make_record(), make_record(source_id="tieba:999"))

    def test_dedupe_preserves_first_seen_order(self):
        a = make_record(source_id="tieba:1", url="https://tieba.baidu.com/p/1", title="甲")
        b = make_record(source_id="tieba:2", url="https://tieba.baidu.com/p/2", title="乙")
        richer_a = make_record(
            source_id="tieba:1",
            url="https://tieba.baidu.com/p/1",
            title="甲",
            full_text="更完整的正文" * 8,
        )
        result = dedupe_records([a, b, richer_a])
        self.assertEqual([r.source_id for r in result], ["tieba:1", "tieba:2"])
        self.assertEqual(result[0].full_text, richer_a.full_text)


class ProviderStatusTests(unittest.TestCase):
    def test_limits_status_values(self):
        self.assertEqual(ProviderStatus(provider="tieba", status="ready").status, "ready")
        with self.assertRaises(ValueError):
            ProviderStatus(provider="tieba", status="ok")
        with self.assertRaises(ValueError):
            ProviderStatus(provider="tieba", status="error")

    def test_to_dict_shape(self):
        status = ProviderStatus(
            provider="anysearch",
            status="degraded",
            detail="未配置 API key，使用匿名额度",
            capabilities=("search", "extract"),
            checked_at="2026-09-24T12:00:00Z",
        )
        payload = status.to_dict()
        self.assertEqual(
            sorted(payload),
            ["capabilities", "checked_at", "detail", "provider", "status"],
        )
        self.assertEqual(payload["capabilities"], ["search", "extract"])


if __name__ == "__main__":
    unittest.main()
