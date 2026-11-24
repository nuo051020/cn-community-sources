"""Tests for the discovery-driven Just One API MCP provider.

These tests never touch the network: a stub MCP client replays the exact
envelope shapes returned by the real server (verified against
``https://mcp.justoneapi.com/mcp``), so candidate ranking, schema-driven
parameter building and cost accounting are all covered deterministically.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from source_adapter.mcp import McpCallResult, McpStats
from source_adapter.providers.justone import JustOneMcpProvider
from source_adapter.providers.base import ProviderResult

try:  # `python3 -m unittest tests.test_justone_mcp`
    from tests.helpers import load_fixture
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import load_fixture

BILIBILI_SEARCH_CANDIDATES = {
    "success": True,
    "data": {
        "endpoints": [
            {
                "endpoint_id": "bilibili.get_user_video_list_v2",
                "description": "检索由UID标识的哔哩哔哩用户发布的视频，用于浏览创作者的视频内容。",
            },
            {
                "endpoint_id": "bilibili.get_video_detail_v2",
                "description": "检索由BVID标识的哔哩哔哩视频详情。用于查找已知视频。",
            },
            {
                "endpoint_id": "bilibili.search_video_detail_v2",
                "description": "按关键词搜索视频并返回视频详情。",
            },
            {
                "endpoint_id": "bilibili.search_video_v2",
                "description": "按关键词搜索哔哩哔哩视频，支持基于页面的分页，并可按照综合排名排序。",
            },
            {
                "endpoint_id": "bilibili.get_video_danmu_v2",
                "description": "检索哔哩哔哩视频片段的单页弹幕评论。",
            },
        ]
    },
}

BILIBILI_SEARCH_CONTRACT = {
    "endpoint_id": "bilibili.search_video_v2",
    "path": "/api/bilibili/search-video/v2",
    "method": "GET",
    "pagination": {"type": "page", "params": ["page"]},
    "params": [
        {"name": "keyword", "api_name": "keyword", "required": True, "type": "string"},
        {"name": "page", "api_name": "page", "required": False, "type": "string"},
        {"name": "order", "api_name": "order", "required": False, "type": "string", "default": "general"},
    ],
}

BILIBILI_COMMENT_CANDIDATES = {
    "success": True,
    "data": {
        "endpoints": [
            {"endpoint_id": "bilibili.get_video_danmu_v2", "description": "检索视频片段的单页弹幕评论。"},
            {"endpoint_id": "bilibili.get_video_comment_v2", "description": "检索由AID标识的哔哩哔哩视频的评论，支持游标分页。"},
        ]
    },
}

BILIBILI_COMMENT_CONTRACT = {
    "endpoint_id": "bilibili.get_video_comment_v2",
    "pagination": {"type": "cursor", "params": ["cursor"]},
    "params": [
        {"name": "aid", "api_name": "aid", "required": True, "type": "string"},
        {"name": "cursor", "api_name": "cursor", "required": False, "type": "string"},
    ],
}

ZHIHU_SEARCH_CANDIDATES = {
    "success": True,
    "data": {
        "endpoints": [
            {"endpoint_id": "zhihu.get_user_info_v1", "description": "获取知乎用户的公开资料。"},
            {"endpoint_id": "zhihu.search_v1", "description": "按关键词搜索知乎，可选的搜索结果类型与偏移量控制。"},
        ]
    },
}

ZHIHU_SEARCH_CONTRACT = {
    "endpoint_id": "zhihu.search_v1",
    "pagination": {"type": "offset", "params": ["offset"]},
    "params": [
        {"name": "keyword", "api_name": "keyword", "required": True, "type": "string"},
        {"name": "offset", "api_name": "offset", "required": False, "type": "integer", "default": 0},
        {"name": "vertical", "api_name": "vertical", "required": False, "type": "string",
         "enum": ["answer", "article", "zvideo"]},
    ],
}

ZHIHU_COMMENT_CANDIDATES = {
    "success": True,
    "data": {
        "endpoints": [
            {"endpoint_id": "zhihu.get_answer_comments_v1", "description": "获取知乎回答的评论，支持最热或最新排序与偏移分页。"},
        ]
    },
}

ZHIHU_COMMENT_CONTRACT = {
    "endpoint_id": "zhihu.get_answer_comments_v1",
    "pagination": {"type": "offset", "params": ["offset"]},
    "params": [
        {"name": "answer_id", "api_name": "answerId", "required": True, "type": "string"},
        {"name": "order_by", "api_name": "orderBy", "required": False, "type": "string", "default": "score"},
        {"name": "offset", "api_name": "offset", "required": False, "type": "string", "default": ""},
    ],
}


class StubMcpClient:
    """Replays scripted tool responses and records every call."""

    def __init__(self, *, token_present=True, balance=(18.4, 18.35)):
        self.token_present = token_present
        self.stats = McpStats()
        self.calls: list[tuple[str, dict]] = []
        self._balance = list(balance)

    def call_tool(self, name, arguments=None, use_cache=None):
        arguments = dict(arguments or {})
        self.calls.append((name, arguments))
        if not self.token_present:
            return McpCallResult(ok=False, tool=name, error="JUSTONEAPI_TOKEN 未设置")
        self.stats.tool_calls += 1
        if name == "search_endpoints":
            query = arguments.get("query", "")
            platform = arguments.get("platform", "")
            # 评论意图优先匹配：真实评论查询里也会出现「视频」二字。
            table = {
                ("评论", "bilibili"): BILIBILI_COMMENT_CANDIDATES,
                ("评论", "zhihu"): ZHIHU_COMMENT_CANDIDATES,
                ("视频", "bilibili"): BILIBILI_SEARCH_CANDIDATES,
                ("搜索", "zhihu"): ZHIHU_SEARCH_CANDIDATES,
            }
            for (needle, want_platform), payload in table.items():
                if want_platform == platform and needle in query:
                    return McpCallResult(ok=True, tool=name, data=payload)
            return McpCallResult(ok=True, tool=name, data={"success": True, "data": {"endpoints": []}})
        if name == "get_endpoint_schema":
            table = {
                "bilibili.get_video_detail_v2": {
                    "endpoint_id": "bilibili.get_video_detail_v2",
                    "params": [{"name": "bvid", "required": True, "type": "string"}],
                    "pagination": {},
                },
                "bilibili.search_video_detail_v2": {
                    "endpoint_id": "bilibili.search_video_detail_v2",
                    "params": [{"name": "bvid", "required": True, "type": "string"}],
                    "pagination": {},
                },
                "bilibili.search_video_v2": BILIBILI_SEARCH_CONTRACT,
                "bilibili.get_video_comment_v2": BILIBILI_COMMENT_CONTRACT,
                "zhihu.search_v1": ZHIHU_SEARCH_CONTRACT,
                "zhihu.get_answer_comments_v1": ZHIHU_COMMENT_CONTRACT,
            }
            payload = table.get(arguments.get("endpoint_id"))
            if payload is None:
                return McpCallResult(ok=False, tool=name, error="unknown endpoint")
            return McpCallResult(ok=True, tool=name, data=payload)
        if name == "call_endpoint":
            endpoint_id = arguments.get("endpoint_id")
            params = arguments.get("params") or {}
            if endpoint_id == "bilibili.search_video_v2":
                return McpCallResult(ok=True, tool=name, data=load_fixture("justone_bilibili.json"))
            if endpoint_id == "bilibili.get_video_comment_v2":
                return McpCallResult(ok=True, tool=name, data=load_fixture("justone_bilibili.json"))
            if endpoint_id == "zhihu.search_v1":
                return McpCallResult(ok=True, tool=name, data=load_fixture("justone_zhihu.json"))
            if endpoint_id == "zhihu.get_answer_comments_v1":
                return McpCallResult(ok=True, tool=name, data=load_fixture("justone_zhihu.json"))
            return McpCallResult(ok=False, tool=name, error=f"unexpected endpoint {endpoint_id} params={params}")
        if name == "get_account_balance":
            value = self._balance.pop(0) if self._balance else 18.3
            return McpCallResult(
                ok=True,
                tool=name,
                data={"success": True, "data": {"balance": value, "currency": "CNY", "availableBalance": value}},
            )
        if name == "list_platforms":
            return McpCallResult(ok=True, tool=name, data={"success": True, "endpoint_count": 319, "platforms": []})
        raise AssertionError(f"unexpected tool {name}")


def make_provider(client=None, **overrides):
    options = {"client": client or StubMcpClient(), "clock": lambda: "2026-09-24T12:00:00Z"}
    options.update(overrides)
    return JustOneMcpProvider(**options)


class DiscoveryTests(unittest.TestCase):
    def test_resolve_endpoint_ranks_candidates_and_reads_the_schema(self):
        provider = make_provider()
        resolved = provider.resolve_endpoint("bilibili", "search")
        self.assertEqual(resolved["endpoint_id"], "bilibili.search_video_v2")
        self.assertGreater(resolved["score"], 0)
        self.assertEqual(resolved["discovery_query"], provider.intent("bilibili", "search")["query"])
        self.assertTrue(resolved["contract"]["required"])
        self.assertEqual(resolved["contract"]["pagination"]["type"], "page")
        calls = provider.client.calls
        self.assertEqual(calls[0][0], "search_endpoints")
        self.assertEqual(calls[1][0], "get_endpoint_schema")
        self.assertEqual(calls[1][1]["endpoint_id"], "bilibili.search_video_v2")

    def test_resolve_endpoint_prefers_comments_over_danmu(self):
        provider = make_provider()
        resolved = provider.resolve_endpoint("bilibili", "comments")
        self.assertEqual(resolved["endpoint_id"], "bilibili.get_video_comment_v2")

    def test_text_score_alone_would_pick_the_detail_endpoint(self):
        candidates = BILIBILI_SEARCH_CANDIDATES["data"]["endpoints"]
        ranked = JustOneMcpProvider.rank_candidates(
            candidates, JustOneMcpProvider.PLATFORM_INTENTS["bilibili"]["search"]
        ) if hasattr(JustOneMcpProvider, "PLATFORM_INTENTS") else None
        if ranked is None:  # PLATFORM_INTENTS 是模块常量
            from source_adapter.providers.justone import PLATFORM_INTENTS

            ranked = JustOneMcpProvider.rank_candidates(
                candidates, PLATFORM_INTENTS["bilibili"]["search"]
            )
        self.assertEqual(ranked[0][0]["endpoint_id"], "bilibili.search_video_v2")

    def test_search_endpoint_must_accept_a_keyword(self):
        provider = make_provider()
        resolved = provider.resolve_endpoint("bilibili", "search")
        # 详情端点在文本上更"像"视频端点，但契约里没有关键词参数，必须被淘汰
        self.assertEqual(resolved["endpoint_id"], "bilibili.search_video_v2")
        checks = {check["endpoint_id"]: check for check in resolved["contract_checks"]}
        self.assertEqual(resolved["contract_checks"][0]["endpoint_id"], "bilibili.search_video_v2")
        self.assertIn("bilibili.search_video_detail_v2", checks, "契约校验必须覆盖文本排名靠前的候选")
        self.assertFalse(checks["bilibili.search_video_detail_v2"]["contract_ok"])
        self.assertIn("关键词", checks["bilibili.search_video_detail_v2"]["reason"])

    def test_validate_contract_rejects_paramless_endpoints(self):
        ok, reason = JustOneMcpProvider.validate_contract(
            {"params": [], "required": [], "pagination": {}}, "search"
        )
        self.assertFalse(ok)
        self.assertIn("关键词", reason)
        ok, reason = JustOneMcpProvider.validate_contract(BILIBILI_SEARCH_CONTRACT, "search")
        self.assertTrue(ok)
        ok, reason = JustOneMcpProvider.validate_contract(BILIBILI_COMMENT_CONTRACT, "comments")
        self.assertTrue(ok)
        ok, reason = JustOneMcpProvider.validate_contract(BILIBILI_SEARCH_CONTRACT, "comments")
        self.assertFalse(ok)

    def test_unknown_intent_raises(self):
        provider = make_provider()
        with self.assertRaises(ValueError):
            provider.resolve_endpoint("bilibili", "nonsense")
        with self.assertRaises(ValueError):
            provider.resolve_endpoint("myspace", "search")

    def test_no_candidate_is_a_gap_not_a_guess(self):
        provider = make_provider()

        class Empty(StubMcpClient):
            def call_tool(self, name, arguments=None, use_cache=None):
                self.calls.append((name, dict(arguments or {})))
                return McpCallResult(ok=True, tool=name, data={"success": True, "data": {"endpoints": []}})

        provider = make_provider(Empty())
        with self.assertRaises(LookupError) as ctx:
            provider.resolve_endpoint("bilibili", "search")
        self.assertIn("端点", str(ctx.exception))


class ParamMappingTests(unittest.TestCase):
    def test_builds_keyword_and_page_params(self):
        contract = JustOneMcpProvider.normalize_contract(BILIBILI_SEARCH_CONTRACT)
        params, missing = JustOneMcpProvider.build_params(
            contract, keyword="高中暗恋", page=2
        )
        self.assertEqual(params, {"keyword": "高中暗恋", "page": "2"})
        self.assertEqual(missing, [])

    def test_maps_object_id_and_reports_missing_required_params(self):
        contract = JustOneMcpProvider.normalize_contract(BILIBILI_COMMENT_CONTRACT)
        params, missing = JustOneMcpProvider.build_params(contract, object_id="743812345")
        self.assertEqual(params, {"aid": "743812345"})
        self.assertEqual(missing, [])
        params, missing = JustOneMcpProvider.build_params(contract)
        self.assertEqual(params, {})
        self.assertEqual(missing, ["aid"])

    def test_supports_offset_and_vertical_enum(self):
        contract = JustOneMcpProvider.normalize_contract(ZHIHU_SEARCH_CONTRACT)
        params, missing = JustOneMcpProvider.build_params(
            contract, keyword="学生时代爱情", vertical="answer"
        )
        self.assertEqual(params["keyword"], "学生时代爱情")
        self.assertEqual(params["vertical"], "answer")
        self.assertEqual(missing, [])

    def test_never_invents_values_for_unknown_optional_params(self):
        contract = JustOneMcpProvider.normalize_contract(BILIBILI_SEARCH_CONTRACT)
        params, _ = JustOneMcpProvider.build_params(contract, keyword="x")
        self.assertNotIn("order", params)


class CollectTests(unittest.TestCase):
    def test_search_collects_records_and_records_the_audit_trail(self):
        client = StubMcpClient()
        provider = make_provider(client)
        result = provider.collect("bilibili", "校园 爱情", limit=20, top=0)
        self.assertIsInstance(result, ProviderResult)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.records[0].source_id, "bilibili:BV1wN4y1D7gz")
        self.assertTrue(result.raw_payloads)
        endpoints = [entry.get("endpoint_id") for entry in result.audit if entry.get("step") == "call"]
        self.assertEqual(endpoints, ["bilibili.search_video_v2"])
        discover = [entry for entry in result.audit if entry.get("step") == "discover"][0]
        self.assertEqual(discover["endpoint_id"], "bilibili.search_video_v2")
        self.assertTrue(discover["candidates"])

    def test_collect_with_comments_uses_the_object_id_from_the_search_result(self):
        client = StubMcpClient()
        provider = make_provider(client)
        result = provider.collect("bilibili", "校园 爱情", limit=20, top=1, comments=True)
        comment_calls = [
            entry for entry in result.audit if entry.get("step") == "call" and entry.get("intent") == "comments"
        ]
        self.assertEqual(len(comment_calls), 1)
        self.assertEqual(comment_calls[0]["params"], {"aid": "743812345"})
        self.assertTrue(any(comment.source_id == "bilibili:1400000001" for comment in result.comments))

    def test_zhihu_uses_answer_id_for_comments(self):
        client = StubMcpClient()
        provider = make_provider(client)
        result = provider.collect("zhihu", "学生时代爱情", limit=20, top=1, comments=True)
        comment_calls = [
            entry for entry in result.audit if entry.get("step") == "call" and entry.get("intent") == "comments"
        ]
        self.assertEqual(comment_calls[0]["params"], {"answer_id": "987654321"})
        self.assertEqual(result.records[0].source_id, "zhihu:987654321")

    def test_cost_is_measured_from_the_account_balance(self):
        client = StubMcpClient(balance=(18.40, 18.35))
        provider = make_provider(client)
        result = provider.collect("bilibili", "x", limit=5, top=0)
        self.assertEqual(result.paid_calls, 1)
        self.assertIn("18.4", json.dumps(result.audit, ensure_ascii=False))
        self.assertAlmostEqual(result.cost["spent"], 0.05, places=6)
        self.assertEqual(result.cost["currency"], "CNY")

    def test_missing_token_is_unavailable(self):
        provider = make_provider(StubMcpClient(token_present=False))
        status = provider.doctor()
        self.assertEqual(status.status, "unavailable")
        result = provider.collect("bilibili", "x")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("JUSTONEAPI_TOKEN", result.error)

    def test_doctor_is_ready_when_the_token_is_present(self):
        provider = make_provider()
        status = provider.doctor()
        self.assertEqual(status.status, "ready")
        self.assertIn("发现", status.detail)


class CommentTargetTests(unittest.TestCase):
    def test_comment_targets_prefer_objects_with_more_comments(self):
        from source_adapter.models import Coverage, SourceRecord

        def record(source_id, comments, likes):
            return SourceRecord(
                source_id=source_id,
                platform="zhihu",
                record_kind="answer",
                source_type="community",
                title="t",
                url=f"https://www.zhihu.com/answer/{source_id.split(':')[-1]}",
                full_text="正文",
                engagement={"comments": comments, "votes": likes},
                coverage=Coverage(level="complete"),
            )

        records = [record("zhihu:1", 0, 9999), record("zhihu:2", 676, 18108), record("zhihu:3", 3, 5)]
        records.sort(key=__import__("source_adapter.providers.justone", fromlist=["_comment_target_rank"])._comment_target_rank, reverse=True)
        self.assertEqual([r.source_id for r in records][:2], ["zhihu:2", "zhihu:3"])


class ComplianceTests(unittest.TestCase):
    def test_module_never_hardcodes_a_data_endpoint_id(self):
        source = Path("source_adapter/providers/justone.py").read_text(encoding="utf-8")
        pattern = re.compile(r"\"(?:bilibili|zhihu|xiaohongshu|weibo|douyin|kuaishou)\.[a-z0-9_]+(?:_v\d+)?\"")
        self.assertIsNone(pattern.search(source), "数据端点 ID 必须是运行时发现的，不得写死在代码里")

    def test_endpoint_list_comes_from_discovery_not_configuration(self):
        client = StubMcpClient()
        provider = make_provider(client)
        provider.resolve_endpoint("zhihu", "search")
        self.assertIn(("search_endpoints", {"query": provider.intent("zhihu", "search")["query"], "platform": "zhihu", "limit": 8}), client.calls)


if __name__ == "__main__":
    unittest.main()
