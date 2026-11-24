"""Smoke contract: the documented commands must work as documented.

These tests exercise the real CLI entry point in a subprocess (no network) and
assert the package contains no browser-automation or access-control bypass path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from source_adapter.verification import scan_for_banned_paths

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
README = PACKAGE_ROOT / "README.md"
SMOKE_SCRIPT = PACKAGE_ROOT / "scripts" / "smoke_test.sh"
VERIFICATION = PACKAGE_ROOT / "verification" / "latest.json"

SUPPORTED_STATUSES = ("ready", "degraded", "unavailable")
ACCEPTANCE_COMMANDS = (
    "python3 -m unittest discover -s tests -v",
    "python3 -m source_adapter doctor --json",
    'python3 -m source_adapter tieba-search "高中 暗恋" --pages 1 --output verification/tieba-smoke',
    'python3 -m source_adapter justone-manifest bilibili "校园 爱情 故事" --comments --limit 20',
    'python3 -m source_adapter justone-manifest zhihu "学生时代的爱情故事" --comments --limit 20',
)
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{6,}"),
)


def run_module(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "source_adapter", *args],
        cwd=str(PACKAGE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


class DoctorContractTests(unittest.TestCase):
    def test_doctor_json_reports_only_supported_statuses(self):
        completed = run_module("doctor", "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], "1.0.0")
        self.assertIn(payload["verdict"], SUPPORTED_STATUSES)
        self.assertTrue(payload["providers"])
        for entry in payload["providers"]:
            self.assertIn(entry["status"], SUPPORTED_STATUSES)
            self.assertIn("detail", entry)
            self.assertIn("checked_at", entry)

    def test_doctor_output_carries_no_credential_like_values(self):
        completed = run_module("doctor", "--json")
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(pattern.search(completed.stdout), pattern.pattern)

    def test_help_lists_every_documented_command(self):
        completed = run_module("--help")
        self.assertEqual(completed.returncode, 0)
        for command in (
            "doctor",
            "tieba-search",
            "tieba-thread",
            "anysearch",
            "anysearch-extract",
            "justone-manifest",
            "justone-tools",
            "justone-balance",
            "justone-discover",
            "justone-collect",
            "ingest-justone",
            "validate-input",
            "verify",
        ):
            self.assertIn(command, completed.stdout)

    def test_manifest_command_is_offline_and_has_no_endpoint_id(self):
        completed = run_module("justone-manifest", "bilibili", "校园 爱情 故事", "--comments", "--limit", "5")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = json.loads(completed.stdout)
        self.assertIsNone(manifest["endpoint_discovery"]["endpoint_id"])
        self.assertTrue(manifest["call_budget"]["requires_user_approval"])


class PolicyContractTests(unittest.TestCase):
    def test_package_contains_no_banned_automation_paths(self):
        findings = scan_for_banned_paths(PACKAGE_ROOT)
        self.assertEqual(findings, [], f"被禁止的代码路径：{findings}")

    def test_no_token_is_persisted_anywhere_in_the_package(self):
        offenders = []
        for path in PACKAGE_ROOT.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix.lower() not in (".py", ".md", ".toml", ".sh", ".json", ".jsonl"):
                continue
            if "verification" in path.parts and path.name.endswith(".jsonl"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in (re.compile(r"(?i)JUSTONEAPI_TOKEN\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{8,}"),
                            re.compile(r"(?i)ANYSEARCH_API_KEY\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{8,}")):
                for match in pattern.finditer(text):
                    if "PLACEHOLDER" in match.group(0) or "your_" in match.group(0):
                        continue
                    offenders.append(f"{path}: {match.group(0)[:40]}")
        self.assertEqual(offenders, [])


class DocumentationContractTests(unittest.TestCase):
    def test_readme_documents_installation_usage_and_limits(self):
        self.assertTrue(README.is_file(), "README.md 必须存在")
        text = README.read_text(encoding="utf-8")
        for phrase in (
            "安装",
            "配置",
            "命令",
            "数据格式",
            "费用",
            "已知限制",
            "aiotieba",
            "endpoint_id",
            "schema_version",
            "1.0.0",
        ):
            self.assertIn(phrase, text, f"README 缺少章节或说明：{phrase}")

    def test_readme_documents_every_acceptance_command(self):
        text = README.read_text(encoding="utf-8")
        for command in ACCEPTANCE_COMMANDS:
            self.assertIn(command, text, f"README 未记录验收命令：{command}")

    def test_smoke_script_exists(self):
        self.assertTrue(SMOKE_SCRIPT.is_file(), "scripts/smoke_test.sh 必须存在")
        self.assertIn("doctor", SMOKE_SCRIPT.read_text(encoding="utf-8"))


class VerificationArtifactTests(unittest.TestCase):
    def test_latest_json_has_the_documented_shape(self):
        if not VERIFICATION.is_file():
            self.skipTest("verification/latest.json 尚未生成")
        report = json.loads(VERIFICATION.read_text(encoding="utf-8"))
        for key in (
            "schema_version",
            "generated_at",
            "verdict",
            "providers",
            "checks",
            "gaps",
            "coverage_limits",
            "banned_code_paths",
            "cost",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["banned_code_paths"], [])
        self.assertEqual(report["cost"]["paid_calls"], 0)
        for entry in report["providers"]:
            self.assertIn(entry["status"], SUPPORTED_STATUSES)
        self.assertIn("tieba_smoke", report["checks"])
        self.assertIn("anysearch_smoke", report["checks"])
        self.assertIn("justone_endpoint_discovery", report["checks"])


if __name__ == "__main__":
    unittest.main()
