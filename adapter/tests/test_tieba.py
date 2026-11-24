"""Tests for Tieba discovery (public search JSON) and deep dives (aiotieba)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from source_adapter.providers.tieba import (
    AiotiebaProvider,
    TiebaPublicProvider,
    public_search_coverage,
)
try:  # `python3 -m unittest tests.test_x`
    from tests.helpers import FakeTransport, bytes_response, json_response, load_fixture
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import FakeTransport, bytes_response, json_response, load_fixture


class FakeAiotiebaClient:
    """Async stand-in for ``aiotieba.Client`` with scripted pages."""

    def __init__(self, pages=(), comment_pages=None):
        self.pages = list(pages)
        self.comment_pages = dict(comment_pages or {})
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_posts(self, tid, pn=1):
        self.calls.append(("get_posts", tid, pn))
        if not self.pages:
            raise AssertionError("no scripted post page left")
        return self.pages.pop(0)

    async def get_comments(self, tid, pid, pn=1):
        self.calls.append(("get_comments", tid, pid, pn))
        # The real SDK takes int ids; scripted pages stay keyed by decimal string.
        key = (str(pid), pn)
        if key not in self.comment_pages:
            raise AssertionError(f"no scripted comment page for {key}")
        return self.comment_pages.pop(key)


def fake_user(name):
    return SimpleNamespace(user_name=name, show_name=name, nick_name=name, user_id=1)


def fake_comment(pid, text, user="路人甲", reply_to_id=None, reply_num=0):
    return SimpleNamespace(
        pid=pid,
        text=text,
        user=fake_user(user),
        create_time=1790097516,
        reply_to_id=reply_to_id,
        reply_num=reply_num,
        agree=2,
    )


def fake_post(pid, floor, text, user="楼主", comments=(), reply_num=None, imgs=()):
    return SimpleNamespace(
        pid=pid,
        floor=floor,
        text=text,
        user=fake_user(user),
        create_time=1790097516,
        reply_num=reply_num if reply_num is not None else len(comments),
        comments=list(comments),
        agree=5,
        imgs=list(imgs),
    )


def fake_page(objs, page=1, has_more=False):
    return SimpleNamespace(objs=list(objs), page=page, has_more=has_more)


class ProviderTestCase(unittest.TestCase):
    def build(self, responses, **options):
        transport = FakeTransport(responses)
        provider = TiebaPublicProvider(http=_http(transport, **options.pop("http", {})))
        return provider, transport


def _http(transport, **overrides):
    from source_adapter.http import HttpClient
    from tests.helpers import FakeClock

    clock = overrides.pop("clock", None) or FakeClock()
    options = {
        "transport": transport,
        "provider": "tieba",
        "spacing": 0.0,
        "sleeper": clock.sleep,
        "monotonic": clock.monotonic,
    }
    options.update(overrides)
    return HttpClient(**options)


class PublicSearchTests(ProviderTestCase):
    def test_parses_saved_payload_into_literal_fields(self):
        provider, transport = self.build([json_response(load_fixture("tieba_search.json"))])
        result = provider.search(
            "高中 暗恋", page=1, page_size=20, raw_ref="raw/tieba-public-search-p1.json"
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.records), 3)
        first = result.records[0]
        self.assertEqual(first.source_id, "tieba:11045552764")
        self.assertEqual(first.platform, "tieba")
        self.assertEqual(first.record_kind, "thread")
        self.assertEqual(first.source_type, "community")
        self.assertEqual(first.title, "想了想还是来这里记录一下高中两年的暗恋")
        self.assertEqual(first.url, "https://tieba.baidu.com/p/11045552764")
        self.assertEqual(first.author_display_name, "空白231")
        self.assertEqual(first.published_at, "2026-09-22T17:18:36Z")
        self.assertTrue(first.excerpt.startswith("起因是今天他第一次点赞了我的朋友圈"))
        self.assertLessEqual(len(first.excerpt), 201)
        self.assertEqual(first.engagement["replies"], 14)
        self.assertEqual(first.engagement["likes"], 0)
        self.assertEqual(first.extra["forum_name"], "暗恋")
        self.assertEqual(first.extra["search_rank"], 1)
        self.assertEqual(first.extra["page"], 1)
        self.assertEqual(first.collection_method, "tieba_public_search_json")
        self.assertEqual(first.raw_ref, "raw/tieba-public-search-p1.json")
        self.assertEqual(result.raw_payloads[0].name, "raw/tieba-public-search-p1.json")
        self.assertEqual(
            json.loads(result.raw_payloads[0].content.decode("utf-8"))["no"], 0
        )

    def test_request_is_bounded_to_the_requested_page(self):
        provider, transport = self.build([json_response(load_fixture("tieba_search.json"))])
        provider.search("高中 暗恋", page=2, page_size=20)
        url = transport.calls[0].url
        self.assertIn("pn=2", url)
        self.assertIn("rn=20", url)
        self.assertIn("word=", url)

    def test_discovery_is_partial_and_reports_more_upstream_pages(self):
        provider, _ = self.build([json_response(load_fixture("tieba_search.json"))])
        result = provider.search("高中 暗恋", page=1)
        self.assertEqual(result.coverage.level, "partial")
        self.assertEqual(result.coverage.discovered_pages, 1)
        self.assertTrue(result.coverage.upstream_reports_more)
        self.assertIn("摘要", result.coverage.notes)
        self.assertEqual(result.termination, "unknown")

    def test_exhausted_when_upstream_has_no_more(self):
        payload = load_fixture("tieba_search.json")
        payload["data"]["has_more"] = 0
        provider, _ = self.build([json_response(payload)])
        result = provider.search("高中 暗恋", page=1)
        self.assertFalse(result.coverage.upstream_reports_more)

    def test_page_limit_cap_is_marked_as_a_gap(self):
        provider, _ = self.build([json_response(load_fixture("tieba_search.json"))])
        result = provider.search("高中 暗恋", page=2, page_limit=2)
        self.assertIn("页数上限", result.coverage.notes)
        self.assertTrue(result.coverage.upstream_reports_more)
        self.assertTrue(any("上限" in gap for gap in result.gaps))

    def test_public_search_coverage_helper_is_explicit(self):
        coverage = public_search_coverage(has_more=True, page=3, page_limit=3, page_size=20)
        self.assertEqual(coverage.level, "partial")
        self.assertTrue(coverage.notes)

    def test_records_keep_fixture_order(self):
        provider, _ = self.build([json_response(load_fixture("tieba_search.json"))])
        result = provider.search("高中 暗恋", page=1)
        self.assertEqual(
            [record.source_id for record in result.records],
            ["tieba:11045552764", "tieba:10845153904", "tieba:11000628066"],
        )

    def test_malformed_json_degrades_without_records(self):
        provider, _ = self.build([bytes_response(b"<html>blocked</html>")])
        result = provider.search("高中 暗恋", page=1)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.records, ())
        self.assertEqual(result.coverage.level, "failed")
        self.assertIn("JSON", result.error)

    def test_upstream_error_field_degrades(self):
        provider, _ = self.build([json_response({"no": 1, "error": "search failed"})])
        result = provider.search("高中 暗恋", page=1)
        self.assertEqual(result.status, "degraded")
        self.assertIn("search failed", result.error)
        self.assertEqual(result.coverage.level, "failed")

    def test_missing_post_list_degrades_with_shape_note(self):
        provider, _ = self.build([json_response({"no": 0, "error": "success", "data": {}})])
        result = provider.search("高中 暗恋", page=1)
        self.assertEqual(result.status, "degraded")
        self.assertIn("post_list", result.error)
        self.assertEqual(result.records, ())

    def test_items_without_ids_are_skipped_and_reported(self):
        payload = load_fixture("tieba_search.json")
        payload["data"]["post_list"].append({"title": "没有 tid 的脏数据"})
        provider, _ = self.build([json_response(payload)])
        result = provider.search("高中 暗恋", page=1)
        self.assertEqual(len(result.records), 3)
        self.assertEqual(result.status, "degraded")
        self.assertIn("跳过 1", result.detail)

    def test_transport_failure_is_unavailable(self):
        provider, _ = self.build([bytes_response(b"", status=503) for _ in range(4)])
        result = provider.search("高中 暗恋", page=1)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.coverage.level, "failed")
        self.assertIn("503", result.error)
        self.assertEqual(result.records, ())

    def test_doctor_reports_ready_without_network(self):
        provider, transport = self.build([])
        status = provider.doctor()
        self.assertEqual(status.status, "ready")
        self.assertEqual(status.provider, "tieba")
        self.assertEqual(transport.call_count, 0)
        self.assertIn("search", status.capabilities)


class AiotiebaTests(unittest.TestCase):
    def build(self, pages=(), comment_pages=None, module=None, importer=None):
        client = FakeAiotiebaClient(pages=pages, comment_pages=comment_pages)
        provider = AiotiebaProvider(
            importer=importer or (lambda name: module or SimpleNamespace()),
            client_factory=lambda _module: client,
        )
        return provider, client

    def test_doctor_is_unavailable_without_the_optional_dependency(self):
        def failing_importer(name):
            raise ImportError(f"No module named {name!r}")

        provider = AiotiebaProvider(importer=failing_importer)
        status = provider.doctor()
        self.assertEqual(status.status, "unavailable")
        self.assertIn("aiotieba", status.detail)

    def test_fetch_without_dependency_returns_structured_failure(self):
        def failing_importer(name):
            raise ImportError(f"No module named {name!r}")

        provider = AiotiebaProvider(importer=failing_importer)
        result = provider.fetch_thread("11045552764", page_limit=1)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.coverage.level, "failed")
        self.assertIn("aiotieba", result.error)
        self.assertTrue(result.gaps)

    def test_fetch_maps_floors_and_comments_with_parents(self):
        floor_one = fake_post("11045552764", 1, "起因是今天他第一次点赞了我的朋友圈。")
        floor_two = fake_post(
            "2222",
            2,
            "楼主加油，我也有类似的经历。",
            user="空白231",
            comments=[fake_comment("3333", "同感，我也在暗恋。")],
        )
        provider, client = self.build(pages=[fake_page([floor_one, floor_two], page=1, has_more=False)])
        result = provider.fetch_thread(
            "11045552764",
            page_limit=1,
            thread_title="高中两年的暗恋",
            raw_ref_prefix="raw/tieba-thread-11045552764",
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.termination, "exhausted")
        thread = result.records[0]
        self.assertEqual(thread.source_id, "tieba:11045552764")
        self.assertEqual(thread.record_kind, "thread")
        self.assertEqual(thread.title, "高中两年的暗恋")
        self.assertEqual(thread.floor_number, 1)
        self.assertEqual(thread.full_text, "起因是今天他第一次点赞了我的朋友圈。")
        self.assertEqual(thread.coverage.level, "complete")
        self.assertEqual(thread.coverage.content_pages, 1)
        floor = result.records[1]
        self.assertEqual(floor.source_id, "tieba:2222")
        self.assertEqual(floor.record_kind, "post")
        self.assertEqual(floor.parent_id, "tieba:11045552764")
        self.assertEqual(floor.thread_id, "tieba:11045552764")
        self.assertEqual(floor.floor_number, 2)
        self.assertEqual(floor.author_display_name, "空白231")
        comment = result.comments[0]
        self.assertEqual(comment.source_id, "tieba:3333")
        self.assertEqual(comment.parent_id, "tieba:2222")
        self.assertEqual(comment.thread_id, "tieba:11045552764")
        self.assertEqual(comment.url, "https://tieba.baidu.com/p/11045552764?pid=3333")
        self.assertEqual(client.calls, [("get_posts", 11045552764, 1)])
        self.assertEqual(len(result.raw_payloads), 1)
        self.assertTrue(result.raw_payloads[0].name.startswith("raw/tieba-thread-11045552764"))

    def test_op_title_falls_back_to_opening_text_and_is_flagged(self):
        floor_one = fake_post("11045552764", 1, "今天想记录一件小事。")
        provider, _ = self.build(pages=[fake_page([floor_one], page=1, has_more=False)])
        result = provider.fetch_thread("11045552764", page_limit=1)
        thread = result.records[0]
        self.assertEqual(thread.title, "今天想记录一件小事。")
        self.assertEqual(thread.extra["title_source"], "opening_excerpt")

    def test_page_limit_marks_partial_and_reports_more(self):
        pages = [
            fake_page([fake_post("11045552764", 1, "第一页")], page=1, has_more=True),
            fake_page([fake_post("2222", 2, "第二页")], page=2, has_more=True),
        ]
        provider, client = self.build(pages=pages)
        result = provider.fetch_thread("11045552764", page_limit=2)
        self.assertEqual(result.termination, "page_limit")
        self.assertEqual(result.coverage.level, "partial")
        self.assertTrue(result.coverage.upstream_reports_more)
        self.assertEqual(result.coverage.content_pages, 2)
        self.assertIn("页数上限", result.coverage.notes)
        self.assertEqual([call[2] for call in client.calls], [1, 2])

    def test_nested_replies_are_fetched_only_when_requested(self):
        parent_comment = fake_comment("3333", "同感，我也在暗恋。", reply_num=3)
        floor_two = fake_post("2222", 2, "楼主加油。", comments=[parent_comment], reply_num=3)
        children = fake_page(
            [
                fake_comment("4444", "抱抱你。", reply_to_id="3333"),
                fake_comment("5555", "蹲后续。", reply_to_id="3333"),
            ]
        )
        provider, client = self.build(
            pages=[fake_page([fake_post("11045552764", 1, "正文"), floor_two], page=1, has_more=False)],
            comment_pages={("3333", 1): children},
        )
        result = provider.fetch_thread("11045552764", page_limit=1, include_nested=True)
        nested = [comment for comment in result.comments if comment.source_id in ("tieba:4444", "tieba:5555")]
        self.assertEqual(len(nested), 2)
        self.assertEqual(nested[0].parent_id, "tieba:3333")
        self.assertTrue(result.coverage.includes_nested_comments)
        self.assertEqual(result.coverage.comment_pages, 1)
        self.assertIn(("get_comments", 11045552764, 3333, 1), client.calls)

    def test_nested_replies_are_not_fetched_by_default(self):
        parent_comment = fake_comment("3333", "同感。", reply_num=3)
        floor_two = fake_post("2222", 2, "楼主加油。", comments=[parent_comment], reply_num=3)
        provider, client = self.build(
            pages=[fake_page([fake_post("11045552764", 1, "正文"), floor_two], page=1, has_more=False)]
        )
        result = provider.fetch_thread("11045552764", page_limit=1)
        self.assertFalse(result.coverage.includes_nested_comments)
        self.assertEqual([call[0] for call in client.calls], ["get_posts"])
        self.assertEqual(result.coverage.level, "complete")

    def test_malformed_floors_are_skipped_and_degrade_the_result(self):
        good = fake_post("2222", 2, "正常楼层")
        broken = SimpleNamespace(pid=None, floor=None, text=None, user=None)
        provider, _ = self.build(
            pages=[fake_page([fake_post("11045552764", 1, "正文"), good, broken], page=1, has_more=False)]
        )
        result = provider.fetch_thread("11045552764", page_limit=1)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(len(result.records), 2)
        self.assertIn("跳过 1", result.detail)


if __name__ == "__main__":
    unittest.main()
