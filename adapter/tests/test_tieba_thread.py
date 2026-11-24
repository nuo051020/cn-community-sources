"""Tests for the anonymous public-web Tieba thread provider (no aiotieba).

Fixtures under ``tests/fixtures/tieba_thread_*`` were captured read-only from
``https://tieba.baidu.com/mo/q/thread`` (no cookies, no login, no attempt to
bypass the 安全验证 wall). See the module docstring of ``source_adapter.providers.tieba_thread``
for the endpoints, the required headers and the documented limits of that page.

Fixture provenance
------------------
* ``tieba_thread_11045552764_p1.html`` - tid 11045552764 (暗恋吧), all 14 floors of
  the captured page, texts verbatim, one server-rendered 楼中楼 reply on floor 4.
* ``tieba_thread_full_p1.html`` / ``..._full_p1_repeat.html`` - tid 2697175246, the
  30-floor page (the upstream cap) twice; the two bodies differ only in volatile
  trace tokens, matching what the live endpoint answers for ``pn=2``.
* ``tieba_thread_11045552764_p2.html`` - a second-page shape (floors 31-34) built
  from the real floor markup of tid 11033510026 with renumbered floor markers.
* ``tieba_thread_11045552764_empty.html`` - the "正在加载中" shell the endpoint
  returns when a thread cannot be served.
"""

from __future__ import annotations

import unittest

from source_adapter.http import HttpClient
from source_adapter.providers.tieba_thread import (
    HTML_ENDPOINT,
    NESTED_LIMIT_GAP,
    PAGE_LIMIT_GAP,
    REPEATED_PAGE_GAP,
    TiebaThreadProvider,
)
try:  # `python3 -m unittest tests.test_tieba_thread`
    from tests.helpers import (
        FakeClock,
        FakeTransport,
        bytes_response,
        read_fixture_bytes,
    )
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import (
        FakeClock,
        FakeTransport,
        bytes_response,
        read_fixture_bytes,
    )

TID = "11045552764"
THREAD_ID = "tieba:11045552764"
PAGE1_FIXTURE = "tieba_thread_11045552764_p1.html"
PAGE2_FIXTURE = "tieba_thread_11045552764_p2.html"
EMPTY_FIXTURE = "tieba_thread_11045552764_empty.html"
FULL_PAGE_FIXTURE = "tieba_thread_full_p1.html"
FULL_PAGE_REPEAT_FIXTURE = "tieba_thread_full_p1_repeat.html"

# Literal values frozen from the captured page (never derived from the clock).
FLOOR4_SOURCE_ID = "tieba:153960720322"
NESTED_PID = "153960725917"
TITLE = "想了想还是来这里记录一下高中两年的暗恋"
FLOOR1_TIME = "2026-09-23T01:18:00Z"
FLOOR1_AUTHOR = "贴吧用户_7yEb1EC"
FLOOR1_TEXT_HEAD = "起因是今天他第一次点赞了我的朋友圈"


def fixture_response(name, url=None):
    """Return an HttpResponse carrying one frozen page fixture."""
    return bytes_response(
        read_fixture_bytes(name),
        status=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        url=url or f"{HTML_ENDPOINT}?kz={TID}&pn=1",
    )


