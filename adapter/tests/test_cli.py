"""Tests for the command line interface and the verification report."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from source_adapter.cache import FileCache
from source_adapter.cli import main
from source_adapter.http import HttpClient
from source_adapter.pipeline import ResearchPipeline
from source_adapter.providers.anysearch import AnySearchProvider
from source_adapter.providers.justone import JustOneBridge
from source_adapter.providers.tieba import AiotiebaProvider, TiebaPublicProvider
from source_adapter.verification import run_verification, scan_for_banned_paths

try:  # `python3 -m unittest tests.test_cli`
    from tests.helpers import (
        CompletedProcessLike,
        FakeClock,
        FakeTransport,
        ScriptedRunner,
        fixture_path,
        json_response,
        load_fixture,
    )
except ModuleNotFoundError:  # `python3 -m unittest discover -s tests`
    from helpers import (
        CompletedProcessLike,
        FakeClock,
        FakeTransport,
        ScriptedRunner,
        fixture_path,
        json_response,
        load_fixture,
    )

TIEBA_QUERY = "高中 暗恋"
FAKE_CLI = textwrap.dedent(
    '''
    import pathlib
    import sys

    counter = pathlib.Path(__file__).with_name("calls.txt")
    count = int(counter.read_text() or "0") if counter.exists() else 0
    counter.write_text(str(count + 1))

    print("## Search Results (1 results, 3ms)")
    print()
    print("### 1. 假 CLI 结果")
    print("- **URL**: https://example.com/fake")
    print("- 摘要文本")
    '''
).lstrip()


def run_cli(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


def quiet_http(transport, cache=None, provider="tieba"):
    clock = FakeClock()
    return HttpClient(
        transport=transport,
        provider=provider,
        spacing=0.0,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
        cache=cache,
    )


class DoctorTests(unittest.TestCase):
    def test_doctor_json_reports_only_supported_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run_cli(
                [
                    "doctor",
                    "--json",
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                    "--anysearch-skill-dir",
                    str(Path(tmp) / "missing-skill"),
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertTrue(payload["providers"])
            names = [entry["provider"] for entry in payload["providers"]]
            self.assertEqual(
                names,
                ["tieba", "tieba-aiotieba", "tieba-thread", "anysearch", "justone", "steam", "justone-mcp"],
            )
            for entry in payload["providers"]:
                self.assertIn(entry["status"], ("ready", "degraded", "unavailable"))
            self.assertIn(payload["verdict"], ("ready", "degraded", "unavailable"))
            self.assertNotIn("sk-", output)
            self.assertNotIn("Bearer", output)

    def test_doctor_human_output_names_every_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run_cli(
                ["doctor", "--cache-dir", str(Path(tmp) / "cache"), "--anysearch-skill-dir", str(Path(tmp) / "missing")]
            )
            self.assertEqual(code, 0)
            for name in ("tieba", "aiotieba", "anysearch", "justone"):
                self.assertIn(name, output)

    def test_doctor_can_fail_on_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_cli(
                [
                    "doctor",
                    "--json",
                    "--fail-on-unavailable",
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                    "--anysearch-skill-dir",
                    str(Path(tmp) / "missing"),
                ]
            )
            self.assertEqual(code, 1)


class ManifestCliTests(unittest.TestCase):
    def test_manifest_is_printed_as_json(self):
        code, output = run_cli(["justone-manifest", "bilibili", "校园 爱情 故事", "--comments", "--limit", "20"])
        self.assertEqual(code, 0)
        manifest = json.loads(output)
        self.assertEqual(manifest["platform"], "bilibili")
        self.assertTrue(manifest["requires_comments"])
        self.assertTrue(manifest["safety"]["read_only"])
        self.assertIsNone(manifest["endpoint_discovery"]["endpoint_id"])

    def test_manifest_can_be_written_to_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "manifest.json"
            code, _ = run_cli(
                ["justone-manifest", "zhihu", "学生时代的爱情故事", "--out", str(target)]
            )
            self.assertEqual(code, 0)
            manifest = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(manifest["platform"], "zhihu")
            self.assertFalse(manifest["requires_comments"])

    def test_manifest_rejects_unsupported_platform(self):
        code, output = run_cli(["justone-manifest", "douyin", "x"])
        self.assertEqual(code, 2)
        self.assertIn("platform", output.lower())


class ValidateInputTests(unittest.TestCase):
    def test_valid_payload_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "request_id": "req-1",
                        "query": "高中 暗恋",
                        "results": [
                            {
                                "source_id": "tieba:1",
                                "title": "标题",
                                "source_type": "community",
                                "url": "https://tieba.baidu.com/p/1",
                                "excerpt": "摘要",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            code, output = run_cli(["validate-input", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("ok", output)

    def test_invalid_payload_exits_one_and_explains(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(json.dumps({"schema_version": "1.0.0"}), encoding="utf-8")
            code, output = run_cli(["validate-input", str(path)])
            self.assertEqual(code, 1)
            self.assertIn("request_id", output)


class TiebaSearchCliTests(unittest.TestCase):
    def warm_cache(self, cache_dir, responses):
        cache = FileCache(cache_dir)
        provider = TiebaPublicProvider(http=quiet_http(FakeTransport(responses), cache=cache))
        result = provider.search(TIEBA_QUERY, page=1, page_size=20)
        self.assertEqual(result.status, "ready")
        return cache

    def test_offline_with_empty_cache_exits_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_cli(
                [
                    "tieba-search",
                    TIEBA_QUERY,
                    "--pages",
                    "1",
                    "--output",
                    str(Path(tmp) / "run"),
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                    "--offline",
                ]
            )
            self.assertEqual(code, 3)

    def test_offline_with_warm_cache_writes_every_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            self.warm_cache(cache_dir, [json_response(load_fixture("tieba_search.json"))])
            run_dir = Path(tmp) / "run"
            code, output = run_cli(
                [
                    "tieba-search",
                    TIEBA_QUERY,
                    "--pages",
                    "1",
                    "--output",
                    str(run_dir),
                    "--cache-dir",
                    str(cache_dir),
                    "--offline",
                    "--json",
                ]
            )
            self.assertEqual(code, 0)
            summary = json.loads(output)
            self.assertEqual(summary["records"], 3)
            self.assertTrue((run_dir / "raw" / "tieba-public-search-p1.json").is_file())
            self.assertTrue((run_dir / "records.jsonl").is_file())
            self.assertTrue((run_dir / "threads" / "tieba-11045552764.json").is_file())
            self.assertTrue((run_dir / "search-agent-input.json").is_file())
            report = json.loads((run_dir / "run-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["termination"], "page_limit")
            self.assertTrue(report["gaps"])

    def test_repeated_duplicate_run_does_not_hit_the_network_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(Path(tmp) / "cache")
            transport = FakeTransport([json_response(load_fixture("tieba_search.json"))])
            provider = TiebaPublicProvider(http=quiet_http(transport, cache=cache))
            provider.search(TIEBA_QUERY, page=1)
            provider.search(TIEBA_QUERY, page=1)
            self.assertEqual(transport.call_count, 1)


class AnySearchCliTests(unittest.TestCase):
    def write_fake_cli(self, tmp) -> str:
        script = Path(tmp) / "fake_anysearch.py"
        script.write_text(FAKE_CLI, encoding="utf-8")
        return f"{sys.executable} {script}"

    def test_search_uses_the_fake_cli_and_caches_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = self.write_fake_cli(tmp)
            cache_dir = Path(tmp) / "cache"
            run_dir = Path(tmp) / "run"
            argv = [
                "anysearch",
                "校园 爱情",
                "--max-results",
                "1",
                "--output",
                str(run_dir),
                "--cache-dir",
                str(cache_dir),
                "--anysearch-command",
                command,
                "--json",
            ]
            code, output = run_cli(argv)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["records"], 1)
            lines = (run_dir / "records.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            calls = Path(tmp) / "calls.txt"
            self.assertEqual(calls.read_text(), "1")

            code, _ = run_cli([*argv, "--offline", "--output", str(Path(tmp) / "run2")])
            self.assertEqual(code, 0)
            self.assertEqual(calls.read_text(), "1")

    def test_extract_writes_a_single_page_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_extract.py"
            script.write_text(
                'print("# 标题\\n\\n正文段落")\n',
                encoding="utf-8",
            )
            run_dir = Path(tmp) / "run"
            code, output = run_cli(
                [
                    "anysearch-extract",
                    "https://example.com/page",
                    "--output",
                    str(run_dir),
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                    "--anysearch-command",
                    f"{sys.executable} {script}",
                    "--json",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["records"], 1)
            thread_files = list((run_dir / "threads").glob("*.json"))
            self.assertEqual(len(thread_files), 1)
            self.assertTrue(thread_files[0].name.startswith("anysearch-url-"))

    def test_failing_cli_exits_one_with_a_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "failing.py"
            script.write_text('import sys\nprint("boom", file=sys.stderr)\nsys.exit(3)\n', encoding="utf-8")
            code, _ = run_cli(
                [
                    "anysearch",
                    "x",
                    "--output",
                    str(Path(tmp) / "run"),
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                    "--anysearch-command",
                    f"{sys.executable} {script}",
                ]
            )
            self.assertEqual(code, 1)


class IngestCliTests(unittest.TestCase):
    def test_ingest_bilibili_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            code, output = run_cli(
                [
                    "ingest-justone",
                    "bilibili",
                    str(fixture_path("justone_bilibili.json")),
                    "--output",
                    str(run_dir),
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                    "--json",
                ]
            )
            self.assertEqual(code, 0)
            summary = json.loads(output)
            self.assertEqual(summary["records"], 1)
            self.assertEqual(summary["comments"], 2)
            self.assertTrue((run_dir / "threads" / "bilibili-BV1wN4y1D7gz.json").is_file())

    def test_ingest_missing_file_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run_cli(
                [
                    "ingest-justone",
                    "zhihu",
                    str(Path(tmp) / "missing.json"),
                    "--output",
                    str(Path(tmp) / "run"),
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                ]
            )
            self.assertEqual(code, 1)
            self.assertIn("not found", output.lower())


class VerificationTests(unittest.TestCase):
    def build_pipeline(self, tmp, *, transport_responses=None):
        cache = FileCache(Path(tmp) / "cache")
        tieba = TiebaPublicProvider(http=quiet_http(FakeTransport(transport_responses or []), cache=cache))
        anysearch = AnySearchProvider(
            command=["node", "/opt/anysearch/cli.js"],
            env={},
            runner=ScriptedRunner(
                [
                    CompletedProcessLike(
                        returncode=0,
                        stdout=(
                            "## Search Results (1 results, 9ms)\n\n"
                            "### 1. 烟测结果\n- **URL**: https://example.com/smoke\n- 摘要\n"
                        ),
                    )
                ]
            ),
        )
        return ResearchPipeline(
            Path(tmp) / "out",
            cache=cache,
            tieba=tieba,
            aiotieba=AiotiebaProvider(importer=_missing_importer),
            anysearch=anysearch,
            justone=JustOneBridge(),
            clock=lambda: "2026-09-24T12:00:00Z",
            run_id_factory=lambda label: f"run-{label}",
        )

    def test_report_records_statuses_limits_and_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self.build_pipeline(
                tmp, transport_responses=[json_response(load_fixture("tieba_search.json"))]
            )
            report = run_verification(
                pipeline,
                scan_root=Path(tmp),
                clock=lambda: "2026-09-24T12:00:00Z",
            )
            self.assertEqual(report["schema_version"], "1.0.0")
            self.assertEqual(report["generated_at"], "2026-09-24T12:00:00Z")
            names = [entry["provider"] for entry in report["providers"]]
            self.assertEqual(
                names, ["tieba", "tieba-aiotieba", "tieba-thread", "anysearch", "justone", "steam"]
            )
            for entry in report["providers"]:
                self.assertIn(entry["status"], ("ready", "degraded", "unavailable"))
            self.assertEqual(report["checks"]["tieba_smoke"]["status"], "ok")
            self.assertEqual(report["checks"]["tieba_smoke"]["records"], 3)
            self.assertEqual(report["checks"]["anysearch_smoke"]["status"], "ok")
            self.assertEqual(report["checks"]["anysearch_smoke"]["records"], 1)
            self.assertEqual(report["checks"]["justone_endpoint_discovery"]["status"], "unavailable")
            self.assertIn("cost", report)
            self.assertEqual(report["cost"]["paid_calls"], 0)
            self.assertTrue(report["gaps"])
            self.assertEqual(report["banned_code_paths"], [])

    def test_ready_mcp_provider_runs_a_free_discovery_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self.build_pipeline(tmp, transport_responses=[])

            class StubMcp:
                name = "justone-mcp"

                def doctor(self):
                    from source_adapter.models import ProviderStatus

                    return ProviderStatus(provider=self.name, status="ready", detail="stub")

                def resolve_endpoint(self, platform, intent, **kwargs):
                    return {
                        "platform": platform,
                        "intent": intent,
                        "discovery_query": "按关键词搜索哔哩哔哩视频列表",
                        "endpoint_id": "stub.search_v1",
                        "candidates": [{"endpoint_id": "stub.search_v1", "score": 8}],
                        "contract_checks": [{"endpoint_id": "stub.search_v1", "contract_ok": True}],
                    }

            pipeline.justone_mcp = StubMcp()
            report = run_verification(pipeline, scan_root=Path(tmp), clock=lambda: "2026-09-24T12:00:00Z")
            check = report["checks"]["justone_endpoint_discovery"]
            self.assertEqual(check["status"], "ok")
            self.assertEqual(check["endpoint_id"], "stub.search_v1")
            self.assertIn("免费", check["cost"])
            self.assertNotIn("Just One API 端点发现未完成", " ".join(report["gaps"]))

    def test_banned_code_path_scan_finds_forbidden_automation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pkg"
            root.mkdir()
            (root / "clean.py").write_text("print('hello')\n", encoding="utf-8")
            self.assertEqual(scan_for_banned_paths(root), [])
            automation = "play" + "wright"
            (root / "bad.py").write_text(
                f"from {automation}.sync_api import sync_{automation}\n", encoding="utf-8"
            )
            findings = scan_for_banned_paths(root)
            self.assertEqual(len(findings), 1)
            self.assertIn("bad.py", findings[0]["path"])
            self.assertEqual(findings[0]["pattern"], automation)

    def test_verify_cli_writes_latest_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_anysearch.py"
            script.write_text(FAKE_CLI, encoding="utf-8")
            cache_dir = Path(tmp) / "cache"
            cache = FileCache(cache_dir)
            provider = TiebaPublicProvider(
                http=quiet_http(FakeTransport([json_response(load_fixture("tieba_search.json"))]), cache=cache)
            )
            provider.search(TIEBA_QUERY, page=1)
            target = Path(tmp) / "verification" / "latest.json"
            code, output = run_cli(
                [
                    "verify",
                    "--json",
                    "--offline",
                    "--verification-path",
                    str(target),
                    "--output",
                    str(Path(tmp) / "out"),
                    "--cache-dir",
                    str(cache_dir),
                    "--scan-root",
                    str(Path(tmp) / "out"),
                    "--anysearch-command",
                    f"{sys.executable} {script}",
                ]
            )
            self.assertEqual(code, 0)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(report["checks"]["tieba_smoke"]["status"], "ok")
            self.assertEqual(json.loads(output)["generated_at"], report["generated_at"])


def _missing_importer(name):
    raise ImportError(name)


class ParserTests(unittest.TestCase):
    def test_unknown_command_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["definitely-not-a-command"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
