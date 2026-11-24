"""Tests for caching, output safety, search-agent export and the pipeline."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from source_adapter.cache import FileCache, atomic_write_json, safe_join
from source_adapter.export import (
    SchemaError,
    build_comment_tree,
    build_search_agent_input,
    export_search_agent_input,
    validate_search_agent_input,
)
from source_adapter.http import HttpClient
from source_adapter.models import CommentRecord, Coverage, SourceRecord
from source_adapter.pipeline import ResearchPipeline
from source_adapter.providers.anysearch import AnySearchProvider
from source_adapter.providers.justone import JustOneBridge
from source_adapter.providers.tieba import AiotiebaProvider, TiebaPublicProvider

try:  # `python3 -m unittest tests.test_pipeline`
    from tests.helpers import FakeClock, FakeTransport, json_response, load_fixture
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import FakeClock, FakeTransport, json_response, load_fixture


def http_with(transport, cache=None, provider="tieba", **overrides):
    clock = FakeClock()
    options = {
        "transport": transport,
        "provider": provider,
        "spacing": 0.0,
        "sleeper": clock.sleep,
        "monotonic": clock.monotonic,
        "cache": cache,
    }
    options.update(overrides)
    return HttpClient(**options)


class CacheTests(unittest.TestCase):
    def test_round_trip_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            key = cache.key("tieba", "http", {"url": "https://example.com", "max_bytes": 10})
            self.assertFalse(cache.has(key))
            cache.put(key, b'{"a": 1}', {"provider": "tieba", "url": "https://example.com"})
            self.assertTrue(cache.has(key))
            entry = cache.get(key)
            self.assertEqual(entry.body, b'{"a": 1}')
            self.assertEqual(entry.meta["provider"], "tieba")
            self.assertEqual(cache.stats()["entries"], 1)

    def test_key_is_stable_and_param_order_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            first = cache.key("tieba", "http", {"b": "2", "a": "1"})
            second = cache.key("tieba", "http", {"a": "1", "b": "2"})
            third = cache.key("zhihu", "http", {"a": "1", "b": "2"})
            self.assertEqual(first, second)
            self.assertNotEqual(first, third)
            self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_put_is_atomic_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            key = cache.key("tieba", "http", {"url": "https://example.com"})
            cache.put(key, b"payload", {})
            leftovers = [p.name for p in cache.root.rglob("*") if ".tmp" in p.name]
            self.assertEqual(leftovers, [])

    def test_missing_entry_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            self.assertIsNone(cache.get("0" * 64))


class SafePathTests(unittest.TestCase):
    def test_rejects_traversal_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in ("../escape.json", "raw/../../escape.json", "/etc/passwd", "raw/\x00bad"):
                with self.assertRaises(ValueError):
                    safe_join(root, bad)
            self.assertEqual(safe_join(root, "raw", "ok.json").parent, root / "raw")

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "link").symlink_to(outside)
            with self.assertRaises(ValueError):
                safe_join(root, "link", "escape.json")

    def test_atomic_write_creates_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b" / "c.json"
            atomic_write_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})


class HttpClientCacheTests(unittest.TestCase):
    def test_second_request_is_served_from_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            transport = FakeTransport([json_response({"no": 0})])
            client = http_with(transport, cache=cache)
            first = client.get_json("https://tieba.baidu.com/mo/q/search/thread", params={"word": "x"})
            second = client.get_json("https://tieba.baidu.com/mo/q/search/thread", params={"word": "x"})
            self.assertTrue(first.ok)
            self.assertFalse(first.from_cache)
            self.assertTrue(second.ok)
            self.assertTrue(second.from_cache)
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(second.data, {"no": 0})

    def test_offline_miss_reports_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            client = http_with(FakeTransport([]), cache=cache, offline=True)
            result = client.get_json("https://example.test/missing")
            self.assertFalse(result.ok)
            self.assertIn("offline", result.error)
            self.assertEqual(result.attempts, 0)

    def test_feature_flag_offline_without_cache_is_refused(self):
        client = http_with(FakeTransport([]), cache=None, offline=True)
        result = client.get_json("https://example.test/missing")
        self.assertFalse(result.ok)
        self.assertIn("offline", result.error)

    def test_anysearch_cli_results_are_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            from tests.helpers import CompletedProcessLike, ScriptedRunner

            markdown = (
                "## Search Results (1 results, 5ms)\n\n"
                "### 1. 缓存命中测试\n- **URL**: https://example.com/cached\n- 摘要\n"
            )
            runner = ScriptedRunner([CompletedProcessLike(returncode=0, stdout=markdown)])
            provider = AnySearchProvider(
                command=["node", "/opt/anysearch/cli.js"],
                env={},
                runner=runner,
                cache=cache,
            )
            first = provider.search("缓存", max_results=1)
            second = provider.search("缓存", max_results=1)
            self.assertEqual(first.status, "ready")
            self.assertEqual(second.status, "ready")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(second.records[0].title, "缓存命中测试")


class PipelineTiebaTests(unittest.TestCase):
    def build(self, responses, *, pages=1, tmp=None, **options):
        cache = FileCache(Path(tmp) / "cache")
        transport = FakeTransport(responses)
        provider = TiebaPublicProvider(http=http_with(transport, cache=cache))
        pipeline = ResearchPipeline(
            Path(tmp) / "out",
            cache=cache,
            tieba=provider,
            clock=lambda: "2026-09-24T12:00:00Z",
            run_id_factory=lambda label: "run-1",
        )
        return pipeline, transport

    def test_single_page_run_records_termination_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.build([json_response(load_fixture("tieba_search.json"))], tmp=tmp)
            result = pipeline.tieba_search("高中 暗恋", pages=1)
            self.assertEqual(len(result.records), 3)
            self.assertEqual(result.pages, 1)
            self.assertEqual(result.termination, "page_limit")
            self.assertEqual(result.coverage.level, "partial")
            self.assertTrue(any("上限" in gap for gap in result.gaps))
            self.assertEqual(result.status, "degraded")

    def test_two_pages_deduplicate_and_exhaust(self):
        page_one = load_fixture("tieba_search.json")
        page_two = load_fixture("tieba_search_page2.json")
        page_two["data"]["has_more"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.build(
                [json_response(page_one), json_response(page_two)], tmp=tmp
            )
            result = pipeline.tieba_search("高中 暗恋", pages=2)
            self.assertEqual(result.pages, 2)
            self.assertEqual(result.termination, "exhausted")
            self.assertEqual(len(result.records), 5)
            self.assertEqual(result.coverage.level, "partial")
            self.assertEqual(result.gaps, ())

    def test_repeated_page_stops_pagination_without_duplicates(self):
        page_one = load_fixture("tieba_search.json")
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, transport = self.build(
                [json_response(page_one), json_response(page_one), json_response(page_one)],
                tmp=tmp,
            )
            result = pipeline.tieba_search("高中 暗恋", pages=3)
            self.assertEqual(result.termination, "repeated_page")
            self.assertEqual(result.pages, 2)
            self.assertEqual(len(result.records), 3)
            self.assertEqual(transport.call_count, 2)
            self.assertTrue(any("重复页" in gap for gap in result.gaps))

    def test_cross_page_duplicates_merge_and_keep_both_raw_refs(self):
        page_one = load_fixture("tieba_search.json")
        page_two = load_fixture("tieba_search.json")
        page_two["data"]["has_more"] = 0
        page_two["data"]["post_list"] = [dict(page_two["data"]["post_list"][0])]
        page_two["data"]["post_list"][0]["content"] = "更完整的正文" * 60
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.build([json_response(page_one), json_response(page_two)], tmp=tmp)
            result = pipeline.tieba_search("高中 暗恋", pages=2)
            self.assertEqual(result.termination, "repeated_page")
            target = [record for record in result.records if record.source_id == "tieba:11045552764"][0]
            self.assertEqual(len(target.excerpt), 201)
            self.assertIn("tieba-public-search-p1.json", target.raw_ref)
            self.assertIn("tieba-public-search-p2.json", target.raw_ref)

    def test_unavailable_provider_produces_a_failed_run(self):
        from tests.helpers import bytes_response

        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.build([bytes_response(b"", status=503) for _ in range(4)], tmp=tmp)
            result = pipeline.tieba_search("高中 暗恋", pages=1)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.records, ())
            self.assertEqual(result.termination, "error")
            self.assertTrue(result.failures)
            self.assertEqual(result.coverage.level, "failed")

    def test_second_identical_run_is_served_from_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, transport = self.build([json_response(load_fixture("tieba_search.json"))], tmp=tmp)
            pipeline.tieba_search("高中 暗恋", pages=1)
            pipeline.tieba_search("高中 暗恋", pages=1)
            self.assertEqual(transport.call_count, 1)


class OutputTests(unittest.TestCase):
    def test_writes_every_documented_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            bridge = JustOneBridge(clock=lambda: "2026-09-24T12:00:00Z")
            pipeline = ResearchPipeline(
                Path(tmp) / "out",
                cache=cache,
                justone=bridge,
                clock=lambda: "2026-09-24T12:00:00Z",
                run_id_factory=lambda label: "run-bili",
            )
            result = pipeline.ingest_justone("bilibili", load_fixture("justone_bilibili.json"))
            result = pipeline.write_outputs(result)
            run_dir = result.run_dir
            self.assertTrue((run_dir / "raw" / "justone-bilibili-1.json").is_file())
            self.assertTrue((run_dir / "records.jsonl").is_file())
            thread_file = run_dir / "threads" / "bilibili-BV1wN4y1D7gz.json"
            self.assertTrue(thread_file.is_file())
            self.assertTrue((run_dir / "search-agent-input.json").is_file())
            self.assertTrue((run_dir / "run-report.json").is_file())

            lines = (run_dir / "records.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)  # 1 video + 2 comments
            payloads = [json.loads(line) for line in lines]
            self.assertEqual(payloads[0]["record_kind"], "video")
            self.assertEqual(payloads[1]["record_kind"], "comment")

            thread = json.loads(thread_file.read_text(encoding="utf-8"))
            self.assertEqual(thread["thread_id"], "bilibili:BV1wN4y1D7gz")
            self.assertEqual(thread["comment_count"], 2)
            self.assertEqual(len(thread["comment_tree"]), 1)
            self.assertEqual(thread["comment_tree"][0]["record"]["source_id"], "bilibili:1400000001")
            self.assertEqual(
                thread["comment_tree"][0]["children"][0]["record"]["source_id"], "bilibili:1400000002"
            )

            exported = json.loads((run_dir / "search-agent-input.json").read_text(encoding="utf-8"))
            validate_search_agent_input(exported)
            self.assertEqual(exported["schema_version"], "1.0.0")
            self.assertEqual(len(exported["results"]), 1)
            self.assertEqual(exported["results"][0]["source_type"], "video")
            self.assertEqual(exported["results"][0]["metadata"]["comment_count"], 2)
            self.assertEqual(exported["results"][0]["metadata"]["coverage"]["level"], "complete")

            report = json.loads((run_dir / "run-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["run_id"], "run-bili")
            self.assertEqual(report["records"], 1)
            self.assertEqual(report["comments"], 2)
            self.assertEqual(report["threads"], 1)
            self.assertEqual(report["cost"]["paid_calls"], 0)
            self.assertEqual(report["cost"]["estimated_cost"], "0")
            self.assertTrue(report["exclusions"])
            self.assertEqual(report["providers"][0]["provider"], "justone")

    def test_records_jsonl_is_valid_json_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = ResearchPipeline(
                Path(tmp) / "out", justone=JustOneBridge(), run_id_factory=lambda label: "run-x"
            )
            result = pipeline.write_outputs(
                pipeline.ingest_justone("zhihu", load_fixture("justone_zhihu.json"))
            )
            for line in (result.run_dir / "records.jsonl").read_text(encoding="utf-8").strip().splitlines():
                self.assertIsInstance(json.loads(line), dict)


class SummaryShapeTests(unittest.TestCase):
    def test_summary_carries_cost_and_artifacts(self):
        from source_adapter.models import Coverage
        from source_adapter.pipeline import RunResult

        run = RunResult(
            run_id="r1",
            run_dir=Path("/tmp/r1"),
            mode="justone-mcp:bilibili",
            query="青梅竹马",
            coverage=Coverage(level="complete"),
            paid_calls=2,
            estimated_cost="0.2500 CNY",
            cost={"spent": 0.25, "currency": "CNY"},
            written={"records.jsonl": "/tmp/r1/records.jsonl"},
        )
        summary = run.summary()
        self.assertEqual(summary["paid_calls"], 2)
        self.assertEqual(summary["estimated_cost"], "0.2500 CNY")
        self.assertEqual(summary["cost"]["spent"], 0.25)
        self.assertEqual(summary["written"], ["records.jsonl"])
        self.assertEqual(json.loads(json.dumps(summary))["run_id"], "r1")


class CommentTreeTests(unittest.TestCase):
    def make_comment(self, source_id, parent_id, text="内容"):
        return CommentRecord(
            source_id=source_id,
            platform="bilibili",
            thread_id="bilibili:BV1",
            parent_id=parent_id,
            full_text=text,
        )

    def test_nests_children_under_parents(self):
        top = self.make_comment("bilibili:1", "bilibili:BV1")
        reply = self.make_comment("bilibili:2", "bilibili:1")
        orphan = self.make_comment("bilibili:3", "bilibili:missing")
        tree = build_comment_tree([top, reply, orphan], "bilibili:BV1")
        self.assertEqual([node["record"]["source_id"] for node in tree], ["bilibili:1", "bilibili:3"])
        self.assertEqual(tree[0]["children"][0]["record"]["source_id"], "bilibili:2")
        self.assertEqual(tree[1]["children"], [])

    def test_survives_a_parent_cycle(self):
        first = self.make_comment("bilibili:1", "bilibili:2")
        second = self.make_comment("bilibili:2", "bilibili:1")
        tree = build_comment_tree([first, second], "bilibili:BV1")
        self.assertEqual(len(tree), 2)
        for node in tree:
            self.assertEqual(node["children"], [])


class ExportTests(unittest.TestCase):
    def make_record(self, **overrides):
        base = {
            "source_id": "tieba:11045552764",
            "platform": "tieba",
            "record_kind": "thread",
            "source_type": "community",
            "title": "想了想还是来这里记录一下高中两年的暗恋",
            "url": "https://tieba.baidu.com/p/11045552764",
            "author_display_name": "空白231",
            "published_at": "2026-09-22T17:18:36Z",
            "retrieved_at": "2026-09-24T12:00:00Z",
            "excerpt": "起因是今天他第一次点赞了我的朋友圈",
            "collection_method": "tieba_public_search_json",
            "coverage": Coverage(level="partial", discovered_pages=1),
            "raw_ref": "raw/tieba-public-search-p1.json",
        }
        base.update(overrides)
        return SourceRecord(**base)

    def test_builds_a_schema_compatible_payload(self):
        payload = build_search_agent_input(
            "高中 暗恋",
            [self.make_record()],
            comments=[CommentRecord(source_id="tieba:1", platform="tieba", thread_id="tieba:11045552764", full_text="hi")],
            request_id="req-1",
        )
        validate_search_agent_input(payload)
        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["mode"], "discover")
        item = payload["results"][0]
        self.assertEqual(item["source_id"], "tieba:11045552764")
        self.assertEqual(item["author"], "空白231")
        self.assertEqual(item["metadata"]["platform"], "tieba")
        self.assertEqual(item["metadata"]["comment_count"], 1)
        self.assertEqual(item["metadata"]["raw_ref"], "raw/tieba-public-search-p1.json")

    def test_excerpt_falls_back_to_title_and_is_flagged(self):
        record = self.make_record(
            url="", excerpt="", full_text="", record_kind="thread", title="只有标题的记录"
        )
        payload = build_search_agent_input("query", [record], request_id="req-2")
        validate_search_agent_input(payload)
        self.assertEqual(payload["results"][0]["excerpt"], "只有标题的记录")
        self.assertEqual(payload["results"][0]["metadata"]["excerpt_source"], "title")

    def test_comment_only_runs_export_an_empty_result_list(self):
        payload = build_search_agent_input("query", [], comments=[], request_id="req-3")
        validate_search_agent_input(payload)
        self.assertEqual(payload["results"], [])

    def validator_rejects(self, payload, expectation):
        with self.assertRaises(SchemaError) as ctx:
            validate_search_agent_input(payload)
        self.assertIn(expectation, str(ctx.exception))

    def test_validator_rejects_contract_violations(self):
        good = build_search_agent_input("q", [self.make_record()], request_id="req")
        broken = json.loads(json.dumps(good))
        del broken["results"]
        self.validator_rejects(broken, "results")

        broken = json.loads(json.dumps(good))
        broken["schema_version"] = "2.0.0"
        self.validator_rejects(broken, "schema_version")

        broken = json.loads(json.dumps(good))
        broken["unexpected"] = True
        self.validator_rejects(broken, "unexpected")

        broken = json.loads(json.dumps(good))
        broken["results"][0]["source_type"] = "blog"
        self.validator_rejects(broken, "source_type")

        broken = json.loads(json.dumps(good))
        broken["results"][0]["unknown_field"] = "x"
        self.validator_rejects(broken, "unknown_field")

        broken = json.loads(json.dumps(good))
        broken["results"][0]["url"] = "not a url"
        self.validator_rejects(broken, "url")

        broken = json.loads(json.dumps(good))
        broken["results"][0]["published_at"] = "2026/09/22"
        self.validator_rejects(broken, "published_at")

        broken = json.loads(json.dumps(good))
        for key in ("excerpt", "full_text", "url"):
            broken["results"][0].pop(key, None)
        self.validator_rejects(broken, "excerpt")

    def test_export_writes_and_revalidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_search_agent_input("q", [self.make_record()], request_id="req")
            path = export_search_agent_input(Path(tmp) / "nested" / "search-agent-input.json", payload)
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["request_id"], "req")


class AiotiebaPipelineTests(unittest.TestCase):
    def test_missing_dependency_is_recorded_as_a_gap_not_a_crash(self):
        def failing_importer(name):
            raise ImportError(name)

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = ResearchPipeline(
                Path(tmp) / "out",
                aiotieba=AiotiebaProvider(importer=failing_importer),
                run_id_factory=lambda label: "run-t",
            )
            result = pipeline.tieba_thread("11045552764", pages=1, via="aiotieba")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.records, ())
            self.assertTrue(result.gaps)
            self.assertIn("aiotieba", result.failures[0])

    def test_public_thread_channel_is_used_by_default(self):
        class StubThreadProvider:
            name = "tieba-thread"

            def __init__(self):
                self.calls = []

            def doctor(self):
                from source_adapter.models import ProviderStatus

                return ProviderStatus(provider=self.name, status="ready")

            def fetch_thread(self, tid, **kwargs):
                from source_adapter.providers.base import ProviderResult

                self.calls.append((tid, kwargs))
                return ProviderResult(provider=self.name, status="unavailable", error="stub")

        stub = StubThreadProvider()
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = ResearchPipeline(
                Path(tmp) / "out", tieba_thread=stub, run_id_factory=lambda label: "run-pub"
            )
            pipeline.tieba_thread("11045552764", pages=2, include_nested=True)
        self.assertEqual(stub.calls[0][0], "11045552764")
        self.assertEqual(stub.calls[0][1]["page_limit"], 2)
        self.assertTrue(stub.calls[0][1]["include_nested"])


if __name__ == "__main__":
    unittest.main()
