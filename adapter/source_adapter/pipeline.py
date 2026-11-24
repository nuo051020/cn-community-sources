"""Collection pipeline: providers in, auditable evidence bundle out.

Each run owns a directory containing ``raw/`` (untouched upstream responses),
``records.jsonl`` (normalized evidence), ``threads/<platform>-<id>.json``
(full objects with comment trees), ``search-agent-input.json`` (the existing
knowledge-base contract) and ``run-report.json`` (status, calls, gaps, cost).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .cache import FileCache, atomic_write_bytes, atomic_write_json, atomic_write_jsonl, safe_join
from .export import build_comment_tree, build_search_agent_input, export_search_agent_input
from .http import HttpClient
from .models import CommentRecord, Coverage, ProviderStatus, SourceRecord, merge_records
from .normalize import now_iso, slugify
from .providers.anysearch import AnySearchProvider
from .providers.base import RawPayload
from .providers.justone import JustOneBridge
from .providers.steam import SteamReviewProvider
from .providers.tieba import AiotiebaProvider, TiebaPublicProvider
from .providers.tieba_thread import TiebaThreadProvider

EXCLUSIONS = (
    "不使用 Computer Use 类桌面操控",
    "不使用任何浏览器自动化框架",
    "不绕过验证码、登录墙或访问控制",
    "不执行发帖、点赞、关注、私信等账号操作",
    "不采集或推断敏感个人信息，公开昵称仅用于原文定位",
)

COST_NOTE = "本管线不产生费用；Just One API 的付费批量调用必须由用户单独确认后才在会话内执行"

CAP_GAP_TEMPLATE = "达到页数上限（pages={pages}），上游仍报告更多结果"
REPEATED_PAGE_TEMPLATE = "第 {page} 页未产生新记录，判定为重复页并停止分页"
NO_RECORDS_NOTE = "未采集到任何记录"


@dataclass
class RunResult:
    """Everything one pipeline run produced, before or after writing."""

    run_id: str
    run_dir: Path
    mode: str
    query: str
    records: tuple[SourceRecord, ...] = ()
    comments: tuple[CommentRecord, ...] = ()
    raw_payloads: tuple[RawPayload, ...] = ()
    providers: tuple[dict[str, Any], ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    termination: str = "unknown"
    pages: int = 0
    calls: int = 0
    from_cache: int = 0
    failures: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    status: str = "ok"
    paid_calls: int = 0
    estimated_cost: str = "0"
    cost: dict[str, Any] = field(default_factory=dict)
    audit: tuple[dict[str, Any], ...] = ()
    written: dict[str, str] = field(default_factory=dict)
    thread_count: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "mode": self.mode,
            "query": self.query,
            "status": self.status,
            "termination": self.termination,
            "pages": self.pages,
            "records": len(self.records),
            "comments": len(self.comments),
            "calls": self.calls,
            "from_cache": self.from_cache,
            "threads": self.thread_count,
            "gaps": list(self.gaps),
            "failures": list(self.failures),
            "coverage": self.coverage.to_dict(),
            "paid_calls": self.paid_calls,
            "estimated_cost": self.estimated_cost,
            "cost": dict(self.cost),
            "written": sorted(self.written),
        }

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "run_id": self.run_id,
            "mode": self.mode,
            "query": self.query,
            "status": self.status,
            "termination": self.termination,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "providers": [dict(entry) for entry in self.providers],
            "calls": self.calls,
            "from_cache": self.from_cache,
            "pages": self.pages,
            "records": len(self.records),
            "comments": len(self.comments),
            "threads": self.thread_count,
            "failures": list(self.failures),
            "gaps": list(self.gaps),
            "coverage": self.coverage.to_dict(),
            "limits": [self.coverage.notes] if self.coverage.notes else [],
            "cost": {
                "paid_calls": self.paid_calls,
                "estimated_cost": self.estimated_cost,
                "measured": dict(self.cost),
                "note": COST_NOTE,
            },
            "audit": [dict(entry) for entry in self.audit],
            "written": dict(self.written),
            "exclusions": list(EXCLUSIONS),
        }


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    for value in values or ():
        if value and value not in ordered:
            ordered.append(value)
    return tuple(ordered)


def _compact_timestamp(value: str) -> str:
    return str(value or "").replace("-", "").replace(":", "")


def _sanitize_id(value: str) -> str:
    text = "".join(char if (char.isalnum() or char in "._-") else "-" for char in str(value or ""))
    text = text.strip("-") or "unknown"
    return text[:80]


def _status_dict(provider: str, status: str, detail: str, checked_at: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": status,
        "detail": detail,
        "capabilities": [],
        "checked_at": checked_at,
    }


class ResearchPipeline:
    """Coordinates providers, deduplication, coverage and artifact writing."""

    def __init__(
        self,
        output_root: Path,
        *,
        cache: Optional[FileCache] = None,
        tieba: Optional[TiebaPublicProvider] = None,
        aiotieba: Optional[AiotiebaProvider] = None,
        tieba_thread: Optional[TiebaThreadProvider] = None,
        anysearch: Optional[AnySearchProvider] = None,
        justone: Optional[JustOneBridge] = None,
        justone_mcp: Optional[Any] = None,
        steam: Optional[SteamReviewProvider] = None,
        clock: Callable[[], str] = now_iso,
        run_id_factory: Optional[Callable[[str], str]] = None,
        fixed_run_dir: Optional[Path] = None,
        offline: bool = False,
    ) -> None:
        self.output_root = Path(output_root)
        self.cache = cache
        self.clock = clock
        self.run_id_factory = run_id_factory
        self.fixed_run_dir = Path(fixed_run_dir) if fixed_run_dir else None
        self.offline = offline
        self.tieba = tieba or TiebaPublicProvider(
            http=HttpClient(provider="tieba", cache=cache, offline=offline, spacing=2.0)
        )
        self.aiotieba = aiotieba or AiotiebaProvider()
        # 公开只读的贴吧正文/楼中楼通道（无需 aiotieba），默认启用。
        self.tieba_thread_provider = tieba_thread or TiebaThreadProvider(
            http=HttpClient(provider="tieba-thread", cache=cache, offline=offline, spacing=2.0)
        )
        self.anysearch = anysearch or AnySearchProvider(cache=cache, offline=offline)
        self.justone = justone or JustOneBridge()
        self.justone_mcp = justone_mcp
        self.steam = steam or SteamReviewProvider(http=HttpClient(provider="steam", cache=cache, offline=offline, spacing=2.0))

    # -- capability -----------------------------------------------------

    def doctor(self) -> tuple[ProviderStatus, ...]:
        statuses = [
            self.tieba.doctor(),
            self.aiotieba.doctor(),
            self.tieba_thread_provider.doctor(),
            self.anysearch.doctor(),
            self.justone.doctor(),
            self.steam.doctor(),
        ]
        if self.justone_mcp is not None:
            statuses.append(self.justone_mcp.doctor())
        return tuple(statuses)

    def new_run_dir(self, label: str) -> Path:
        if self.fixed_run_dir is not None:
            return self.fixed_run_dir
        if self.run_id_factory is not None:
            run_id = self.run_id_factory(label)
        else:
            run_id = f"{_compact_timestamp(self.clock())}-{slugify(label)}"
        return self.output_root / run_id

    # -- runs -----------------------------------------------------------

    def tieba_search(self, query: str, *, pages: int = 1, page_size: int = 20) -> RunResult:
        if pages < 1:
            raise ValueError("pages must be >= 1")
        started = self.clock()
        run_dir = self.new_run_dir(f"tieba-{query}")
        records_by_id: dict[str, SourceRecord] = {}
        order: list[str] = []
        payloads: list[RawPayload] = []
        failures: list[str] = []
        gaps: list[str] = []
        calls = 0
        from_cache = 0
        pages_fetched = 0
        termination = "unknown"
        provider_entry: Optional[dict[str, Any]] = None

        for page in range(1, pages + 1):
            raw_ref = f"raw/tieba-public-search-p{page}.json"
            result = self.tieba.search(
                query, page=page, page_size=page_size, page_limit=pages, raw_ref=raw_ref
            )
            calls += result.calls
            from_cache += result.from_cache
            payloads.extend(result.raw_payloads)
            gaps.extend(result.gaps)
            provider_entry = _status_dict(
                result.provider, result.status, result.detail, self.clock()
            )
            pages_fetched += 1
            if result.error:
                failures.append(result.error)
            if result.status == "unavailable":
                termination = "error"
                break
            new_ids: list[str] = []
            for record in result.records:
                existing = records_by_id.get(record.source_id)
                if existing is None:
                    records_by_id[record.source_id] = record
                    order.append(record.source_id)
                    new_ids.append(record.source_id)
                else:
                    records_by_id[record.source_id] = merge_records(existing, record)
            if not new_ids:
                termination = "repeated_page"
                gaps.append(REPEATED_PAGE_TEMPLATE.format(page=page))
                break
            if not result.coverage.upstream_reports_more:
                termination = "exhausted"
                break
            termination = "page_limit"

        records = [records_by_id[source_id] for source_id in order]
        if termination == "page_limit":
            gaps.append(CAP_GAP_TEMPLATE.format(pages=pages))
        coverage = self._aggregate_coverage(records, [], pages_fetched, termination, failures)
        status = self._status_for(records, failures, gaps)
        return RunResult(
            run_id=run_dir.name,
            run_dir=run_dir,
            mode="tieba-search",
            query=query,
            records=tuple(records),
            raw_payloads=tuple(payloads),
            providers=(provider_entry,) if provider_entry else (),
            coverage=coverage,
            termination=termination,
            pages=pages_fetched,
            calls=calls,
            from_cache=from_cache,
            failures=_unique(failures),
            gaps=_unique(gaps),
            started_at=started,
            finished_at=self.clock(),
            status=status,
        )

    def tieba_thread(
        self,
        tid: str,
        *,
        pages: int = 1,
        include_nested: bool = False,
        title: str = "",
        via: str = "public",
    ) -> RunResult:
        """读取贴吧主题帖正文与楼中楼。

        ``via="public"``（默认）走公开移动页，无需额外依赖；``via="aiotieba"``
        使用可选依赖深挖。两者产出同一套记录模型。
        """
        started = self.clock()
        run_dir = self.new_run_dir(f"tieba-thread-{tid}")
        if via == "aiotieba":
            result = self.aiotieba.fetch_thread(
                tid,
                page_limit=pages,
                include_nested=include_nested,
                thread_title=title,
                raw_ref_prefix=f"raw/tieba-thread-{tid}",
            )
        elif via == "public":
            result = self.tieba_thread_provider.fetch_thread(
                tid,
                page_limit=pages,
                include_nested=include_nested,
                thread_title=title,
                max_comment_pages=pages,
                raw_ref_prefix=f"raw/tieba-thread-{tid}",
            )
        else:
            raise ValueError("via must be 'public' or 'aiotieba'")
        return self._from_provider_result(
            result,
            run_dir=run_dir,
            mode="tieba-thread",
            query=tid,
            started_at=started,
            pages=result.coverage.content_pages,
        )

    def anysearch_search(
        self, query: str, *, max_results: int = 5, domain: str = "", sub_domain: str = ""
    ) -> RunResult:
        started = self.clock()
        run_dir = self.new_run_dir(f"anysearch-{query}")
        result = self.anysearch.search(
            query,
            max_results=max_results,
            domain=domain,
            sub_domain=sub_domain,
            raw_ref="raw/anysearch-search-1.md",
        )
        return self._from_provider_result(
            result, run_dir=run_dir, mode="anysearch", query=query, started_at=started, pages=1
        )

    def anysearch_extract(self, url: str) -> RunResult:
        started = self.clock()
        run_dir = self.new_run_dir(f"anysearch-extract-{url}")
        result = self.anysearch.extract(url, raw_ref="raw/anysearch-extract-1.md")
        return self._from_provider_result(
            result, run_dir=run_dir, mode="anysearch-extract", query=url, started_at=started, pages=1
        )

    def steam_reviews(self, appid: str, *, app_name: str = "", language: str = "schinese", pages: int = 1, page_size: int = 100, filter: str = "recent") -> RunResult:
        started = self.clock()
        run_dir = self.new_run_dir(f"steam-{appid}")
        result = self.steam.collect(appid, app_name=app_name, language=language, pages=pages, page_size=page_size, filter=filter)
        return self._from_provider_result(result, run_dir=run_dir, mode="steam-reviews", query=str(appid), started_at=started, pages=result.coverage.content_pages)

    def ingest_justone(self, platform: str, payload: Any, *, raw_ref: str = "") -> RunResult:
        started = self.clock()
        run_dir = self.new_run_dir(f"justone-{platform}")
        result = self.justone.ingest_response(payload, platform, raw_ref=raw_ref)
        run = self._from_provider_result(
            result,
            run_dir=run_dir,
            mode=f"ingest-justone:{platform}",
            query=f"justone:{platform}",
            started_at=started,
            pages=1,
        )
        run.termination = "complete" if result.records else "error"
        return run

    def collect_justone_mcp(
        self,
        platform: str,
        query: str,
        *,
        limit: int = 20,
        top: int = 2,
        comments: bool = False,
        pages: int = 1,
        vertical: Optional[str] = None,
    ) -> RunResult:
        if self.justone_mcp is None:
            raise ValueError("pipeline 未注入 Just One API MCP 提供器（缺少 MCP 客户端）")
        started = self.clock()
        run_dir = self.new_run_dir(f"justone-mcp-{platform}-{query}")
        result = self.justone_mcp.collect(
            platform, query, limit=limit, top=top, comments=comments, pages=pages, vertical=vertical
        )
        run = self._from_provider_result(
            result,
            run_dir=run_dir,
            mode=f"justone-mcp:{platform}",
            query=query,
            started_at=started,
            pages=pages,
        )
        run.paid_calls = result.paid_calls
        run.cost = dict(result.cost or {})
        run.audit = tuple(dict(entry) for entry in result.audit or ())
        spent = run.cost.get("spent")
        if spent is None:
            run.estimated_cost = "unknown"
        else:
            run.estimated_cost = f"{float(spent):.4f} {run.cost.get('currency', 'CNY')}"
        return run

    def _from_provider_result(
        self,
        result: Any,
        *,
        run_dir: Path,
        mode: str,
        query: str,
        started_at: str,
        pages: int,
    ) -> RunResult:
        failures = [result.error] if result.error else []
        status = {
            "ready": self._status_for(result.records, failures, result.gaps),
            "degraded": "degraded",
            "unavailable": "failed",
        }[result.status]
        return RunResult(
            run_id=run_dir.name,
            run_dir=run_dir,
            mode=mode,
            query=query,
            records=tuple(result.records),
            comments=tuple(result.comments),
            raw_payloads=tuple(result.raw_payloads),
            providers=(_status_dict(result.provider, result.status, result.detail, self.clock()),),
            coverage=result.coverage,
            termination=result.termination,
            pages=pages,
            calls=result.calls,
            from_cache=result.from_cache,
            failures=_unique(failures),
            gaps=_unique(result.gaps),
            started_at=started_at,
            finished_at=self.clock(),
            status=status,
            paid_calls=getattr(result, "paid_calls", 0),
            cost=dict(getattr(result, "cost", {}) or {}),
            audit=tuple(dict(entry) for entry in getattr(result, "audit", ()) or ()),
        )

    def _aggregate_coverage(
        self,
        records: Sequence[SourceRecord],
        comments: Sequence[CommentRecord],
        pages: int,
        termination: str,
        failures: Sequence[str],
    ) -> Coverage:
        if not records and not comments:
            return Coverage(level="failed", discovered_pages=pages, notes=NO_RECORDS_NOTE)
        complete = all(record.coverage.level == "complete" for record in records) if records else False
        if failures:
            level = "partial"
        elif termination == "exhausted" and complete:
            level = "complete"
        else:
            level = "partial"
        notes = [record.coverage.notes for record in records if record.coverage.notes]
        return Coverage(
            level=level,
            discovered_pages=pages,
            content_pages=pages if records else 0,
            comment_pages=1 if comments else 0,
            includes_nested_comments=any(
                record.coverage.includes_nested_comments for record in records
            ),
            upstream_reports_more=termination == "page_limit",
            notes="; ".join(dict.fromkeys(notes)),
        )

    def _status_for(
        self, records: Sequence[SourceRecord], failures: Sequence[str], gaps: Sequence[str]
    ) -> str:
        if not records:
            return "failed"
        if failures or gaps:
            return "degraded"
        return "ok"

    # -- writing --------------------------------------------------------

    def write_outputs(
        self,
        run: RunResult,
        *,
        request_id: Optional[str] = None,
        mode: str = "discover",
        constraints: Optional[Mapping[str, Any]] = None,
    ) -> RunResult:
        run_dir = Path(run.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        written: dict[str, str] = {}

        for payload in run.raw_payloads:
            name = self._unique_name(payload.name or "raw/payload.json", written)
            path = safe_join(run_dir, name)
            atomic_write_bytes(path, payload.content)
            written[name] = str(path)

        rows = [record.to_dict() for record in run.records]
        rows.extend(comment.to_source_record().to_dict() for comment in run.comments)
        records_path = safe_join(run_dir, "records.jsonl")
        atomic_write_jsonl(records_path, rows)
        written["records.jsonl"] = str(records_path)

        grouped = self._group_threads(run)
        for key, members in grouped.items():
            platform = key.split(":", 1)[0] if ":" in key else "unknown"
            object_id = key.split(":", 1)[1] if ":" in key else key
            filename = f"threads/{platform}-{_sanitize_id(object_id)}.json"
            path = safe_join(run_dir, filename)
            atomic_write_json(path, self._thread_document(key, members, run))
            written[filename] = str(path)
        run.thread_count = len(grouped)

        input_path = safe_join(run_dir, "search-agent-input.json")
        export_search_agent_input(
            input_path,
            build_search_agent_input(
                run.query or run.run_id,
                run.records,
                request_id=request_id or run.run_id,
                mode=mode,
                constraints=constraints,
                comments=run.comments,
            ),
        )
        written["search-agent-input.json"] = str(input_path)

        run.written = written
        report_path = safe_join(run_dir, "run-report.json")
        atomic_write_json(report_path, run.to_report_dict())
        written["run-report.json"] = str(report_path)
        run.written = written
        return run

    def _unique_name(self, name: str, written: Mapping[str, str]) -> str:
        if name not in written:
            return name
        path = Path(name)
        index = 2
        candidate = name
        while candidate in written:
            candidate = str(path.with_name(f"{path.stem}-{index}{path.suffix}"))
            index += 1
        return candidate

    def _group_threads(
        self, run: RunResult
    ) -> dict[str, tuple[list[SourceRecord], list[CommentRecord]]]:
        order: list[str] = []
        records: dict[str, list[SourceRecord]] = {}
        comments: dict[str, list[CommentRecord]] = {}
        for record in run.records:
            key = record.thread_id or record.source_id
            if key not in records:
                records[key] = []
                order.append(key)
            records[key].append(record)
        for comment in run.comments:
            key = comment.thread_id or comment.parent_id or "unknown"
            if key not in records and key not in comments:
                order.append(key)
            comments.setdefault(key, []).append(comment)
        return {key: (records.get(key, []), comments.get(key, [])) for key in order}

    def _thread_document(
        self,
        key: str,
        members: tuple[list[SourceRecord], list[CommentRecord]],
        run: RunResult,
    ) -> dict[str, Any]:
        records, comments = members
        head = records[0] if records else None
        coverage = head.coverage if head else run.coverage
        retrieved = [record.retrieved_at for record in records if record.retrieved_at]
        retrieved.extend(comment.retrieved_at for comment in comments if comment.retrieved_at)
        return {
            "schema_version": "1.0.0",
            "platform": key.split(":", 1)[0] if ":" in key else head.platform if head else "unknown",
            "thread_id": key,
            "title": head.title if head else "",
            "url": head.url if head else "",
            "collection_method": head.collection_method if head else "",
            "retrieved_at": max(retrieved) if retrieved else "",
            "coverage": coverage.to_dict(),
            "record_count": len(records),
            "comment_count": len(comments),
            "records": [record.to_dict() for record in records],
            "comments": [comment.to_source_record().to_dict() for comment in comments],
            "comment_tree": build_comment_tree(comments, key),
            "run_id": run.run_id,
        }