class TiebaThreadProviderTests(unittest.TestCase):
    def build(self, responses):
        """Build a provider on a scripted transport (no real network, no sleeps)."""
        clock = FakeClock()
        transport = FakeTransport(responses)
        http = HttpClient(
            transport=transport,
            provider="tieba-thread",
            spacing=0.0,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )
        return TiebaThreadProvider(http=http), transport

    # -- 1. parsing frozen fixtures into literal fields ------------------

    def test_parses_saved_page_into_literal_fields(self):
        provider, transport = self.build([fixture_response(PAGE1_FIXTURE)])
        result = provider.fetch_thread(TID, page_limit=1)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.termination, "exhausted")
        self.assertEqual(result.provider, "tieba-thread")
        self.assertEqual(len(result.records), 14)
        self.assertEqual(transport.call_count, 1)

        opening = result.records[0]
        self.assertEqual(opening.source_id, THREAD_ID)
        self.assertEqual(opening.record_kind, "thread")
        self.assertEqual(opening.source_type, "community")
        self.assertIsNone(opening.thread_id)
        self.assertIsNone(opening.parent_id)
        self.assertEqual(opening.floor_number, 1)
        self.assertEqual(opening.published_at, FLOOR1_TIME)
        self.assertEqual(opening.author_display_name, FLOOR1_AUTHOR)
        self.assertTrue(opening.full_text.startswith(FLOOR1_TEXT_HEAD))
        self.assertIn("剩下放评论", opening.full_text)
        self.assertEqual(opening.title, TITLE)
        self.assertEqual(opening.extra["title_source"], "opening_excerpt")
        self.assertEqual(opening.extra["floor_pid"], "153960692835")
        self.assertEqual(opening.extra["forum_name"], "暗恋")
        self.assertEqual(opening.extra["page"], 1)
        self.assertEqual(opening.url, f"https://tieba.baidu.com/p/{TID}")
        self.assertEqual(opening.raw_ref, "raw/tieba-thread-p1.html")
        self.assertEqual(opening.coverage.level, "complete")

        second = result.records[1]
        self.assertEqual(second.source_id, "tieba:153960700784")
        self.assertEqual(second.record_kind, "post")
        self.assertEqual(second.thread_id, THREAD_ID)
        self.assertEqual(second.parent_id, THREAD_ID)
        self.assertEqual(second.floor_number, 2)
        self.assertEqual(second.published_at, "2026-09-23T01:23:00Z")
        self.assertEqual(second.url, f"https://tieba.baidu.com/p/{TID}?pid=153960700784")
        self.assertTrue(second.full_text.startswith("如主楼所说"))
        self.assertEqual(second.title, TITLE)
        self.assertEqual(second.excerpt, second.full_text)

    def test_provided_thread_title_wins_and_is_flagged(self):
        provider, _ = self.build([fixture_response(PAGE1_FIXTURE)])
        result = provider.fetch_thread(TID, thread_title="自定义标题")
        opening = result.records[0]
        self.assertEqual(opening.title, "自定义标题")
        self.assertEqual(opening.extra["title_source"], "provided")
        self.assertEqual(result.records[1].title, "自定义标题")
        self.assertEqual(result.records[1].extra["title_source"], "provided")

    def test_raw_payload_is_the_untouched_response_and_uses_the_prefix(self):
        provider, _ = self.build([fixture_response(PAGE1_FIXTURE)])
        result = provider.fetch_thread(TID, raw_ref_prefix="raw/demo")
        self.assertEqual(len(result.raw_payloads), 1)
        payload = result.raw_payloads[0]
        self.assertEqual(payload.name, "raw/demo-p1.html")
        self.assertEqual(payload.content, read_fixture_bytes(PAGE1_FIXTURE))
        self.assertEqual(payload.media_type, "text/html")
        for record in result.records:
            self.assertEqual(record.raw_ref, "raw/demo-p1.html")

    # -- 2. parent/child relationships ----------------------------------

    def test_floor_and_nested_parent_relationships_are_literal(self):
        provider, _ = self.build([fixture_response(PAGE1_FIXTURE)])
        result = provider.fetch_thread(TID, include_nested=True)

        floor = next(record for record in result.records if record.floor_number == 4)
        self.assertEqual(floor.source_id, FLOOR4_SOURCE_ID)
        self.assertEqual(floor.record_kind, "post")
        self.assertEqual(floor.thread_id, THREAD_ID)
        self.assertEqual(floor.parent_id, THREAD_ID)
        self.assertEqual(floor.author_display_name, FLOOR1_AUTHOR)

        self.assertEqual(len(result.comments), 1)
        comment = result.comments[0]
        self.assertEqual(comment.source_id, f"tieba:{NESTED_PID}")
        self.assertEqual(comment.platform, "tieba")
        self.assertEqual(comment.thread_id, THREAD_ID)
        self.assertEqual(comment.parent_id, FLOOR4_SOURCE_ID)
        self.assertEqual(comment.url, f"https://tieba.baidu.com/p/{TID}?pid={NESTED_PID}")
        self.assertEqual(comment.author_display_name, FLOOR1_AUTHOR)
        self.assertTrue(comment.full_text.startswith("不过那个女生当时有男朋友了"))
        self.assertEqual(comment.engagement["likes"], 0)
        self.assertEqual(comment.extra["nested"], True)
        self.assertEqual(comment.raw_ref, "raw/tieba-thread-p1.html")

    # -- 3. nested comments are opt-in ----------------------------------

    def test_nested_comments_are_not_requested_by_default(self):
        provider, transport = self.build([fixture_response(PAGE1_FIXTURE)])
        result = provider.fetch_thread(TID, page_limit=1)

        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.calls[0].url, f"{HTML_ENDPOINT}?kz={TID}&pn=1")
        self.assertEqual(result.comments, ())
        self.assertFalse(result.coverage.includes_nested_comments)
        self.assertEqual(result.coverage.comment_pages, 0)

    def test_include_nested_fetches_and_counts_comment_pages(self):
        provider, transport = self.build([fixture_response(PAGE1_FIXTURE)])
        result = provider.fetch_thread(TID, page_limit=1, include_nested=True)

        self.assertTrue(result.coverage.includes_nested_comments)
        self.assertGreaterEqual(result.coverage.comment_pages, 1)
        self.assertEqual(len(result.comments), 1)
        self.assertEqual(result.coverage.level, "complete")
        # Nested replies ride along in the same HTML response.
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.calls[0].url, f"{HTML_ENDPOINT}?kz={TID}&pn=1")

    def test_max_comment_pages_bounds_nested_parsing_and_reports_a_gap(self):
        provider, _ = self.build(
            [fixture_response(FULL_PAGE_FIXTURE), fixture_response(PAGE2_FIXTURE)]
        )
        result = provider.fetch_thread(
            TID, page_limit=2, include_nested=True, max_comment_pages=1
        )

        self.assertIn(NESTED_LIMIT_GAP, result.gaps)
        self.assertIn("max_comment_pages", result.coverage.notes)
        self.assertEqual(result.coverage.level, "partial")
        self.assertEqual(result.coverage.comment_pages, 1)
        self.assertTrue(result.coverage.includes_nested_comments)
        self.assertTrue(result.comments)

    # -- 4. page limits --------------------------------------------------

    def test_page_limit_reports_partial_and_a_cap_gap(self):
        # A full 30-floor page is the upstream cap: the provider stops at the
        # requested page_limit instead of walking on.
        provider, transport = self.build([fixture_response(FULL_PAGE_FIXTURE)])
        result = provider.fetch_thread(TID, page_limit=1)

        self.assertEqual(result.termination, "page_limit")
        self.assertEqual(result.coverage.level, "partial")
        self.assertTrue(result.coverage.upstream_reports_more)
        self.assertIn("页数上限", result.coverage.notes)
        self.assertIn(PAGE_LIMIT_GAP, result.gaps)
        self.assertTrue(any("上限" in gap for gap in result.gaps))
        self.assertEqual(result.coverage.content_pages, 1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.calls[0].url, f"{HTML_ENDPOINT}?kz={TID}&pn=1")
        self.assertEqual(len(result.records), 30)
        self.assertEqual(result.records[0].floor_number, 1)

    def test_second_page_is_collected_when_the_cap_allows_it(self):
        provider, transport = self.build(
            [fixture_response(FULL_PAGE_FIXTURE), fixture_response(PAGE2_FIXTURE)]
        )
        result = provider.fetch_thread(TID, page_limit=2)

        self.assertEqual(result.termination, "exhausted")
        self.assertEqual(result.coverage.content_pages, 2)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(
            [call.url for call in transport.calls],
            [
                f"{HTML_ENDPOINT}?kz={TID}&pn=1",
                f"{HTML_ENDPOINT}?kz={TID}&pn=2",
            ],
        )
        self.assertEqual(len(result.records), 34)
        self.assertEqual(
            [record.floor_number for record in result.records[30:]],
            [31, 32, 33, 34],
        )
        self.assertEqual(result.records[30].extra["page"], 2)
        self.assertEqual(result.records[30].author_display_name, "路过的路人甲")
        self.assertEqual(result.records[30].raw_ref, "raw/tieba-thread-p2.html")
        self.assertEqual(result.raw_payloads[1].name, "raw/tieba-thread-p2.html")
        self.assertEqual(result.coverage.level, "complete")

    def test_exhausted_when_upstream_returns_a_short_page(self):
        provider, transport = self.build([fixture_response(PAGE1_FIXTURE)])
        result = provider.fetch_thread(TID, page_limit=5)

        self.assertEqual(result.termination, "exhausted")
        self.assertEqual(result.coverage.level, "complete")
        self.assertFalse(result.coverage.upstream_reports_more)
        self.assertEqual(result.coverage.content_pages, 1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result.gaps, ())

    # -- 5. repeated page guard -----------------------------------------

    def test_repeated_page_stops_immediately_without_duplicate_records(self):
        provider, transport = self.build(
            [
                fixture_response(FULL_PAGE_FIXTURE),
                fixture_response(FULL_PAGE_REPEAT_FIXTURE),
            ]
        )
        result = provider.fetch_thread(TID, page_limit=3)

        self.assertEqual(result.termination, "repeated_page")
        self.assertEqual(len(result.records), 30)
        source_ids = [record.source_id for record in result.records]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertEqual(result.coverage.content_pages, 2)
        self.assertIn(REPEATED_PAGE_GAP, result.gaps)
        self.assertIn("重复", result.coverage.notes)
        self.assertEqual(result.coverage.level, "partial")
        self.assertEqual(transport.call_count, 2)

    # -- 6. failure handling --------------------------------------------

    def test_transport_failure_is_unavailable_with_the_status_code(self):
        provider, _ = self.build(
            [bytes_response(b"<html>upstream boom</html>", status=503) for _ in range(4)]
        )
        result = provider.fetch_thread(TID, page_limit=1)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.coverage.level, "failed")
        self.assertEqual(result.records, ())
        self.assertEqual(result.comments, ())
        self.assertIn("503", result.error)
        self.assertEqual(result.termination, "error")
        self.assertTrue(result.gaps)
        self.assertEqual(len(result.raw_payloads), 1)

    def test_unparsable_page_degrades_without_records(self):
        provider, _ = self.build(
            [bytes_response(read_fixture_bytes(EMPTY_FIXTURE), status=200)]
        )
        result = provider.fetch_thread(TID, page_limit=1)

        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.coverage.level, "failed")
        self.assertEqual(result.records, ())
        self.assertEqual(result.comments, ())
        self.assertIn("楼层容器", result.error)
        self.assertEqual(result.termination, "error")
        self.assertEqual(len(result.raw_payloads), 1)

    def test_single_broken_floor_is_skipped_and_reported(self):
        body = read_fixture_bytes(PAGE1_FIXTURE).decode("utf-8")
        broken = (
            '<li tid="999999" fn="99" data-info="{&quot;pid&quot;:999999,&quot;floor_num&quot;:99}"'
            ' class="list_item post_list_item j_post_list_item"><div class="list_item_wrapper">'
            '<div class="list_main"><div class="content">   </div></div></div></li>'
        )
        # Insert the broken floor right after the opening post, before floor 2.
        marker = '<div class="fr_list 2 j_floor_panel"'
        self.assertIn(marker, body)
        modified = body.replace(marker, broken + marker, 1)
        provider, _ = self.build([bytes_response(modified.encode("utf-8"))])
        result = provider.fetch_thread(TID, page_limit=1)

        self.assertEqual(result.status, "degraded")
        self.assertEqual(len(result.records), 14)
        self.assertIn("跳过 1", result.detail)
        self.assertTrue(any("跳过" in gap for gap in result.gaps))
        self.assertEqual(result.coverage.level, "partial")

    def test_error_strings_are_redacted(self):
        provider, _ = self.build(
            [
                bytes_response(
                    b"",
                    status=599,
                    error="Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345",
                )
                for _ in range(4)
            ]
        )
        result = provider.fetch_thread(TID, page_limit=1)

        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", result.error)
        self.assertIn("redacted", result.error)

    # -- 7. local readiness ---------------------------------------------

    def test_doctor_is_ready_without_any_network_call(self):
        provider, transport = self.build([])
        status = provider.doctor()

        self.assertEqual(status.status, "ready")
        self.assertEqual(status.provider, "tieba-thread")
        self.assertEqual(transport.call_count, 0)
        self.assertIn("thread", status.capabilities)
        self.assertIn("公开 Web 接口", status.detail)
        self.assertIn("aiotieba", status.detail)
        self.assertTrue(status.checked_at)

    def test_invalid_arguments_are_rejected_locally(self):
        provider, transport = self.build([])
        with self.assertRaises(ValueError):
            provider.fetch_thread("   ")
        with self.assertRaises(ValueError):
            provider.fetch_thread(TID, page_limit=0)
        with self.assertRaises(ValueError):
            provider.fetch_thread(TID, include_nested=True, max_comment_pages=0)
        self.assertEqual(transport.call_count, 0)


if __name__ == "__main__":
    unittest.main()
