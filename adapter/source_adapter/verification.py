"""Machine-readable capability verification and policy scanning.

``run_verification`` performs the read-only smoke checks required by the handoff
(Tieba public search, AnySearch anonymous search) and records provider status,
coverage limits, gaps and cost in ``verification/latest.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .models import ProviderStatus, worst_provider_status
from .normalize import now_iso
from .pipeline import EXCLUSIONS, COST_NOTE, ResearchPipeline

SCHEMA_VERSION = "1.0.0"

# Code paths this project must never contain. The list is assembled from
# fragments so a scan of this very file does not match its own patterns.
BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("browser-automation", "play" + "wright"),
    ("browser-automation", "sele" + "nium"),
    ("browser-automation", "pyppe" + "teer"),
    ("browser-automation", "web" + "driver"),
    ("browser-automation", "chromium" + ".connect"),
    ("computer-use", "computer" + "_use"),
    ("computer-use", "computer" + "-use"),
    ("captcha-bypass", "cap" + "tcha"),
    ("captcha-bypass", "打" + "码平台"),
    ("captcha-bypass", "验证" + "码识别"),
    ("account-actions", "log" + "in("),
    ("account-actions", "post_" + "reply"),
)

DEFAULT_TIEBA_QUERY = "高中 暗恋"
DEFAULT_ANYSEARCH_QUERY = "高中 暗恋 故事"


def scan_for_banned_paths(root: Path, *, extra_skip: Iterable[Path] = ()) -> list[dict[str, str]]:
    """Scan Python sources under ``root`` for forbidden automation paths."""
    root_path = Path(root)
    skip = {Path(path).resolve() for path in extra_skip}
    skip.add(Path(__file__).resolve())
    findings: list[dict[str, str]] = []
    if not root_path.exists():
        return findings
    for path in sorted(root_path.rglob("*.py")):
        resolved = path.resolve()
        if resolved in skip or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:  # pragma: no cover - defensive only
            continue
        for category, needle in BANNED_PATTERNS:
            if needle.lower() in text:
                findings.append(
                    {"path": str(path), "category": category, "pattern": needle}
                )
    return findings


def _provider_entries(providers: Iterable[ProviderStatus]) -> list[dict[str, Any]]:
    return [status.to_dict() for status in providers]


def _smoke_entry(run: Any, *, status: str, reason: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "status": status,
        "provider_status": run.status if run is not None else "unavailable",
        "records": len(run.records) if run is not None else 0,
        "comments": len(run.comments) if run is not None else 0,
        "termination": run.termination if run is not None else "",
        "coverage": run.coverage.to_dict() if run is not None else {},
        "calls": run.calls if run is not None else 0,
        "from_cache": run.from_cache if run is not None else 0,
        "run_dir": str(run.run_dir) if run is not None else "",
        "gaps": list(run.gaps) if run is not None else [],
        "failures": list(run.failures) if run is not None else [],
    }
    if reason:
        entry["reason"] = reason
    return entry


def run_verification(
    pipeline: ResearchPipeline,
    *,
    tieba_query: str = DEFAULT_TIEBA_QUERY,
    anysearch_query: str = DEFAULT_ANYSEARCH_QUERY,
    max_results: int = 3,
    scan_root: Optional[Path] = None,
    clock: Callable[[], str] = now_iso,
) -> dict[str, Any]:
    """Run the read-only smoke checks and return the verification report."""
    generated_at = clock()
    providers = list(pipeline.doctor())
    statuses = {status.provider: status for status in providers}

    checks: dict[str, Any] = {}
    gaps: list[str] = []

    tieba_status = statuses.get("tieba")
    if tieba_status is not None and tieba_status.status != "unavailable":
        run = pipeline.tieba_search(tieba_query, pages=1)
        pipeline.write_outputs(run)
        ok = run.status in ("ok", "degraded") and bool(run.records)
        checks["tieba_smoke"] = _smoke_entry(run, status="ok" if ok else "failed")
        checks["tieba_smoke"]["artifacts"] = sorted(run.written)
        gaps.extend(f"贴吧公开搜索：{gap}" for gap in run.gaps)
        if not ok:
            gaps.append("贴吧公开搜索烟测未取得记录")
    else:
        checks["tieba_smoke"] = _smoke_entry(None, status="skipped", reason="provider unavailable")
        gaps.append("贴吧公开搜索烟测被跳过：提供器不可用")

    anysearch_status = statuses.get("anysearch")
    if anysearch_status is not None and anysearch_status.status != "unavailable":
        run = pipeline.anysearch_search(anysearch_query, max_results=max_results)
        pipeline.write_outputs(run)
        ok = run.status in ("ok", "degraded") and bool(run.records)
        checks["anysearch_smoke"] = _smoke_entry(run, status="ok" if ok else "failed")
        checks["anysearch_smoke"]["artifacts"] = sorted(run.written)
        gaps.extend(f"AnySearch：{gap}" for gap in run.gaps)
        if not ok:
            gaps.append("AnySearch 烟测未取得记录")
    else:
        checks["anysearch_smoke"] = _smoke_entry(None, status="skipped", reason="provider unavailable")
        gaps.append("AnySearch 烟测被跳过：提供器不可用")

    mcp_status = statuses.get("justone-mcp")
    mcp_provider = getattr(pipeline, "justone_mcp", None)
    if mcp_status is not None and mcp_status.status != "unavailable" and mcp_provider is not None:
        # 端点发现与 schema 读取不计费，因此每次验证都可以真实执行一遍。
        try:
            resolved = mcp_provider.resolve_endpoint("bilibili", "search")
            checks["justone_endpoint_discovery"] = {
                "status": "ok",
                "platform": resolved["platform"],
                "intent": resolved["intent"],
                "discovery_query": resolved["discovery_query"],
                "endpoint_id": resolved["endpoint_id"],
                "candidates": resolved["candidates"],
                "contract_checks": [dict(item) for item in resolved.get("contract_checks", ())],
                "cost": "免费（search_endpoints / get_endpoint_schema 不计费）",
            }
        except Exception as exc:  # 网络或协议问题不应中断整份验证报告
            checks["justone_endpoint_discovery"] = {
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            gaps.append("Just One API 端点发现失败，B站/知乎/小红书采集不可用")
    else:
        checks["justone_endpoint_discovery"] = {
            "status": "unavailable",
            "reason": (
                "JUSTONEAPI_TOKEN 未设置：直连通道不可用。"
                "仍可用 justone-manifest 生成清单、由会话执行，再用 ingest-justone 归一化"
            ),
            "manifest_command": "python3 -m source_adapter justone-manifest bilibili \"校园 爱情 故事\" --comments --limit 20",
            "ingest_command": "python3 -m source_adapter ingest-justone bilibili <saved-response.json> --output <dir>",
        }
        gaps.append("Just One API 端点发现未完成：JUSTONEAPI_TOKEN 未设置（直连通道不可用）")

    public_thread_status = statuses.get("tieba-thread")
    aiotieba_status = statuses.get("tieba-aiotieba")
    if public_thread_status is not None and public_thread_status.status != "unavailable":
        checks["tieba_deep_dive"] = {
            "status": "ready",
            "via": "tieba-thread（公开移动页，无需可选依赖）",
            "reason": public_thread_status.detail,
        }
    elif aiotieba_status is not None and aiotieba_status.status != "unavailable":
        checks["tieba_deep_dive"] = {"status": "ready", "via": "aiotieba", "reason": aiotieba_status.detail}
    else:
        checks["tieba_deep_dive"] = {
            "status": "unavailable",
            "reason": (aiotieba_status.detail if aiotieba_status else "无可用的贴吧正文通道"),
        }
        gaps.append("贴吧楼层与楼中楼深挖不可用：公开页通道与 aiotieba 都不可用")

    scan_target = Path(scan_root) if scan_root else Path(__file__).resolve().parents[1]
    findings = scan_for_banned_paths(scan_target)
    if findings:
        gaps.append(f"在 {scan_target} 中发现 {len(findings)} 处被禁止的自动化代码路径")

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "verdict": worst_provider_status(providers),
        "providers": _provider_entries(providers),
        "checks": checks,
        "gaps": gaps,
        "coverage_limits": [
            "贴吧公开移动端搜索只返回摘要与主题帖元数据；完整正文与楼中楼走 tieba-thread 公开移动页，且上游只渲染前 30 层",
            "AnySearch 返回搜索摘要；正文需另行 extract",
            "Just One API 的数据端点 ID 由运行时发现，不写死；发现与 schema 读取免费，数据调用计费",
            "本次验证不做批量收费调用",
        ],
        "banned_code_paths": findings,
        "scan_root": str(scan_target),
        "cost": {"paid_calls": 0, "estimated_cost": "0", "note": COST_NOTE},
        "exclusions": list(EXCLUSIONS),
    }
    return report
