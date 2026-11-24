"""Command line interface: ``python3 -m source_adapter <command>``.

Exit codes: ``0`` success, ``1`` the requested operation produced no usable
evidence, ``2`` invalid input or usage, ``3`` an ``--offline`` run missed the
cache.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any, Optional, Sequence

from .cache import FileCache, atomic_write_json
from .config import Config, load_config
from .export import SCHEMA_VERSION, SchemaError, load_json, validate_search_agent_input
from .http import HttpClient
from .mcp import McpHttpClient
from .models import worst_provider_status
from .normalize import now_iso
from .pipeline import ResearchPipeline
from .providers.anysearch import AnySearchProvider
from .providers.justone import JustOneBridge
from .providers.justone import JustOneMcpProvider
from .providers.tieba import TiebaPublicProvider
from .verification import (
    DEFAULT_ANYSEARCH_QUERY,
    DEFAULT_TIEBA_QUERY,
    run_verification,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = PACKAGE_ROOT / ".cache"
DEFAULT_OUTPUT_ROOT = PACKAGE_ROOT / "outputs"
DEFAULT_VERIFICATION_PATH = PACKAGE_ROOT / "verification" / "latest.json"

EXIT_OK = 0
EXIT_NO_EVIDENCE = 1
EXIT_USAGE = 2
EXIT_OFFLINE_MISS = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="source_adapter",
        description=(
            "匿名、只读、可审计的中文社区来源适配器：统一接入贴吧、AnySearch 与 "
            "Just One API 证据。不包含浏览器自动化或验证码绕过路径。"
        ),
    )
    parser.add_argument("--version", action="version", version="source_adapter 0.1.0")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default="", help="配置文件路径（默认 tools/source_adapter/config.toml）"
    )
    common.add_argument(
        "--cache-dir", default="", help="原始响应缓存目录（覆盖配置，默认 tools/source_adapter/.cache）"
    )
    common.add_argument("--no-cache", action="store_true", help="禁用缓存读写")
    common.add_argument("--offline", action="store_true", help="只读缓存，缓存缺失即失败")
    common.add_argument("--json", action="store_true", help="以 JSON 输出结果摘要")
    common.add_argument("--anysearch-command", default="", help="覆盖 AnySearch CLI 命令")
    common.add_argument("--anysearch-skill-dir", default="", help="覆盖 AnySearch skill 目录")

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", parents=[common], help="检查各提供器本地就绪状态")
    doctor.add_argument("--fail-on-unavailable", action="store_true", help="有提供器不可用时返回 1")
    doctor.set_defaults(handler=cmd_doctor)

    tieba_search = sub.add_parser("tieba-search", parents=[common], help="贴吧公开移动端搜索发现")
    tieba_search.add_argument("query")
    tieba_search.add_argument("--pages", type=int, default=None)
    tieba_search.add_argument("--page-size", type=int, default=None)
    tieba_search.add_argument("--output", default="", help="本次运行的输出目录（默认自动生成时间戳目录）")
    tieba_search.set_defaults(handler=cmd_tieba_search)

    tieba_thread = sub.add_parser(
        "tieba-thread", parents=[common], help="读取贴吧主题帖正文楼层与楼中楼（公开页，无需依赖）"
    )
    tieba_thread.add_argument("tid")
    tieba_thread.add_argument("--pages", type=int, default=None)
    tieba_thread.add_argument("--include-nested", action="store_true")
    tieba_thread.add_argument("--title", default="")
    tieba_thread.add_argument(
        "--via", default="public", choices=["public", "aiotieba"], help="public=公开移动页（默认），aiotieba=可选依赖"
    )
    tieba_thread.add_argument("--output", default="")
    tieba_thread.set_defaults(handler=cmd_tieba_thread)

    steam = sub.add_parser("steam-reviews", parents=[common], help="采集任意 Steam app 的公开用户评测")
    steam.add_argument("appid")
    steam.add_argument("--app-name", default="")
    steam.add_argument("--language", default="schinese")
    steam.add_argument("--pages", type=int, default=1)
    steam.add_argument("--page-size", type=int, default=100)
    steam.add_argument("--filter", choices=["recent", "updated", "all"], default="recent")
    steam.add_argument("--output", default="")
    steam.set_defaults(handler=cmd_steam_reviews)

    anysearch = sub.add_parser("anysearch", parents=[common], help="调用已安装的 AnySearch CLI 搜索")
    anysearch.add_argument("query")
    anysearch.add_argument("--max-results", type=int, default=5)
    anysearch.add_argument("--domain", default="")
    anysearch.add_argument("--sub-domain", default="")
    anysearch.add_argument("--output", default="")
    anysearch.set_defaults(handler=cmd_anysearch)

    extract = sub.add_parser("anysearch-extract", parents=[common], help="调用 AnySearch CLI 提取网页正文")
    extract.add_argument("url")
    extract.add_argument("--output", default="")
    extract.set_defaults(handler=cmd_anysearch_extract)

    manifest = sub.add_parser("justone-manifest", parents=[common], help="生成 Just One API 调用清单")
    manifest.add_argument("platform")
    manifest.add_argument("query")
    manifest.add_argument("--comments", action="store_true", help="需要评论或回复层")
    manifest.add_argument("--limit", type=int, default=20)
    manifest.add_argument("--pages", type=int, default=1)
    manifest.add_argument("--page-size", type=int, default=20)
    manifest.add_argument("--inbox", default="", help="原始响应存放目录（写入清单）")
    manifest.add_argument("--out", default="", help="把清单写入文件而不是打印到标准输出")
    manifest.set_defaults(handler=cmd_justone_manifest)

    tools = sub.add_parser("justone-tools", parents=[common], help="列出 MCP 工具与支持的平台（不产生费用）")
    tools.set_defaults(handler=cmd_justone_tools)

    balance = sub.add_parser("justone-balance", parents=[common], help="查看 Just One API 余额与用量（不产生费用）")
    balance.set_defaults(handler=cmd_justone_balance)

    discover = sub.add_parser("justone-discover", parents=[common], help="端点发现 + schema 读取（不产生费用）")
    discover.add_argument("platform")
    discover.add_argument("query")
    discover.add_argument("--intent", default="search", choices=["search", "comments"])
    discover.add_argument("--out", default="", help="把发现结果写入文件")
    discover.set_defaults(handler=cmd_justone_discover)

    collect = sub.add_parser("justone-collect", parents=[common], help="真实采集 B站/知乎内容与评论（按调用计费）")
    collect.add_argument("platform")
    collect.add_argument("query")
    collect.add_argument("--limit", type=int, default=20)
    collect.add_argument("--pages", type=int, default=1)
    collect.add_argument("--vertical", default="", help="知乎结果类型：answer/article/zvideo")
    collect.add_argument("--comments", action="store_true", help="同时采集评论")
    collect.add_argument("--top", type=int, default=2, help="为前 N 条内容采集评论")
    collect.add_argument("--max-calls", type=int, default=0, help="本次运行的最大端点调用次数（预算护栏）")
    collect.add_argument("--output", default="")
    collect.set_defaults(handler=cmd_justone_collect)

    ingest = sub.add_parser("ingest-justone", parents=[common], help="归一化已保存的 Just One API 响应")
    ingest.add_argument("platform")
    ingest.add_argument("path")
    ingest.add_argument("--output", default="")
    ingest.set_defaults(handler=cmd_ingest_justone)

    validate = sub.add_parser("validate-input", parents=[common], help="校验 search-agent 输入包")
    validate.add_argument("path")
    validate.set_defaults(handler=cmd_validate_input)

    verify = sub.add_parser("verify", parents=[common], help="执行只读烟测并写出 verification/latest.json")
    verify.add_argument("--verification-path", default=str(DEFAULT_VERIFICATION_PATH))
    verify.add_argument("--scan-root", default="", help="禁用代码路径扫描目录（默认适配器目录）")
    verify.add_argument("--tieba-query", default=DEFAULT_TIEBA_QUERY)
    verify.add_argument("--anysearch-query", default=DEFAULT_ANYSEARCH_QUERY)
    verify.add_argument("--max-results", type=int, default=3)
    verify.add_argument("--output", default="", help="运行产物的父目录")
    verify.set_defaults(handler=cmd_verify)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except SchemaError as exc:
        print(f"校验失败：{exc}")
        return EXIT_USAGE
    except ValueError as exc:
        print(f"错误：{exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("已中断")
        return 130


# -- helpers ------------------------------------------------------------


def _build_justone_mcp(args: argparse.Namespace, config: Config, cache: Any) -> JustOneMcpProvider:
    """构造发现驱动的 Just One API MCP 提供器（token 只从环境读取）。"""
    client = McpHttpClient(
        config.justone_mcp_url,
        token_env=config.justone_token_env,
        cache=cache,
        max_calls=getattr(args, "max_calls", None) or config.justone_max_calls,
    )
    return JustOneMcpProvider(client=client, discover_limit=config.justone_discover_limit)


def _load_config(args: argparse.Namespace) -> Config:
    path = getattr(args, "config", "") or ""
    return load_config(Path(path) if path else None)


def _build_pipeline(
    args: argparse.Namespace, *, fixed_output: bool = True, config: Optional[Config] = None
) -> ResearchPipeline:
    config = config or _load_config(args)
    offline = bool(getattr(args, "offline", False))
    cache_dir = getattr(args, "cache_dir", "") or str(config.resolve(config.cache_dir))
    cache = None if getattr(args, "no_cache", False) else FileCache(Path(cache_dir))
    skill_dir = getattr(args, "anysearch_skill_dir", "") or config.anysearch_skill_dir
    anysearch = AnySearchProvider(
        command=shlex.split(args.anysearch_command) if args.anysearch_command else None,
        skill_dir=Path(skill_dir) if skill_dir else None,
        cache=cache,
        offline=offline,
        timeout=config.anysearch_timeout_seconds,
        max_output=config.anysearch_max_output,
    )
    tieba = TiebaPublicProvider(
        http=HttpClient(
            provider="tieba",
            cache=cache,
            offline=offline,
            spacing=config.request_spacing_seconds,
            retries=config.max_retries,
            max_bytes=config.max_response_bytes,
        ),
        search_url=config.tieba_search_url,
    )
    output = getattr(args, "output", "") or ""
    output_root = Path(output) if output else config.resolve(config.output_root)
    return ResearchPipeline(
        output_root,
        cache=cache,
        tieba=tieba,
        anysearch=anysearch,
        justone=JustOneBridge(server=config.justone_server),
        justone_mcp=_build_justone_mcp(args, config, cache),
        offline=offline,
        fixed_run_dir=Path(output) if (output and fixed_output) else None,
    )


def _is_offline_miss(run: Any) -> bool:
    return any("offline cache miss" in failure for failure in run.failures)


def _exit_for_run(run: Any) -> int:
    if _is_offline_miss(run):
        return EXIT_OFFLINE_MISS
    if run.status == "failed" or not run.records:
        return EXIT_NO_EVIDENCE
    return EXIT_OK


def _emit_run(run: Any, args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        print(json.dumps(run.summary(), ensure_ascii=False, indent=2))
        return
    print(f"运行目录：{run.run_dir}")
    print(f"状态：{run.status}｜终止原因：{run.termination}｜页数：{run.pages}")
    print(f"记录：{len(run.records)} 条内容、{len(run.comments)} 条评论、{run.thread_count} 个帖子文件")
    print(f"调用：{run.calls} 次（缓存命中 {run.from_cache}）｜覆盖率：{run.coverage.level}")
    if run.failures:
        print("失败：" + "；".join(run.failures))
    if run.gaps:
        print("缺口：" + "；".join(run.gaps))
    print("产物：" + "、".join(sorted(run.written)))


# -- commands -----------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    pipeline = _build_pipeline(args, fixed_output=False)
    statuses = pipeline.doctor()
    verdict = worst_provider_status(statuses)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": now_iso(),
        "verdict": verdict,
        "providers": [status.to_dict() for status in statuses],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"提供器状态（总体：{verdict}）")
        for status in statuses:
            print(f"- {status.provider}: {status.status}｜{status.detail}")
    if args.fail_on_unavailable and verdict == "unavailable":
        return EXIT_NO_EVIDENCE
    return EXIT_OK


def cmd_tieba_search(args: argparse.Namespace) -> int:
    config = _load_config(args)
    pipeline = _build_pipeline(args, config=config)
    pages = args.pages if args.pages is not None else config.tieba_page_limit
    page_size = args.page_size if args.page_size is not None else config.tieba_page_size
    run = pipeline.tieba_search(args.query, pages=pages, page_size=page_size)
    pipeline.write_outputs(run)
    _emit_run(run, args)
    return _exit_for_run(run)


def cmd_tieba_thread(args: argparse.Namespace) -> int:
    config = _load_config(args)
    pipeline = _build_pipeline(args, config=config)
    pages = args.pages if args.pages is not None else config.aiotieba_page_limit
    include_nested = bool(args.include_nested or config.aiotieba_include_nested)
    run = pipeline.tieba_thread(
        args.tid,
        pages=pages,
        include_nested=include_nested,
        title=args.title,
        via=getattr(args, "via", "public"),
    )
    pipeline.write_outputs(run)
    _emit_run(run, args)
    return _exit_for_run(run)


def cmd_anysearch(args: argparse.Namespace) -> int:
    pipeline = _build_pipeline(args)
    run = pipeline.anysearch_search(
        args.query, max_results=args.max_results, domain=args.domain, sub_domain=args.sub_domain
    )
    pipeline.write_outputs(run)
    _emit_run(run, args)
    return _exit_for_run(run)


def cmd_steam_reviews(args: argparse.Namespace) -> int:
    pipeline = _build_pipeline(args)
    run = pipeline.steam_reviews(args.appid, app_name=args.app_name, language=args.language, pages=args.pages, page_size=args.page_size, filter=args.filter)
    pipeline.write_outputs(run)
    _emit_run(run, args)
    return _exit_for_run(run)


def cmd_anysearch_extract(args: argparse.Namespace) -> int:
    pipeline = _build_pipeline(args)
    run = pipeline.anysearch_extract(args.url)
    pipeline.write_outputs(run)
    _emit_run(run, args)
    return _exit_for_run(run)


def cmd_justone_manifest(args: argparse.Namespace) -> int:
    bridge = JustOneBridge(server=_load_config(args).justone_server)
    manifest = bridge.create_manifest(
        args.platform,
        args.query,
        comments=args.comments,
        limit=args.limit,
        page_limit=args.pages,
        page_size=args.page_size,
        output_dir=args.inbox or str(DEFAULT_OUTPUT_ROOT / "justone-inbox"),
    )
    if args.out:
        target = atomic_write_json(Path(args.out), manifest)
        print(f"已写入调用清单：{target}")
        print("提示：端点 ID 必须由会话内的 justoneapi 发现工具在运行时返回，不得写死。")
        return EXIT_OK
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_ingest_justone(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"错误：file not found: {path}")
        return EXIT_NO_EVIDENCE
    payload = path.read_text(encoding="utf-8", errors="replace")
    pipeline = _build_pipeline(args)
    run = pipeline.ingest_justone(
        args.platform, payload, raw_ref=f"raw/justone-{args.platform}-1.json"
    )
    pipeline.write_outputs(run)
    _emit_run(run, args)
    return _exit_for_run(run)


def _mcp_provider(args: argparse.Namespace) -> tuple[JustOneMcpProvider, Config]:
    config = _load_config(args)
    cache_dir = getattr(args, "cache_dir", "") or str(config.resolve(config.cache_dir))
    cache = None if getattr(args, "no_cache", False) else FileCache(Path(cache_dir))
    return _build_justone_mcp(args, config, cache), config


def cmd_justone_tools(args: argparse.Namespace) -> int:
    provider, _config = _mcp_provider(args)
    if not getattr(provider.client, "token_present", False):
        print("错误：JUSTONEAPI_TOKEN 未设置；请先导出会话凭据或使用 justone-manifest/ingest-justone 离线通道")
        return EXIT_NO_EVIDENCE
    tools = provider.client.list_tools()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mcp_url": provider.client.url,
        "tools": [
            {"name": tool.get("name"), "description": (tool.get("description") or "")[:200]}
            for tool in tools
        ],
        "platforms": provider.platforms(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"MCP：{payload['mcp_url']}｜工具 {len(tools)} 个")
        for tool in payload["tools"]:
            print(f"- {tool['name']}：{tool['description']}")
    return EXIT_OK


def cmd_justone_balance(args: argparse.Namespace) -> int:
    provider, _config = _mcp_provider(args)
    balance = provider.balance()
    print(json.dumps(balance, ensure_ascii=False, indent=2) if args.json else
          f"余额：{balance.get('balance')} {balance.get('currency', '')}（可用 {balance.get('available')}）")
    return EXIT_OK if "error" not in balance else EXIT_NO_EVIDENCE


def cmd_justone_discover(args: argparse.Namespace) -> int:
    provider, _config = _mcp_provider(args)
    try:
        payload = provider.resolve_endpoint(args.platform, args.intent)
    except (LookupError, RuntimeError, ValueError) as exc:
        print(f"发现失败：{exc}")
        return EXIT_NO_EVIDENCE
    report = {
        "schema_version": SCHEMA_VERSION,
        "platform": payload["platform"],
        "intent": payload["intent"],
        "discovery_query": payload["discovery_query"],
        "selected_endpoint_id": payload["endpoint_id"],
        "candidates": payload["candidates"],
        "contract_checks": [dict(item) for item in payload.get("contract_checks", ())],
        "contract": payload["contract"],
    }
    if args.out:
        target = atomic_write_json(Path(args.out), report)
        print(f"已写入发现结果：{target}")
        return EXIT_OK
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_justone_collect(args: argparse.Namespace) -> int:
    config = _load_config(args)
    pipeline = _build_pipeline(args, config=config)
    run = pipeline.collect_justone_mcp(
        args.platform,
        args.query,
        limit=args.limit,
        top=args.top,
        comments=args.comments,
        pages=args.pages,
        vertical=args.vertical or None,
    )
    pipeline.write_outputs(run)
    _emit_run(run, args)
    if run.paid_calls and not args.json:
        print(f"计费调用：{run.paid_calls} 次｜实测扣费：{run.estimated_cost}")
    return _exit_for_run(run)


def cmd_validate_input(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"错误：file not found: {path}")
        return EXIT_NO_EVIDENCE
    payload = load_json(path)
    try:
        validate_search_agent_input(payload)
    except SchemaError as exc:
        print(f"校验失败：{exc}")
        return EXIT_NO_EVIDENCE
    print(f"ok：符合 search-agent input.schema.json {SCHEMA_VERSION}")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    pipeline = _build_pipeline(args, fixed_output=False)
    report = run_verification(
        pipeline,
        tieba_query=args.tieba_query,
        anysearch_query=args.anysearch_query,
        max_results=args.max_results,
        scan_root=Path(args.scan_root) if args.scan_root else None,
    )
    target = atomic_write_json(Path(args.verification_path), report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"验证报告：{target}")
        print(f"总体状态：{report['verdict']}")
        for name, check in report["checks"].items():
            print(f"- {name}: {check['status']}")
        if report["gaps"]:
            print("缺口：")
            for gap in report["gaps"]:
                print(f"  * {gap}")
        if report["banned_code_paths"]:
            print("被禁止的代码路径：")
            for finding in report["banned_code_paths"]:
                print(f"  * {finding['path']} ({finding['pattern']})")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
