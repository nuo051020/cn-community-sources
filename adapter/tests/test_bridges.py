"""Tests for the AnySearch CLI bridge and the Just One API dual-phase bridge."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from source_adapter.providers.anysearch import (
    AnySearchProvider,
    parse_anysearch_markdown,
)
from source_adapter.providers.justone import JustOneBridge
try:  # `python3 -m unittest tests.test_x`
    from tests.helpers import CompletedProcessLike, ScriptedRunner, load_fixture, read_fixture_bytes
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import CompletedProcessLike, ScriptedRunner, load_fixture, read_fixture_bytes

COMMAND = ["node", "/opt/anysearch/scripts/anysearch_cli.js"]
FAKE_KEY = "sk-live-this-key-must-never-leak-123456"


def markdown_text() -> str:
    return read_fixture_bytes("anysearch_results.md").decode("utf-8")


def make_anysearch(**overrides) -> AnySearchProvider:
    options = {
        "command": COMMAND,
        "skill_dir": Path("/opt/anysearch"),
        "env": {},
        "timeout": 60.0,
    }
    options.update(overrides)
    return AnySearchProvider(**options)


class AnySearchCommandTests(unittest.TestCase):
    def test_search_argv_and_subprocess_bounds_are_exact(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=markdown_text())])
        provider = make_anysearch(runner=runner)
        provider.search("高中 暗恋 故事", max_results=3)
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv, COMMAND + ["search", "高中 暗恋 故事", "--max_results", "3"])
        self.assertEqual(kwargs["timeout"], 60.0)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("env", kwargs)

    def test_vertical_search_adds_discovery_flags(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=markdown_text())])
        provider = make_anysearch(runner=runner)
        provider.search("校园 爱情", max_results=5, domain="social_media", sub_domain="social_media.post")
        argv, _ = runner.calls[0]
        self.assertEqual(
            argv,
            COMMAND
            + [
                "search",
                "校园 爱情",
                "--max_results",
                "5",
                "--domain",
                "social_media",
                "--sub_domain",
                "social_media.post",
            ],
        )

    def test_extract_argv_is_exact(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout="# 标题\n\n正文")])
        provider = make_anysearch(runner=runner)
        provider.extract("https://zhuanlan.zhihu.com/p/593267924")
        argv, _ = runner.calls[0]
        self.assertEqual(argv, COMMAND + ["extract", "https://zhuanlan.zhihu.com/p/593267924"])

    def test_search_rejects_out_of_range_max_results(self):
        provider = make_anysearch(runner=ScriptedRunner([]))
        with self.assertRaises(ValueError):
            provider.search("x", max_results=11)
        with self.assertRaises(ValueError):
            provider.search("x", max_results=0)
        with self.assertRaises(ValueError):
            provider.search("   ", max_results=3)


class AnySearchParsingTests(unittest.TestCase):
    def test_parses_saved_markdown_into_literal_records(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=markdown_text())])
        provider = make_anysearch(runner=runner)
        result = provider.search("高中 暗恋 故事", max_results=3)
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.records), 3)
        first = result.records[0]
        self.assertEqual(first.platform, "anysearch")
        self.assertEqual(first.record_kind, "page")
        self.assertEqual(first.source_type, "article")
        self.assertEqual(first.title, "一名男生的高中暗恋故事")
        self.assertEqual(first.url, "https://zhuanlan.zhihu.com/p/593267924")
        self.assertTrue(first.excerpt.startswith("在高中时我暗恋过一个女孩"))
        self.assertIsNone(first.published_at)
        self.assertEqual(first.engagement["search_rank"], 1)
        self.assertEqual(first.collection_method, "anysearch_cli_search")
        self.assertEqual(first.coverage.level, "partial")
        self.assertIn("摘要", first.coverage.notes)
        self.assertEqual(len(result.raw_payloads), 1)
        self.assertEqual(result.raw_payloads[0].name, "raw/anysearch-search-1.md")

    def test_trailing_search_metadata_is_preserved_separately(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=markdown_text())])
        provider = make_anysearch(runner=runner)
        result = provider.search("高中 暗恋 故事", max_results=3)
        second = result.records[1]
        self.assertEqual(second.extra["date_text"], "Nov 9, 2023")
        self.assertEqual(second.published_at, "2023-11-09T00:00:00Z")
        self.assertNotIn("date:", second.excerpt)
        third = result.records[2]
        self.assertEqual(third.extra["rating"], "4.2")
        self.assertEqual(third.extra["rating_count"], "6384")
        self.assertEqual(third.published_at, "2026-01-07T00:00:00Z")

    def test_publisher_suffix_is_split_from_title(self):
        text = (
            "## Search Results (1 results, 10ms)\n\n"
            "### 1. 你的高中暗恋故事是什么？ : r/AskReddit\n"
            "- **URL**: https://www.reddit.com/r/AskReddit/comments/3meut8/x/\n"
            "- 高中开始了，我仍然超级爱着他。\n"
        )
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=text)])
        provider = make_anysearch(runner=runner)
        record = provider.search("x", max_results=1).records[0]
        self.assertEqual(record.title, "你的高中暗恋故事是什么？ : r/AskReddit")
        self.assertEqual(record.publisher, "r/AskReddit")

    def test_markdown_parser_handles_blocks_without_urls(self):
        parsed = parse_anysearch_markdown("## Search Results\n\n### 1. 无链接结果\n- 只有摘要\n")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["url"], "")

    def test_json_stdout_is_also_supported(self):
        payload = {
            "results": [
                {"title": "结构化结果", "url": "https://example.com/a", "snippet": "摘要文本"}
            ]
        }
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=json.dumps(payload, ensure_ascii=False))])
        provider = make_anysearch(runner=runner)
        result = provider.search("x", max_results=1)
        self.assertEqual(result.records[0].title, "结构化结果")
        self.assertEqual(result.records[0].url, "https://example.com/a")

    def test_extract_returns_page_record_with_full_text(self):
        body = "# 一名男生的高中暗恋故事\n\n" + "在高中时我暗恋过一个女孩，她坐在我前面两排。" * 12
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=body)])
        provider = make_anysearch(runner=runner)
        result = provider.extract("https://zhuanlan.zhihu.com/p/593267924")
        self.assertEqual(result.status, "ready")
        record = result.records[0]
        self.assertEqual(record.title, "一名男生的高中暗恋故事")
        self.assertEqual(record.record_kind, "page")
        self.assertIn("她坐在我前面两排", record.full_text)
        self.assertGreater(len(record.full_text), 200)
        self.assertEqual(record.coverage.level, "complete")
        self.assertEqual(record.coverage.content_pages, 1)

    def test_blocked_extract_is_marked_partial_not_complete(self):
        """真实缺陷：知乎/豆瓣返回的 54 字拦截页曾被标成 complete。"""
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout="请登录后继续访问\n")])
        provider = make_anysearch(runner=runner)
        result = provider.extract("https://zhuanlan.zhihu.com/p/630276243")
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.coverage.level, "partial")
        self.assertIn("拦截", result.coverage.notes)
        self.assertTrue(result.gaps)
        self.assertTrue(result.records[0].extra["blocked_suspected"])

    def test_long_but_blocked_extract_is_also_flagged(self):
        body = "安全验证" + "x" * 2000
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=body)])
        provider = make_anysearch(runner=runner)
        result = provider.extract("https://www.zhihu.com/question/1")
        self.assertEqual(result.coverage.level, "partial")

    def test_extract_without_heading_falls_back_to_the_url(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout="没有标题的正文")])
        provider = make_anysearch(runner=runner)
        result = provider.extract("https://example.com/plain")
        self.assertEqual(result.records[0].title, "https://example.com/plain")
        self.assertEqual(result.records[0].extra["title_source"], "url")


class AnySearchFailureTests(unittest.TestCase):
    def test_timeout_is_degraded(self):
        def raising(argv, kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

        runner = ScriptedRunner([raising])
        provider = make_anysearch(runner=runner, timeout=5.0)
        result = provider.search("x", max_results=1)
        self.assertEqual(result.status, "degraded")
        self.assertIn("timeout", result.error)
        self.assertIn("5", result.error)
        self.assertEqual(result.records, ())

    def test_nonzero_exit_degrades_and_redacts_stderr(self):
        runner = ScriptedRunner(
            [
                CompletedProcessLike(
                    returncode=1,
                    stdout="",
                    stderr=f"API Error: invalid key sk-live-this-key-must-never-leak-123456 and token={FAKE_KEY}",
                )
            ]
        )
        provider = make_anysearch(runner=runner)
        result = provider.search("x", max_results=1)
        self.assertEqual(result.status, "degraded")
        self.assertIn("<redacted>", result.error)
        self.assertNotIn(FAKE_KEY, result.error)
        self.assertNotIn("sk-live-this-key-must-never-leak", result.error)

    def test_missing_cli_is_unavailable(self):
        def raising(argv, kwargs):
            raise FileNotFoundError("node")

        runner = ScriptedRunner([raising])
        provider = make_anysearch(runner=runner)
        result = provider.search("x", max_results=1)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("CLI", result.error)

    def test_empty_output_degrades(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout="  \n ")])
        provider = make_anysearch(runner=runner)
        result = provider.search("x", max_results=1)
        self.assertEqual(result.status, "degraded")
        self.assertIn("空输出", result.error)

    def test_oversized_output_is_truncated_and_flagged(self):
        runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout="x" * 5000)])
        provider = make_anysearch(runner=runner, max_output=100)
        result = provider.search("x", max_results=1)
        self.assertIn("截断", result.coverage.notes)


class AnySearchDiscoveryTests(unittest.TestCase):
    def write_skill_dir(self, directory: Path, *, with_key: bool) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "runtime.conf").write_text(
            "# AnySearch Runtime Configuration\n"
            "Runtime: Node.js\n"
            f"Command: node {directory}/scripts/anysearch_cli.js\n",
            encoding="utf-8",
        )
        if with_key:
            (directory / ".env").write_text(f"ANYSEARCH_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        return directory

    def test_doctor_is_ready_when_cli_and_key_are_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self.write_skill_dir(Path(tmp) / "anysearch", with_key=True)
            provider = AnySearchProvider(skill_dir=skill_dir, env={}, runner=ScriptedRunner([]))
            status = provider.doctor()
            self.assertEqual(status.status, "ready")
            self.assertEqual(status.capabilities, ("search", "extract"))
            self.assertNotIn(FAKE_KEY, json.dumps(status.to_dict(), ensure_ascii=False))

    def test_doctor_is_degraded_on_anonymous_access_without_a_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self.write_skill_dir(Path(tmp) / "anysearch", with_key=False)
            provider = AnySearchProvider(skill_dir=skill_dir, env={}, runner=ScriptedRunner([]))
            status = provider.doctor()
            self.assertEqual(status.status, "degraded")
            self.assertIn("匿名", status.detail)

    def test_doctor_is_unavailable_without_a_resolvable_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = AnySearchProvider(skill_dir=Path(tmp) / "missing", env={}, runner=ScriptedRunner([]))
            status = provider.doctor()
            self.assertEqual(status.status, "unavailable")
            self.assertEqual(status.capabilities, ())

    def test_command_resolution_prefers_explicit_then_env_then_runtime_conf(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = self.write_skill_dir(Path(tmp) / "anysearch", with_key=False)
            explicit = make_anysearch(command=["python3", "cli.py"], skill_dir=skill_dir)
            self.assertEqual(explicit.resolved_command()[0], ["python3", "cli.py"])
            from_env = AnySearchProvider(
                skill_dir=skill_dir, env={"ANYSEARCH_COMMAND": "node /env/cli.js"}, runner=ScriptedRunner([])
            )
            self.assertEqual(from_env.resolved_command()[0], ["node", "/env/cli.js"])
            from_conf = AnySearchProvider(skill_dir=skill_dir, env={}, runner=ScriptedRunner([]))
            self.assertEqual(from_conf.resolved_command()[0], ["node", f"{skill_dir}/scripts/anysearch_cli.js"])


class JustOneManifestTests(unittest.TestCase):
    def setUp(self):
        self.bridge = JustOneBridge(clock=lambda: "2026-09-24T12:00:00Z")

    def test_bilibili_manifest_declares_discovery_budget_and_handoff(self):
        manifest = self.bridge.create_manifest(
            "bilibili", "校园 爱情 故事", comments=True, limit=20, output_dir="outputs/run"
        )
        self.assertEqual(manifest["manifest_version"], "1.0.0")
        self.assertEqual(manifest["platform"], "bilibili")
        self.assertEqual(manifest["created_at"], "2026-09-24T12:00:00Z")
        self.assertTrue(manifest["endpoint_discovery"]["required"])
        self.assertIsNone(manifest["endpoint_discovery"]["endpoint_id"])
        self.assertTrue(any("发现" in step for step in manifest["endpoint_discovery"]["steps"]))
        self.assertTrue(any("schema" in step for step in manifest["endpoint_discovery"]["steps"]))
        self.assertTrue(manifest["requires_comments"])
        self.assertTrue(manifest["call_budget"]["requires_user_approval"])
        self.assertTrue(manifest["call_budget"]["paid"])
        self.assertGreaterEqual(manifest["call_budget"]["max_calls"], 2)
        self.assertEqual(manifest["pagination"]["max_items"], 20)
        self.assertIn("ingest-justone bilibili", manifest["output_contract"]["ingest_command"])
        self.assertTrue(manifest["safety"]["read_only"])
        self.assertFalse(manifest["safety"]["browser_automation"])
        self.assertEqual(manifest["requires_mcp"]["server"], "justoneapi")

    def test_zhihu_manifest_is_platform_specific(self):
        manifest = self.bridge.create_manifest("zhihu", "学生时代的爱情故事", comments=False)
        self.assertEqual(manifest["platform"], "zhihu")
        self.assertFalse(manifest["requires_comments"])
        self.assertIn("zhihu", manifest["output_contract"]["ingest_command"])

    def test_manifest_never_hardcodes_an_endpoint_id(self):
        payload = json.dumps(self.bridge.create_manifest("bilibili", "x", comments=True), ensure_ascii=False)
        self.assertNotRegex(payload, r"endpoint_id\"\s*:\s*\"?\d")

    def test_manifest_rejects_unknown_platform_and_bad_limits(self):
        with self.assertRaises(ValueError):
            self.bridge.create_manifest("douyin", "x")
        with self.assertRaises(ValueError):
            self.bridge.create_manifest("bilibili", "")
        with self.assertRaises(ValueError):
            self.bridge.create_manifest("bilibili", "x", limit=0)
        with self.assertRaises(ValueError):
            self.bridge.create_manifest("bilibili", "x", page_limit=0)

    def test_doctor_explains_the_mcp_handoff(self):
        status = self.bridge.doctor()
        self.assertEqual(status.status, "unavailable")
        self.assertIn("MCP", status.detail)
        self.assertEqual(sorted(status.capabilities), ["ingest", "manifest"])


class JustOneIngestTests(unittest.TestCase):
    def setUp(self):
        self.bridge = JustOneBridge(clock=lambda: "2026-09-24T12:00:00Z")

    def test_bilibili_response_maps_video_and_comment_thread(self):
        result = self.bridge.ingest_response(load_fixture("justone_bilibili.json"), "bilibili")
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.records), 1)
        video = result.records[0]
        self.assertEqual(video.source_id, "bilibili:BV1wN4y1D7gz")
        self.assertEqual(video.record_kind, "video")
        self.assertEqual(video.source_type, "video")
        self.assertEqual(video.title, "真实的高中校园暗恋故事！你是否也曾经有过一个爱而不得的人")
        self.assertEqual(video.url, "https://www.bilibili.com/video/BV1wN4y1D7gz")
        self.assertEqual(video.author_display_name, "校园故事计划")
        self.assertEqual(video.published_at, "2023-11-09T02:40:00Z")
        self.assertEqual(video.engagement["views"], 128000)
        self.assertEqual(video.engagement["replies"], 456)
        self.assertEqual(video.media, ("https://i0.hdslb.com/bfs/archive/example-cover.jpg",))
        self.assertIn("暗恋、错过与释怀", video.full_text)
        self.assertEqual(video.coverage.level, "complete")
        self.assertEqual(len(result.comments), 2)
        top = result.comments[0]
        self.assertEqual(top.source_id, "bilibili:1400000001")
        self.assertEqual(top.parent_id, "bilibili:BV1wN4y1D7gz")
        self.assertEqual(top.thread_id, "bilibili:BV1wN4y1D7gz")
        self.assertEqual(top.author_display_name, "观众甲")
        self.assertEqual(top.engagement["likes"], 231)
        self.assertEqual(top.url, "https://www.bilibili.com/video/BV1wN4y1D7gz#reply1400000001")
        reply = result.comments[1]
        self.assertEqual(reply.source_id, "bilibili:1400000002")
        self.assertEqual(reply.parent_id, "bilibili:1400000001")
        self.assertEqual(len(result.raw_payloads), 1)

    def test_real_bilibili_search_envelope_is_mapped(self):
        """真实 MCP 信封：data.data.result，字段名与样例完全不同。"""
        payload = load_fixture("justone_bilibili_search.json")
        result = self.bridge.ingest_response(payload, "bilibili")
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.records), 2)
        video = result.records[0]
        self.assertEqual(video.source_id, "bilibili:BV1RHzzYzELm")
        self.assertEqual(
            video.title,
            "（全文已完结）暗恋，我暗恋一个人三年，从高一到高三还挺长时间的，暗恋好苦哦",
        )
        self.assertNotIn("<em", video.title)
        self.assertEqual(video.author_display_name, "资深完结少女")
        self.assertEqual(video.url, "https://www.bilibili.com/video/av113566099315027")
        self.assertEqual(video.published_at, "2024-11-29T12:05:47Z")
        self.assertIn("暗恋好苦哦", video.full_text)
        self.assertEqual(video.engagement["views"], 64112)
        self.assertEqual(video.engagement["replies"], 85)
        self.assertEqual(video.engagement["likes"], 1183)
        self.assertEqual(video.engagement["favorites"], 994)
        self.assertEqual(video.engagement["duration_seconds"], 2194)
        self.assertEqual(video.media, ("https://i0.hdslb.com/bfs/archive/61111fb029b6c02826a3e00081c4cac9140db0e2.jpg",))
        self.assertEqual(video.extra["bvid"], "BV1RHzzYzELm")
        self.assertEqual(video.extra["text_source_field"], "description")

    def test_real_envelope_keeps_the_next_step_for_pagination(self):
        payload = load_fixture("justone_bilibili_search.json")
        self.assertEqual(payload["next_step"]["params"]["page"], 2)
        result = self.bridge.ingest_response(payload, "bilibili")
        self.assertEqual(result.raw_payloads[0].name, "raw/justone-bilibili-1.json")

    def test_zhihu_response_maps_answer_and_comments(self):
        result = self.bridge.ingest_response(load_fixture("justone_zhihu.json"), "zhihu")
        self.assertEqual(result.status, "ready")
        answer = result.records[0]
        self.assertEqual(answer.source_id, "zhihu:987654321")
        self.assertEqual(answer.record_kind, "answer")
        self.assertEqual(answer.source_type, "community")
        self.assertEqual(answer.title, "学生时代的爱情故事有哪些让你印象深刻的细节？")
        self.assertEqual(answer.thread_id, "zhihu:12345678")
        self.assertEqual(answer.parent_id, "zhihu:12345678")
        self.assertEqual(answer.url, "https://www.zhihu.com/question/12345678/answer/987654321")
        self.assertEqual(answer.author_display_name, "沈某")
        self.assertEqual(answer.published_at, "2026-09-22T17:18:36Z")
        self.assertEqual(answer.engagement["votes"], 3210)
        self.assertNotIn("<p>", answer.full_text)
        self.assertIn("后来分班", answer.full_text)
        self.assertEqual(len(result.comments), 2)
        self.assertEqual(result.comments[0].source_id, "zhihu:555000111")
        self.assertEqual(result.comments[0].parent_id, "zhihu:987654321")
        self.assertEqual(result.comments[1].parent_id, "zhihu:555000111")
        self.assertEqual(
            result.comments[1].url,
            "https://www.zhihu.com/question/12345678/answer/987654321/comment/555000112",
        )

    def test_real_bilibili_comment_payload_maps_to_comment_records(self):
        payload = load_fixture("justone_bilibili_comments.json")
        result = self.bridge.ingest_response(
            payload,
            "bilibili",
            kind="comments",
            thread_source_id="bilibili:BV1RHzzYzELm",
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.records, ())
        self.assertEqual(len(result.comments), 4)  # 3 条一层评论 + 1 条楼中楼
        top = result.comments[0]
        self.assertEqual(top.source_id, "bilibili:317227061264")
        self.assertEqual(top.thread_id, "bilibili:BV1RHzzYzELm")
        self.assertEqual(top.parent_id, "bilibili:BV1RHzzYzELm")
        self.assertEqual(top.author_display_name, "骨架太有操作")
        self.assertIn("哪有什么双向暗恋", top.full_text)
        self.assertEqual(top.engagement["likes"], 1)
        self.assertTrue(top.url.endswith("#reply317227061264"))
        self.assertEqual(result.coverage.level, "complete")
        self.assertEqual(result.coverage.comment_pages, 1)
        self.assertTrue(result.coverage.includes_nested_comments)

    def test_comment_payload_for_a_different_thread_is_empty_not_fabricated(self):
        payload = {"data": {"data": {"replies": []}}}
        result = self.bridge.ingest_response(payload, "bilibili", kind="comments")
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.comments, ())

    def test_real_zhihu_search_envelope_is_mapped(self):
        payload = load_fixture("justone_zhihu_search.json")
        result = self.bridge.ingest_response(payload, "zhihu")
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.records), 2)
        answer = result.records[1]
        self.assertEqual(answer.source_id, "zhihu:364892125")
        self.assertEqual(answer.record_kind, "answer")
        self.assertEqual(answer.title, "高中恋爱有什么搞笑的事？")
        self.assertEqual(answer.url, "https://www.zhihu.com/question/271524836/answer/364892125")
        self.assertEqual(answer.author_display_name, "韵韵")
        self.assertEqual(answer.published_at, "2018-04-12T05:05:59Z")
        self.assertIn("偷偷把早餐塞他抽屉", answer.full_text)
        self.assertNotIn("<p", answer.full_text)
        self.assertNotIn("<em", answer.excerpt)
        self.assertEqual(answer.engagement["votes"], 18108)
        self.assertEqual(answer.engagement["comments"], 676)
        self.assertEqual(answer.engagement["views"], 80275720)
        self.assertEqual(answer.extra["question_id"], "271524836")
        self.assertEqual(answer.extra["search_index"], 1)
        self.assertEqual(answer.thread_id, "zhihu:271524836")

    def test_api_zhihu_url_is_rewritten_to_a_public_url(self):
        payload = load_fixture("justone_zhihu_search.json")
        result = self.bridge.ingest_response(payload, "zhihu")
        for record in result.records:
            self.assertNotIn("api.zhihu.com", record.url)
            self.assertTrue(record.url.startswith("https://www.zhihu.com/"))

    def test_real_xiaohongshu_search_envelope_is_mapped(self):
        payload = load_fixture("justone_xiaohongshu_search.json")
        result = self.bridge.ingest_response(payload, "xiaohongshu")
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.records), 2)
        note = result.records[0]
        self.assertEqual(note.source_id, "xiaohongshu:68213c89000000002202de57")
        self.assertEqual(note.platform, "xiaohongshu")
        self.assertEqual(note.source_type, "community")
        self.assertEqual(note.record_kind, "post")
        self.assertEqual(note.author_display_name, "微微奇思妙想记")
        self.assertIn("上过表白墙", note.full_text)
        self.assertEqual(note.engagement["likes"], 1897)
        self.assertEqual(note.engagement["comments"], 170)
        self.assertEqual(note.published_at, "2025-05-12T00:10:49Z")
        self.assertTrue(note.url.startswith("https://www.xiaohongshu.com/explore/68213c89000000002202de57?xsec_token="))
        self.assertEqual(len(note.media), 3)
        self.assertTrue(note.media[0].startswith("https://sns-"))
        self.assertEqual(note.extra["text_source_field"], "desc")

    def test_items_with_only_images_are_skipped_not_fatal(self):
        """真实缺陷：无标题无正文的笔记曾让整轮采集中断。"""
        payload = {
            "code": 0,
            "data": {"items": [
                {"model_type": "note", "note": {"id": "abc123", "title": "", "desc": "", "images_list": [{"url": "https://x/1.jpg"}]}},
                {"model_type": "note", "note": {"id": "def456", "title": "<em>表白墙</em>故事", "desc": "高中表白墙上的暗恋。", "user": {"nickname": "某人"}}},
            ]},
        }
        result = self.bridge.ingest_response(payload, "xiaohongshu")
        self.assertEqual(result.status, "degraded")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].source_id, "xiaohongshu:def456")
        self.assertEqual(result.records[0].title, "表白墙故事")
        self.assertEqual(result.records[0].full_text, "高中表白墙上的暗恋。")

    def test_mcp_envelope_is_unwrapped(self):
        inner = json.dumps(load_fixture("justone_bilibili.json"), ensure_ascii=False)
        envelope = {"content": [{"type": "text", "text": inner}], "isError": False}
        result = self.bridge.ingest_response(envelope, "bilibili")
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.records[0].source_id, "bilibili:BV1wN4y1D7gz")

    def test_generic_fallback_maps_unknown_shapes(self):
        payload = {
            "items": [
                {
                    "id": "note-1",
                    "title": "一条来历不明的公开内容",
                    "content": "正文内容",
                    "url": "https://example.com/note-1",
                    "author": {"name": "某人"},
                    "created_time": 1790097516,
                }
            ]
        }
        result = self.bridge.ingest_response(payload, "other", raw_ref="raw/justone-other-1.json")
        self.assertEqual(result.status, "ready")
        record = result.records[0]
        self.assertEqual(record.source_id, "other:note-1")
        self.assertEqual(record.source_type, "other")
        self.assertEqual(record.author_display_name, "某人")
        self.assertEqual(record.url, "https://example.com/note-1")

    def test_empty_payload_degrades(self):
        result = self.bridge.ingest_response({}, "bilibili")
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.records, ())
        self.assertIn("未解析", result.error)

    def test_payload_text_is_accepted_and_kept_raw(self):
        raw = read_fixture_bytes("justone_zhihu.json").decode("utf-8")
        result = self.bridge.ingest_response(raw, "zhihu", raw_ref="raw/justone-zhihu-1.json")
        self.assertEqual(result.records[0].source_id, "zhihu:987654321")
        self.assertEqual(result.raw_payloads[0].name, "raw/justone-zhihu-1.json")

    def test_malformed_json_text_degrades(self):
        result = self.bridge.ingest_response("{not json", "zhihu")
        self.assertEqual(result.status, "degraded")
        self.assertIn("JSON", result.error)


class JustOneEndpointSchemaTests(unittest.TestCase):
    def setUp(self):
        self.bridge = JustOneBridge(clock=lambda: "2026-09-24T12:00:00Z")

    def test_discovered_catalog_is_normalized_and_validated(self):
        report = self.bridge.ingest_endpoint_schema(load_fixture("justone_endpoint_catalog.json"))
        self.assertEqual(report["schema_version"], "1.0.0")
        self.assertTrue(report["validation"]["ok"])
        self.assertEqual(len(report["endpoints"]), 2)
        first = report["endpoints"][0]
        self.assertTrue(first["endpoint_id"])
        self.assertEqual(sorted(first["parameters"]), ["keyword", "page", "page_size"])
        self.assertEqual(first["required_parameters"], ["keyword", "page"])

    def test_schema_validation_reports_concrete_problems(self):
        report = self.bridge.ingest_endpoint_schema({"tools": [{"description": "没有名字"}]})
        self.assertFalse(report["validation"]["ok"])
        self.assertTrue(report["validation"]["problems"])
        self.assertIn("endpoint_id", " ".join(report["validation"]["problems"]))

    def test_single_endpoint_shape_is_accepted(self):
        payload = {
            "endpoint_id": "discovered-at-runtime",
            "name": "知乎回答评论",
            "parameters": {"type": "object", "properties": {"answer_id": {"type": "string"}}, "required": ["answer_id"]},
        }
        report = self.bridge.ingest_endpoint_schema(payload, platform="zhihu")
        self.assertTrue(report["validation"]["ok"])
        self.assertEqual(report["platform"], "zhihu")
        self.assertEqual(report["endpoints"][0]["endpoint_id"], "discovered-at-runtime")


if __name__ == "__main__":
    unittest.main()
