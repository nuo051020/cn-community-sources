"""Just One API bridge: local manifests plus response ingestion.

Just One API is reached through the ``justoneapi`` MCP tools owned by the Codex
session, so a plain Python process cannot call it directly. The bridge therefore
works in two phases:

1. :meth:`JustOneBridge.create_manifest` writes an auditable call manifest that
   starts with endpoint discovery; it never hardcodes an endpoint id.
2. :meth:`JustOneBridge.ingest_response` reads the raw response saved by the
   session and normalizes it into the shared evidence model.

The project never stores a token; only the MCP session holds credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..models import (
    RECORD_KINDS,
    merge_records,
    CommentRecord,
    Coverage,
    ProviderStatus,
    SourceRecord,
    stable_source_id,
)
from ..normalize import (
    make_excerpt,
    normalize_whitespace,
    now_iso,
    parse_time,
    redact_secrets,
)
from .base import ProviderResult, RawPayload, clean_text, json_bytes, jsonable, merge_gaps

MANIFEST_VERSION = "1.0.0"
SCHEMA_REPORT_VERSION = "1.0.0"
SUPPORTED_PLATFORMS = ("bilibili", "zhihu")
MAX_ITEMS = 500
MAX_PAGES = 50

DISCOVERY_STEPS = (
    "调用 {server} 端点发现工具，列出平台 {platform} 可用端点及其说明",
    "读取选中端点的 schema，确认必填参数、分页字段与返回结构",
    "把运行时返回的 endpoint_id 与 schema 写入本清单（不得凭记忆写死）",
    "按 manifest 的 pagination 与 call_budget 执行调用，并把原始响应存入 inbox 目录",
    "用 ingest-justone 子命令归一化响应；本工具不保存 token",
)

INGEST_NOTE = "仅归一化上游响应中实际存在的字段，缺失层级保持为空"
_NESTED_PARENT_KEYS = ("parent_id", "parent", "reply_to_id", "reply_to")


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any, *, html: bool = True) -> str:
    if value is None:
        return ""
    raw = str(value)
    if html:
        return clean_text(raw)
    return normalize_whitespace(raw)


def _author_of(item: Any, *paths: Sequence[str]) -> str:
    for path in paths:
        value: Any = item
        for step in path:
            if isinstance(value, Mapping):
                value = value.get(step)
            else:
                value = getattr(value, step, None)
            if value in (None, ""):
                break
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("uname") or value.get("nickname")
        if value not in (None, ""):
            return normalize_whitespace(value)
    return ""


def _load_payload(payload: Any) -> Any:
    """Decode text/bytes JSON payloads; raises ValueError with a redacted reason."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(redact_secrets(f"响应 JSON 解析失败：{exc}")) from exc
    return payload


def unwrap_mcp(value: Any, depth: int = 0) -> Any:
    """Unwrap an MCP ``content[].text`` envelope or a ``result`` wrapper."""
    if depth > 4:
        return value
    if isinstance(value, Mapping):
        content = value.get("content")
        if isinstance(content, list):
            texts = [
                item.get("text")
                for item in content
                if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str)
            ]
            if len(texts) == 1:
                try:
                    return unwrap_mcp(json.loads(texts[0]), depth + 1)
                except json.JSONDecodeError:
                    return texts[0]
            if texts:
                return [unwrap_mcp(text, depth + 1) for text in texts]
        if "result" in value:
            return unwrap_mcp(value["result"], depth + 1)
    return value


PREFERRED_LIST_KEYS = (
    "result",
    "list",
    "items",
    "results",
    "videos",
    "answers",
    "articles",
    "records",
    "notes",
    "comments",
    "replies",
    "data",
    "content",
)


def _find_list(value: Any, depth: int = 0) -> Optional[list[Any]]:
    """Find the record list inside a nested response envelope.

    Real Just One API responses nest records as ``data.data.result`` (B站搜索) or
    ``data.data`` (知乎), so preferred keys are tried first and a generic
    "first list of objects" fallback keeps the mapper working when the envelope
    changes shape.
    """
    if isinstance(value, list):
        if not value or any(isinstance(item, Mapping) for item in value):
            return value
        return None
    if not isinstance(value, Mapping) or depth >= 5:
        return None
    for key in PREFERRED_LIST_KEYS:
        if key in value:
            found = _find_list(value[key], depth + 1)
            if found is not None:
                return found
    for key, item in value.items():
        if str(key) in ("raw", "exp_list", "next_step", "warnings", "hit_columns", "rec_tags"):
            continue  # 信封元数据或重复体，不是记录层
        if isinstance(item, list) and (not item or isinstance(item[0], Mapping)):
            return list(item)
        if isinstance(item, Mapping):
            found = _find_list(item, depth + 1)
            if found is not None:
                return found
    return None


def _raw_bytes(payload: Any) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json_bytes(payload)


def _coverage_for(
    records: Sequence[SourceRecord],
    comments: Sequence[CommentRecord],
    *,
    skipped: int = 0,
) -> Coverage:
    if not records and not comments:
        return Coverage(level="failed", notes="上游响应中未解析出任何内容对象")
    if not records:
        return Coverage(
            level="complete" if not skipped else "partial",
            comment_pages=1,
            includes_nested_comments=any(
                comment.parent_id
                and any(other.source_id == comment.parent_id for other in comments)
                for comment in comments
            ),
            notes="上游响应为评论层，不含内容对象",
        )
    nested = any(
        comment.parent_id
        and any(other.source_id == comment.parent_id for other in comments)
        for comment in comments
    )
    text_layers = sum(1 for record in records if record.full_text)
    level = "complete" if text_layers and not skipped else "partial"
    notes = [INGEST_NOTE]
    if skipped:
        notes.append(f"跳过 {skipped} 条结构异常条目")
    if not comments:
        notes.append("上游响应未包含评论层或调用时未请求评论")
    return Coverage(
        level=level,
        content_pages=1,
        comment_pages=1 if comments else 0,
        includes_nested_comments=nested,
        upstream_reports_more=False,
        notes="; ".join(notes),
    )


@dataclass(frozen=True, slots=True)
class _Mapped:
    records: list[SourceRecord]
    comments: list[CommentRecord]
    skipped: int = 0


class JustOneBridge:
    """Dual-phase bridge for Bilibili and Zhihu collection through MCP."""

    name = "justone"

    def __init__(self, *, clock: Callable[[], str] = now_iso, server: str = "justoneapi") -> None:
        self.clock = clock
        self.server = server

    def doctor(self) -> ProviderStatus:
        return ProviderStatus(
            provider=self.name,
            status="unavailable",
            detail=(
                "Just One API 只能通过 Codex 会话内的 justoneapi MCP 工具调用，"
                "本地进程无法直接执行；请先运行 justone-manifest 生成调用清单，"
                "由会话完成端点发现与采集后，再用 ingest-justone 归一化响应"
            ),
            capabilities=("manifest", "ingest"),
            checked_at=self.clock(),
        )

    # -- phase 1: manifest ---------------------------------------------

    def create_manifest(
        self,
        platform: str,
        query: str,
        *,
        comments: bool = False,
        limit: int = 20,
        page_limit: int = 1,
        page_size: int = 20,
        object_ids: Iterable[Any] = (),
        output_dir: str = "outputs",
        intent: str = "collect",
        time_range: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_platform = str(platform or "").strip().lower()
        if normalized_platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"platform must be one of {SUPPORTED_PLATFORMS}, got {platform!r}")
        keywords = normalize_whitespace(query)
        if not keywords:
            raise ValueError("query must not be blank")
        if not 1 <= int(limit) <= MAX_ITEMS:
            raise ValueError(f"limit must be between 1 and {MAX_ITEMS}")
        if not 1 <= int(page_limit) <= MAX_PAGES:
            raise ValueError(f"page_limit must be between 1 and {MAX_PAGES}")
        if not 1 <= int(page_size) <= 50:
            raise ValueError("page_size must be between 1 and 50")
        if intent not in ("collect", "endpoint_discovery"):
            raise ValueError("intent must be 'collect' or 'endpoint_discovery'")

        normalized_ids = [str(value).strip() for value in object_ids if str(value).strip()]
        detail_calls = int(page_limit) * (2 if comments else 1)
        return {
            "manifest_version": MANIFEST_VERSION,
            "created_at": self.clock(),
            "intent": intent,
            "platform": normalized_platform,
            "requires_mcp": {
                "server": self.server,
                "transport": "codex-mcp",
                "credential": "由会话安全配置持有；本地项目不保存 token",
                "note": "本地 Python 进程不能直接 import 或调用 MCP 工具",
            },
            "endpoint_discovery": {
                "required": True,
                "endpoint_id": None,
                "schema": None,
                "steps": [step.format(server=self.server, platform=normalized_platform) for step in DISCOVERY_STEPS],
            },
            "query": {
                "keywords": keywords,
                "object_ids": normalized_ids,
                "time_range": dict(time_range or {}),
            },
            "pagination": {
                "start_page": 1,
                "page_limit": int(page_limit),
                "page_size": int(page_size),
                "max_items": int(limit),
            },
            "requires_comments": bool(comments),
            "call_budget": {
                "max_calls": detail_calls + 1,
                "paid": True,
                "requires_user_approval": True,
                "note": (
                    "端点发现与 schema 读取合计至少 1 次调用；详情与评论按分页计算。"
                    "未经用户明确确认，不得执行批量收费调用。"
                ),
            },
            "output_contract": {
                "inbox_dir": str(output_dir),
                "filename_pattern": f"{normalized_platform}-<n>.json",
                "format": "MCP 工具原始返回 JSON（可为 content[].text 包装）",
                "ingest_command": (
                    f"python3 -m source_adapter ingest-justone {normalized_platform} <path> --output <dir>"
                ),
            },
            "safety": {
                "anonymous": True,
                "read_only": True,
                "browser_automation": False,
                "access_control_bypass": False,
                "stores_token": False,
            },
        }

    # -- phase 2: ingestion --------------------------------------------

    def ingest_response(
        self,
        payload: Any,
        platform: str,
        *,
        raw_ref: str = "",
        retrieved_at: Optional[str] = None,
        kind: str = "content",
        thread_source_id: str = "",
    ) -> ProviderResult:
        """归一化一条 Just One API 响应。

        ``kind="content"`` 处理内容层（搜索/详情），``kind="comments"`` 处理评论层
        （评论端点的列表本身就是记录）。两者共用同一套记录模型。
        """
        normalized_platform = str(platform or "").strip().lower()
        reference = raw_ref or f"raw/justone-{normalized_platform or 'unknown'}-1.json"
        raw = _raw_bytes(payload)
        try:
            data = _load_payload(payload)
        except ValueError as exc:
            return self._failure(str(exc), raw, reference, "上游响应不是合法 JSON，未产生记录")
        data = unwrap_mcp(data)
        if kind == "comments":
            comment_mapper = {
                "bilibili": self._map_bilibili_comments,
                "zhihu": self._map_zhihu_comments,
            }.get(normalized_platform, self._map_generic_comments)
            mapped = comment_mapper(
                data, reference, retrieved_at or self.clock(), thread_source_id
            )
        else:
            mapper = {
                "bilibili": self._map_bilibili,
                "zhihu": self._map_zhihu,
                "xiaohongshu": self._map_xiaohongshu,
            }.get(normalized_platform, self._map_generic)
            mapped = mapper(data, reference, retrieved_at or self.clock())
        if not mapped.records and not mapped.comments:
            return self._failure(
                "上游响应中未解析出可归一化的内容对象",
                raw,
                reference,
                "响应结构无法识别，未产生记录",
                status="degraded",
            )
        coverage = _coverage_for(mapped.records, mapped.comments, skipped=mapped.skipped)
        records = tuple(replace(record, coverage=coverage) for record in mapped.records)
        comments = tuple(replace(comment, coverage=coverage) for comment in mapped.comments)
        gaps = ("部分上游条目结构异常，已跳过并计入缺口",) if mapped.skipped else ()
        status = "degraded" if mapped.skipped else "ready"
        detail = f"归一化 {len(records)} 条内容、{len(comments)} 条评论"
        if mapped.skipped:
            detail += f"，跳过 {mapped.skipped} 条结构异常条目"
        return ProviderResult(
            provider=self.name,
            status=status,
            records=records,
            comments=comments,
            coverage=coverage,
            raw_payloads=(RawPayload(name=reference, content=raw),),
            detail=detail,
            calls=1,
            termination="complete" if normalized_platform in SUPPORTED_PLATFORMS else "unknown",
            gaps=gaps,
        )

    def _failure(
        self,
        error: str,
        raw: bytes,
        reference: str,
        detail: str,
        *,
        status: str = "degraded",
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            status=status,
            coverage=Coverage(level="failed", notes=detail),
            raw_payloads=(RawPayload(name=reference, content=raw),),
            error=redact_secrets(error),
            detail=detail,
            calls=1,
            termination="error",
            gaps=("Just One API 响应归一化失败，未产生记录",),
        )

    # -- endpoint schema validation ------------------------------------

    def ingest_endpoint_schema(
        self,
        payload: Any,
        *,
        platform: Optional[str] = None,
        raw_ref: str = "",
    ) -> dict[str, Any]:
        """Normalize a saved endpoint-discovery or schema response."""
        data = unwrap_mcp(_load_payload(payload))
        candidates: list[Any] = []
        if isinstance(data, Mapping) and isinstance(data.get("tools"), list):
            candidates = list(data["tools"])
        elif isinstance(data, Mapping) and ("endpoint_id" in data or "name" in data or "inputSchema" in data):
            candidates = [data]
        elif isinstance(data, list):
            candidates = list(data)
        elif isinstance(data, Mapping):
            candidates = [data]
        endpoints = [_normalize_endpoint(item) for item in candidates]
        problems: list[str] = []
        if not endpoints:
            problems.append("响应中没有任何端点定义")
        for index, endpoint in enumerate(endpoints, start=1):
            if not endpoint["endpoint_id"]:
                problems.append(f"endpoint {index}: 缺少 endpoint_id（需由发现工具返回）")
            if not endpoint["parameters_declared"] and not endpoint["parameters"]:
                problems.append(f"endpoint {index}: 缺少参数 schema")
        return {
            "schema_version": SCHEMA_REPORT_VERSION,
            "platform": str(platform or "").strip().lower(),
            "retrieved_at": self.clock(),
            "raw_ref": raw_ref,
            "endpoints": endpoints,
            "validation": {"ok": not problems, "problems": problems},
        }

    # -- platform mappers ----------------------------------------------

    def _map_bilibili(self, payload: Any, raw_ref: str, retrieved_at: str) -> _Mapped:
        items = _find_list(payload) or []
        records: list[SourceRecord] = []
        comments: list[CommentRecord] = []
        skipped = 0
        for item in items:
            if not isinstance(item, Mapping):
                skipped += 1
                continue
            bvid = normalize_whitespace(item.get("bvid"))
            aid = _int(item.get("aid") or item.get("avid"))
            object_id = bvid or (f"av{aid}" if aid else "")
            title = _text(item.get("title") or item.get("name"))
            url = _absolute_url(item.get("url") or item.get("arcurl") or item.get("short_link"))
            if not url and (bvid or aid):
                url = f"https://www.bilibili.com/video/{bvid or f'av{aid}'}"
            if not object_id and not url:
                skipped += 1
                continue
            if not title:
                title = url
            source_id = (
                stable_source_id("bilibili", object_id=object_id)
                if object_id
                else stable_source_id("bilibili", url=url)
            )
            stat = item.get("stat") if isinstance(item.get("stat"), Mapping) else {}
            description = _text(
                item.get("description") or item.get("desc") or item.get("dynamic")
            )
            try:
                record = SourceRecord(
                    source_id=source_id,
                    platform="bilibili",
                    record_kind="video",
                    source_type="video",
                    title=title,
                    url=url,
                    author_display_name=_author_of(item, ("owner",), ("author",), ("member",), ("up",)),
                    published_at=parse_time(item.get("pubdate") or item.get("ctime") or item.get("created")),
                    retrieved_at=retrieved_at,
                    excerpt=make_excerpt(description or title),
                    full_text=description,
                    engagement={
                        "views": _first_int(stat.get("view"), item.get("play"), item.get("view")),
                        "danmaku": _first_int(stat.get("danmaku"), item.get("danmaku"), item.get("video_review")),
                        "replies": _first_int(stat.get("reply"), item.get("review"), item.get("comment_count")),
                        "likes": _first_int(stat.get("like"), item.get("like")),
                        "coins": _first_int(stat.get("coin"), item.get("coin")),
                        "favorites": _first_int(stat.get("favorite"), item.get("favorites")),
                        "shares": _first_int(stat.get("share"), item.get("share")),
                        "duration_seconds": _duration_seconds(item.get("duration")),
                    },
                    media=tuple(
                        url
                        for url in (_absolute_url(item.get("pic") or item.get("cover")),)
                        if url
                    ),
                    collection_method="justone_mcp_bilibili",
                    coverage=Coverage(level="unknown"),
                    raw_ref=raw_ref,
                    extra={
                        "aid": aid,
                        "bvid": bvid,
                        "mid": _int(item.get("mid")),
                        "typename": normalize_whitespace(item.get("typename")),
                        "tags": normalize_whitespace(item.get("tag")),
                        "rank_index": _int(item.get("rank_index")),
                        "text_source_field": "description",
                        "justone_endpoint_id": "<resolved-at-runtime>",
                    },
                )
            except ValueError:
                skipped += 1
                continue
            records.append(record)
            for reply in _iter_replies(item):
                comment = self._bilibili_comment(reply, source_id=source_id, raw_ref=raw_ref, retrieved_at=retrieved_at)
                if comment is None:
                    skipped += 1
                    continue
                comments.append(comment)
        return _Mapped(records=records, comments=comments, skipped=skipped)

    def _bilibili_comment(
        self, reply: Any, *, source_id: str, raw_ref: str, retrieved_at: str
    ) -> Optional[CommentRecord]:
        if not isinstance(reply, Mapping):
            return None
        rpid = normalize_whitespace(reply.get("rpid") or reply.get("rpid_str") or reply.get("id"))
        content = reply.get("content")
        if isinstance(content, Mapping):
            message = content.get("message")
        else:
            message = content or reply.get("message") or reply.get("text")
        text = _text(message)
        if not rpid or not text:
            return None
        parent = reply.get("parent") or reply.get("parent_id")
        parent_id = source_id
        if parent not in (None, 0, "0", ""):
            parent_id = stable_source_id("bilibili", object_id=parent)
        return CommentRecord(
            source_id=stable_source_id("bilibili", object_id=rpid),
            platform="bilibili",
            thread_id=source_id,
            parent_id=parent_id,
            url=f"{source_id_to_url(source_id)}#reply{rpid}",
            author_display_name=_author_of(reply, ("member",), ("author",), ("user",)),
            published_at=parse_time(reply.get("ctime") or reply.get("created") or reply.get("time")),
            retrieved_at=retrieved_at,
            full_text=text,
            engagement={
                "likes": _int(reply.get("like") or reply.get("like_count")),
                "replies": _int(reply.get("rcount") or reply.get("reply_count")),
            },
            collection_method="justone_mcp_bilibili_comments",
            coverage=Coverage(level="unknown"),
            raw_ref=raw_ref,
            extra={"bvid_source": source_id},
        )

    def _map_zhihu(self, payload: Any, raw_ref: str, retrieved_at: str) -> _Mapped:
        items = _find_list(payload) or []
        records: list[SourceRecord] = []
        comments: list[CommentRecord] = []
        skipped = 0
        for item in items:
            if not isinstance(item, Mapping):
                skipped += 1
                continue
            item = _unwrap_search_item(item)
            answer_id = normalize_whitespace(item.get("id") or item.get("answer_id") or item.get("token"))
            question = item.get("question") if isinstance(item.get("question"), Mapping) else {}
            question_id = normalize_whitespace(question.get("id") or item.get("question_id"))
            title = _text(
                question.get("title")
                or question.get("name")
                or item.get("title")
                or item.get("subject")
            )
            body = _text(item.get("content") or item.get("excerpt") or item.get("text"))
            url = _zhihu_public_url(item.get("url") or item.get("link"), answer_id, question_id)
            if not answer_id and not url:
                skipped += 1
                continue
            record_kind = "answer" if (question or item.get("type") in ("answer", None)) else "post"
            if record_kind not in RECORD_KINDS:  # pragma: no cover - defensive only
                record_kind = "post"
            if not title:
                title = make_excerpt(body, 60) or url
            thread_id = stable_source_id("zhihu", object_id=question_id) if question_id else None
            source_id = (
                stable_source_id("zhihu", object_id=answer_id)
                if answer_id
                else stable_source_id("zhihu", url=url)
            )
            parent_id = source_id if not thread_id else thread_id
            try:
                record = SourceRecord(
                    source_id=source_id,
                    platform="zhihu",
                    record_kind=record_kind,
                    source_type="community",
                    title=title,
                    url=url,
                    author_display_name=_author_of(item, ("author",), ("member",), ("user",)),
                    published_at=parse_time(
                        item.get("created_time") or item.get("created") or item.get("updated_time")
                    ),
                    retrieved_at=retrieved_at,
                    excerpt=make_excerpt(_text(item.get("excerpt")) or body),
                    full_text=body,
                    thread_id=thread_id,
                    parent_id=parent_id,
                    engagement={
                        "votes": _int(item.get("voteup_count") or item.get("vote_count")),
                        "comments": _int(item.get("comment_count")),
                        "views": _int(item.get("visits_count")),
                    },
                    media=(str(item.get("image_url")).strip(),) if item.get("image_url") else (),
                    collection_method="justone_mcp_zhihu",
                    coverage=Coverage(level="unknown"),
                    raw_ref=raw_ref,
                    extra={
                        "question_id": question_id,
                        "question_answer_count": _int(question.get("answer_count")),
                        "question_follow_count": _int(question.get("follow_count")),
                        "is_anonymous": bool(item.get("is_anonymous")),
                        "answer_type": normalize_whitespace(item.get("answer_type")),
                        "search_index": _int(item.get("_search_index")),
                        "text_source_field": "content",
                        "justone_endpoint_id": "<resolved-at-runtime>",
                    },
                )
            except ValueError:
                skipped += 1
                continue
            records.append(record)
            for reply in _iter_replies(item):
                comment = self._zhihu_comment(
                    reply,
                    answer_id=answer_id,
                    question_id=question_id,
                    source_id=source_id,
                    raw_ref=raw_ref,
                    retrieved_at=retrieved_at,
                )
                if comment is None:
                    skipped += 1
                    continue
                comments.append(comment)
        return _Mapped(records=records, comments=comments, skipped=skipped)

    def _zhihu_comment(
        self,
        reply: Any,
        *,
        answer_id: str,
        question_id: str,
        source_id: str,
        raw_ref: str,
        retrieved_at: str,
    ) -> Optional[CommentRecord]:
        if not isinstance(reply, Mapping):
            return None
        comment_id = normalize_whitespace(reply.get("id") or reply.get("comment_id"))
        text = _text(reply.get("content") or reply.get("text") or reply.get("message"))
        if not comment_id or not text:
            return None
        parent_value = next(
            (reply.get(key) for key in _NESTED_PARENT_KEYS if reply.get(key) not in (None, "", 0, "0")),
            None,
        )
        parent_id = source_id if parent_value is None else stable_source_id("zhihu", object_id=parent_value)
        if question_id and answer_id:
            url = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}/comment/{comment_id}"
        else:
            url = f"{source_id_to_url(source_id)}/comment/{comment_id}"
        return CommentRecord(
            source_id=stable_source_id("zhihu", object_id=comment_id),
            platform="zhihu",
            thread_id=source_id,
            parent_id=parent_id,
            url=url,
            author_display_name=_author_of(reply, ("author",), ("member",), ("user",)),
            published_at=parse_time(reply.get("created_time") or reply.get("created") or reply.get("time")),
            retrieved_at=retrieved_at,
            full_text=text,
            engagement={
                "likes": _int(reply.get("like_count") or reply.get("vote_count")),
                "replies": _int(reply.get("child_comment_count") or reply.get("reply_count")),
            },
            collection_method="justone_mcp_zhihu_comments",
            coverage=Coverage(level="unknown"),
            raw_ref=raw_ref,
        )

    def _map_bilibili_comments(
        self, payload: Any, raw_ref: str, retrieved_at: str, thread_source_id: str
    ) -> _Mapped:
        """B站评论端点：``data.data.replies`` 就是记录层，含 root/parent 与楼中楼。"""
        comments: list[CommentRecord] = []
        skipped = 0
        for item in _collect_comment_items(payload, platform="bilibili"):
            comment = self._bilibili_comment(
                item,
                source_id=thread_source_id or _oid_thread_id(item),
                raw_ref=raw_ref,
                retrieved_at=retrieved_at,
            )
            if comment is None:
                skipped += 1
                continue
            comments.append(comment)
            for nested in item.get("replies") or ():
                if not isinstance(nested, Mapping):
                    skipped += 1
                    continue
                child = self._bilibili_comment(
                    nested,
                    source_id=thread_source_id or _oid_thread_id(item),
                    raw_ref=raw_ref,
                    retrieved_at=retrieved_at,
                )
                if child is None:
                    skipped += 1
                    continue
                comments.append(child)
        return _Mapped(records=[], comments=comments, skipped=skipped)

    def _map_zhihu_comments(
        self, payload: Any, raw_ref: str, retrieved_at: str, thread_source_id: str
    ) -> _Mapped:
        comments: list[CommentRecord] = []
        skipped = 0
        answer_id = thread_source_id.split(":", 1)[-1] if thread_source_id else ""
        for item in _collect_comment_items(payload, platform="zhihu"):
            comment = self._zhihu_comment(
                item,
                answer_id=answer_id,
                question_id="",
                source_id=thread_source_id or "zhihu:unknown",
                raw_ref=raw_ref,
                retrieved_at=retrieved_at,
            )
            if comment is None:
                skipped += 1
                continue
            comments.append(comment)
        return _Mapped(records=[], comments=comments, skipped=skipped)

    def _map_generic_comments(
        self, payload: Any, raw_ref: str, retrieved_at: str, thread_source_id: str
    ) -> _Mapped:
        comments: list[CommentRecord] = []
        skipped = 0
        for item in _collect_comment_items(payload, platform="other"):
            text = _text(item.get("content") or item.get("text") or item.get("message"))
            comment_id = normalize_whitespace(item.get("id") or item.get("comment_id") or item.get("rpid"))
            if not comment_id or not text:
                skipped += 1
                continue
            parent_value = next(
                (item.get(key) for key in _NESTED_PARENT_KEYS if item.get(key) not in (None, "", 0, "0")),
                None,
            )
            comments.append(
                CommentRecord(
                    source_id=stable_source_id("other", object_id=comment_id),
                    platform="other",
                    thread_id=thread_source_id or "other:unknown",
                    parent_id=thread_source_id or "other:unknown"
                    if parent_value is None
                    else stable_source_id("other", object_id=parent_value),
                    url=str(item.get("url") or ""),
                    author_display_name=_author_of(item, ("author",), ("user",), ("member",)),
                    published_at=parse_time(item.get("created_time") or item.get("ctime")),
                    retrieved_at=retrieved_at,
                    full_text=text,
                    collection_method="justone_mcp_generic_comments",
                    coverage=Coverage(level="unknown"),
                    raw_ref=raw_ref,
                )
            )
        return _Mapped(records=[], comments=comments, skipped=skipped)

    def _map_xiaohongshu(self, payload: Any, raw_ref: str, retrieved_at: str) -> _Mapped:
        """小红书搜索：``data.items[].note``，含正文 desc、互动与 xsec_token。"""
        items = _find_list(payload) or []
        records: list[SourceRecord] = []
        skipped = 0
        for item in items:
            if not isinstance(item, Mapping):
                skipped += 1
                continue
            note = _unwrap_search_item(item)
            note_id = normalize_whitespace(note.get("id") or note.get("note_id"))
            body = _text(note.get("desc") or note.get("description"))
            title = _text(note.get("title")) or make_excerpt(body, 60)
            if not title:
                # 既无标题也无正文的笔记没有任何可引用证据：跳过并计入缺口。
                skipped += 1
                continue
            if not note_id and not body:
                skipped += 1
                continue
            token = normalize_whitespace(note.get("xsec_token"))
            url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
            if url and token:
                url = f"{url}?xsec_token={token}"
            user = note.get("user") if isinstance(note.get("user"), Mapping) else {}
            try:
                record = SourceRecord(
                    source_id=(
                        stable_source_id("xiaohongshu", object_id=note_id)
                        if note_id
                        else stable_source_id("xiaohongshu", title=title)
                    ),
                    platform="xiaohongshu",
                    record_kind="post",
                    source_type="community",
                    title=title,
                    url=url,
                    author_display_name=normalize_whitespace(
                        user.get("nickname") or user.get("nick_name") or note.get("author")
                    ),
                    published_at=parse_time(note.get("timestamp") or note.get("time")),
                    retrieved_at=retrieved_at,
                    excerpt=make_excerpt(note.get("title") or body),
                    full_text=body,
                    engagement={
                        "likes": _int(note.get("liked_count") or note.get("like_count")),
                        "comments": _int(note.get("comments_count") or note.get("comment_count")),
                        "collected": _int(note.get("collected_count")),
                        "shares": _int(note.get("shared_count")),
                    },
                    media=_xhs_images(note.get("images_list") or note.get("image_list")),
                    collection_method="justone_mcp_xiaohongshu",
                    coverage=Coverage(level="unknown"),
                    raw_ref=raw_ref,
                    extra={
                        "note_type": normalize_whitespace(note.get("type")),
                        "user_id": normalize_whitespace(user.get("userid") or user.get("user_id")),
                        "search_index": _int(note.get("_search_index")),
                        "text_source_field": "desc",
                        "justone_endpoint_id": "<resolved-at-runtime>",
                    },
                )
            except ValueError:
                skipped += 1
                continue
            records.append(record)
        return _Mapped(records=records, comments=[], skipped=skipped)

    def _map_generic(self, payload: Any, raw_ref: str, retrieved_at: str) -> _Mapped:
        items = _find_list(payload) or []
        records: list[SourceRecord] = []
        skipped = 0
        for item in items:
            if not isinstance(item, Mapping):
                skipped += 1
                continue
            item = _unwrap_search_item(item)
            object_id = normalize_whitespace(
                item.get("id") or item.get("object_id") or item.get("token") or item.get("note_id")
            )
            url = str(item.get("url") or item.get("link") or item.get("share_url") or "").strip()
            if not object_id and not url:
                skipped += 1
                continue
            question = item.get("question") if isinstance(item.get("question"), Mapping) else {}
            body = _text(item.get("content") or item.get("text") or item.get("desc") or item.get("excerpt"))
            title = _text(
                item.get("title") or item.get("subject") or item.get("name") or question.get("title")
            )
            if not title:
                title = make_excerpt(body, 60) or url
            records.append(
                SourceRecord(
                    source_id=(
                        stable_source_id("other", object_id=object_id)
                        if object_id
                        else stable_source_id("other", url=url)
                    ),
                    platform="other",
                    record_kind="post",
                    source_type="other",
                    title=title,
                    url=url,
                    author_display_name=_author_of(item, ("author",), ("user",), ("member",)),
                    published_at=parse_time(
                        item.get("created_time") or item.get("create_time") or item.get("time")
                    ),
                    retrieved_at=retrieved_at,
                    excerpt=make_excerpt(_text(item.get("excerpt")) or body),
                    full_text=body,
                    collection_method="justone_mcp_generic",
                    coverage=Coverage(level="unknown"),
                    raw_ref=raw_ref,
                    extra={"justone_endpoint_id": "<resolved-at-runtime>"},
                )
            )
        return _Mapped(records=records, comments=[], skipped=skipped)


def _iter_replies(item: Mapping[str, Any]) -> tuple[Any, ...]:
    for key in ("replies", "comments", "top_replies", "reply_list", "comment_list"):
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            return tuple(value)
    return ()


def source_id_to_url(source_id: str) -> str:
    """Rebuild a public URL from a ``platform:id`` source id."""
    platform, _, object_id = str(source_id or "").partition(":")
    if platform == "bilibili":
        return f"https://www.bilibili.com/video/{object_id}"
    if platform == "zhihu":
        return f"https://www.zhihu.com/answer/{object_id}"
    return ""


def _normalize_endpoint(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, Mapping):
        return {
            "endpoint_id": "",
            "name": "",
            "description": "",
            "parameters": {},
            "required_parameters": [],
            "parameters_declared": False,
            "schema": {},
        }
    schema = (
        tool.get("inputSchema")
        or tool.get("input_schema")
        or tool.get("parameters")
        or tool.get("parameter_schema")
        or tool.get("schema")
        or {}
    )
    if not isinstance(schema, Mapping):
        schema = {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return {
        "endpoint_id": normalize_whitespace(tool.get("endpoint_id") or tool.get("id") or tool.get("name")),
        "name": normalize_whitespace(tool.get("name") or tool.get("title")),
        "description": normalize_whitespace(tool.get("description") or tool.get("desc")),
        "parameters": {
            str(key): (str(value.get("type")) if isinstance(value, Mapping) and value.get("type") else "")
            for key, value in properties.items()
        },
        "required_parameters": [str(item) for item in required],
        "parameters_declared": bool(schema),
        "schema": jsonable(schema),
    }


# --------------------------------------------------------------------------
# 发现驱动的 MCP 直连提供器
# --------------------------------------------------------------------------

# MCP 服务端自身的发现工作流工具名（不是数据端点 ID）。数据端点 ID 一律由
# search_endpoints 在运行时返回，代码里不得出现任何具体数据端点 ID。
DISCOVERY_TOOL = "search_endpoints"
SCHEMA_TOOL = "get_endpoint_schema"
CALL_TOOL = "call_endpoint"
BALANCE_TOOL = "get_account_balance"
PLATFORMS_TOOL = "list_platforms"

KEYWORD_PARAM_NAMES = frozenset(
    {"keyword", "keywords", "query", "q", "word", "search_word", "kw", "search_keyword"}
)
PAGE_PARAM_NAMES = frozenset({"page", "pn", "page_num", "page_no", "pageindex", "page_index"})
OFFSET_PARAM_NAMES = frozenset({"offset", "start", "begin", "skip"})
CURSOR_PARAM_NAMES = frozenset({"cursor", "next", "next_cursor"})
VERTICAL_PARAM_NAMES = frozenset({"vertical", "type", "result_type", "search_type", "sub_type"})
SUBJECT_ID_PARAM_NAMES = frozenset(
    {
        "aid",
        "avid",
        "bvid",
        "answer_id",
        "question_id",
        "note_id",
        "article_id",
        "post_id",
        "object_id",
        "item_id",
        "id",
        "oid",
    }
)

ID_PARAM_NAMES = frozenset(
    {
        "aid",
        "avid",
        "bvid",
        "cid",
        "answer_id",
        "question_id",
        "comment_id",
        "note_id",
        "article_id",
        "mid",
        "uid",
        "id",
        "object_id",
        "item_id",
        "post_id",
    }
)

# 平台意图：用自然语言交给端点发现工具，用正负关键词给候选排序。
PLATFORM_INTENTS: dict[str, dict[str, dict[str, Any]]] = {
    "bilibili": {
        "search": {
            "query": "按关键词搜索哔哩哔哩视频列表",
            "positive": ("搜索", "search", "视频", "video"),
            "negative": (
                "用户", "user", "弹幕", "danmu", "danmaku", "字幕", "caption",
                "评论", "comment", "合集", "collection", "详情", "detail",
                "分享", "share", "链接", "link", "重定向", "redirect",
            ),
        },
        "comments": {
            "query": "获取哔哩哔哩视频的评论与回复",
            "positive": ("评论", "comment", "回复", "reply"),
            "negative": ("弹幕", "danmu", "danmaku", "字幕", "caption", "用户", "user"),
        },
    },
    "zhihu": {
        "search": {
            "query": "按关键词搜索知乎的回答与文章",
            "positive": ("搜索", "search", "回答", "answer", "内容"),
            "negative": (
                "评论", "comment", "用户", "user", "专栏列表", "column",
                "详情", "detail", "资料", "profile",
            ),
        },
        "comments": {
            "query": "获取知乎回答的评论",
            "positive": ("评论", "comment"),
            "negative": (
                "用户", "user", "专栏", "column", "文章", "article",
                "回复", "repl", "子评论", "child",
            ),
        },
    },
    "xiaohongshu": {
        "search": {
            "query": "按关键词搜索小红书笔记",
            "positive": ("搜索", "search", "笔记", "note"),
            "negative": (
                "用户", "user", "评论", "comment", "关键词建议", "suggest",
                "详情", "detail", "推荐", "recommend",
            ),
        },
        "comments": {
            "query": "获取小红书笔记的评论",
            "positive": ("评论", "comment"),
            "negative": ("用户", "user", "笔记搜索", "search"),
        },
    },
    "weibo": {
        "search": {
            "query": "按关键词搜索微博帖子",
            "positive": ("搜索", "search", "微博", "weibo", "帖子", "post"),
            "negative": ("用户", "profile", "评论", "comment", "详情", "detail"),
        },
        "comments": {
            "query": "获取微博帖子的评论",
            "positive": ("评论", "comment"),
            "negative": ("用户", "profile"),
        },
    },
}


def _iter_endpoint_candidates(payload: Any) -> list[Mapping[str, Any]]:
    """Pull endpoint candidates out of the several envelope shapes."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("endpoints", "data", "results", "items", "candidates"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                nested = _iter_endpoint_candidates(value)
                if nested:
                    return nested
    return []


def _task_text(candidate: Mapping[str, Any]) -> str:
    parts = [
        str(candidate.get("endpoint_id") or ""),
        str(candidate.get("name") or ""),
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        " ".join(str(item) for item in (candidate.get("use_cases") or ())),
        " ".join(str(item) for item in (candidate.get("search_aliases") or ())),
    ]
    return " ".join(parts).lower()


class JustOneMcpProvider:
    """Discovery-driven B站/知乎/小红书/微博 collection through the MCP server.

    Flow per intent: ``search_endpoints``（发现）→ ``get_endpoint_schema``（读取契约）
    → ``call_endpoint``（采集）→ 复用 :class:`JustOneBridge` 归一化。端点 ID 全部来自
    运行时发现，参数按 schema 角色映射；缺必需参数时只记录缺口，不猜测取值。
    """

    name = "justone-mcp"

    def __init__(
        self,
        *,
        client: Any = None,
        bridge: Optional[JustOneBridge] = None,
        clock: Callable[[], str] = now_iso,
        discover_limit: int = 8,
    ) -> None:
        self.client = client
        self.bridge = bridge or JustOneBridge(clock=clock)
        self.clock = clock
        self.discover_limit = discover_limit

    # -- 能力 -----------------------------------------------------------

    def doctor(self) -> ProviderStatus:
        if self.client is None:
            return ProviderStatus(
                provider=self.name,
                status="unavailable",
                detail="未注入 MCP 客户端；请设置 JUSTONEAPI_TOKEN 后重试",
                capabilities=(),
                checked_at=self.clock(),
            )
        if not getattr(self.client, "token_present", False):
            return ProviderStatus(
                provider=self.name,
                status="unavailable",
                detail="JUSTONEAPI_TOKEN 未设置：B站/知乎/小红书需要会话级 MCP 凭据",
                capabilities=(),
                checked_at=self.clock(),
            )
        return ProviderStatus(
            provider=self.name,
            status="ready",
            detail=(
                "Just One API MCP 可用：端点发现 → schema → 调用；"
                "数据端点 ID 全部由运行时发现，未写死在代码里"
            ),
            capabilities=("discover", "schema", "search", "comments", "balance"),
            checked_at=self.clock(),
        )

    def intent(self, platform: str, intent_name: str) -> dict[str, Any]:
        platform_key = str(platform or "").strip().lower()
        spec = PLATFORM_INTENTS.get(platform_key, {}).get(str(intent_name))
        if spec is None:
            raise ValueError(
                f"unsupported platform/intent: {platform_key}/{intent_name}"
                f"（可用平台：{sorted(PLATFORM_INTENTS)}；意图：search, comments）"
            )
        return spec

    # -- 发现与契约 -----------------------------------------------------

    def discover_candidates(self, platform: str, intent_name: str, *, limit: int = 0) -> dict[str, Any]:
        spec = self.intent(platform, intent_name)
        limit = limit or self.discover_limit
        arguments = {"query": spec["query"], "platform": str(platform).strip().lower(), "limit": limit}
        call = self.client.call_tool(DISCOVERY_TOOL, arguments)
        if not call.ok:
            raise RuntimeError(call.error or f"{DISCOVERY_TOOL} 调用失败")
        candidates = _iter_endpoint_candidates(call.data)
        ranked = self.rank_candidates(candidates, spec)
        return {
            "platform": str(platform).strip().lower(),
            "intent": intent_name,
            "query": spec["query"],
            "arguments": arguments,
            "candidates": [
                {
                    "endpoint_id": str(item.get("endpoint_id") or ""),
                    "score": score,
                    "description": str(item.get("description") or "")[:200],
                }
                for item, score in ranked
            ],
            "from_cache": call.from_cache,
        }

    @staticmethod
    def rank_candidates(
        candidates: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
    ) -> list[tuple[Mapping[str, Any], int]]:
        scored: list[tuple[Mapping[str, Any], int]] = []
        for candidate in candidates:
            text = _task_text(candidate)
            if not str(candidate.get("endpoint_id") or "").strip():
                continue
            score = 0
            for word in spec.get("positive", ()):  # type: ignore[union-attr]
                if str(word).lower() in text:
                    score += 2
            for word in spec.get("negative", ()):  # type: ignore[union-attr]
                if str(word).lower() in text:
                    score -= 3
            if score > 0:
                scored.append((candidate, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    @staticmethod
    def normalize_contract(payload: Any) -> dict[str, Any]:
        data = payload
        if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
            data = payload["data"]
        if not isinstance(data, Mapping):
            return {"endpoint_id": "", "params": [], "pagination": {}, "required": []}
        raw_params = data.get("params") or data.get("parameters") or []
        params: list[dict[str, Any]] = []
        if isinstance(raw_params, list):
            for item in raw_params:
                if not isinstance(item, Mapping):
                    continue
                params.append(
                    {
                        "name": str(item.get("name") or item.get("api_name") or ""),
                        "api_name": str(item.get("api_name") or item.get("name") or ""),
                        "required": bool(item.get("required")),
                        "type": str(item.get("type") or "string"),
                        "default": item.get("default"),
                        "enum": list(item.get("enum") or ()),
                        "description": str(item.get("description") or ""),
                    }
                )
        pagination = data.get("pagination") if isinstance(data.get("pagination"), Mapping) else {}
        return {
            "endpoint_id": str(data.get("endpoint_id") or ""),
            "title": str(data.get("title") or ""),
            "description": str(data.get("description") or ""),
            "method": str(data.get("method") or "GET"),
            "path": str(data.get("path") or ""),
            "params": params,
            "required": [param["name"] for param in params if param["required"]],
            "pagination": {
                "type": str(pagination.get("type") or ""),
                "params": [str(item) for item in (pagination.get("params") or ())],
            },
        }

    def read_contract(self, endpoint_id: str) -> dict[str, Any]:
        call = self.client.call_tool(SCHEMA_TOOL, {"endpoint_id": endpoint_id})
        if not call.ok:
            raise RuntimeError(call.error or f"{SCHEMA_TOOL} 调用失败")
        contract = self.normalize_contract(call.data)
        if not contract["endpoint_id"]:
            contract["endpoint_id"] = endpoint_id
        return contract

    @staticmethod
    def validate_contract(contract: Mapping[str, Any], intent_name: str) -> tuple[bool, str]:
        """用 schema 契约做语义校验：搜索必须收关键词，评论必须收对象 ID。"""
        params = list(contract.get("params") or ())
        names = [str(param.get("name") or "").lower() for param in params]
        keyword_like = [
            name for name in names if name in KEYWORD_PARAM_NAMES or "keyword" in name
        ]
        id_like = [
            name for name in names if name in ID_PARAM_NAMES or name.endswith("_id")
        ]
        if intent_name == "search":
            if not keyword_like:
                return False, "端点不接受关键词参数，不是搜索端点"
            required = [str(item).lower() for item in contract.get("required") or ()]
            if required and not any(name in keyword_like for name in required):
                return False, "关键词参数不是必需参数，疑似详情类端点"
            return True, "含关键词参数"
        if intent_name == "comments":
            subject_like = [name for name in names if name in SUBJECT_ID_PARAM_NAMES]
            if not subject_like:
                return False, "端点只接受评论级 ID 或不接受对象 ID，无法按内容对象取评论"
            required = [str(item).lower() for item in contract.get("required") or ()]
            if required and not any(name in subject_like for name in required):
                return False, "对象 ID 不是必需参数"
            return True, "含对象级 ID 参数"
        return True, "无额外契约要求"

    def resolve_endpoint(
        self, platform: str, intent_name: str, *, limit: int = 0, verify_top: int = 3
    ) -> dict[str, Any]:
        """发现候选 → 逐个读取 schema → 用契约语义挑选，全部过程留痕。"""
        discovery = self.discover_candidates(platform, intent_name, limit=limit)
        ranked = discovery["candidates"]
        if not ranked:
            raise LookupError(
                f"端点发现未返回 {platform}/{intent_name} 的可用候选："
                f"query={discovery['query']!r}（不猜测端点 ID）"
            )
        checks: list[dict[str, Any]] = []
        selected: Optional[dict[str, Any]] = None
        fallback: Optional[dict[str, Any]] = None
        for candidate in ranked[: max(1, verify_top)]:
            contract = self.read_contract(candidate["endpoint_id"])
            ok, reason = self.validate_contract(contract, intent_name)
            checks.append(
                {
                    "endpoint_id": candidate["endpoint_id"],
                    "score": candidate["score"],
                    "contract_ok": ok,
                    "reason": reason,
                    "required": list(contract.get("required") or ()),
                }
            )
            if ok and selected is None:
                selected = {**candidate, "contract": contract}
            elif fallback is None:
                fallback = {**candidate, "contract": contract}
        if selected is None:
            # 契约校验全部不通过时退化为文本最高分，但必须留下明确缺口。
            best = ranked[0]
            contract = self.read_contract(best["endpoint_id"])
            selected = {**best, "contract": contract}
            checks.append(
                {
                    "endpoint_id": best["endpoint_id"],
                    "score": best["score"],
                    "contract_ok": False,
                    "reason": "所有候选都未通过契约语义校验，退化为文本最高分（需人工复核）",
                    "required": list(contract.get("required") or ()),
                }
            )
        return {
            "platform": discovery["platform"],
            "intent": intent_name,
            "endpoint_id": selected["endpoint_id"],
            "score": selected["score"],
            "discovery_query": discovery["query"],
            "candidates": ranked,
            "contract": selected["contract"],
            "contract_checks": checks,
            "from_cache": discovery["from_cache"],
        }

    # -- 参数映射 -------------------------------------------------------

    @staticmethod
    def build_params(
        contract: Mapping[str, Any],
        *,
        keyword: Optional[str] = None,
        object_id: Optional[str] = None,
        page: Optional[int] = None,
        offset: Optional[int] = None,
        cursor: Optional[str] = None,
        vertical: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> tuple[dict[str, Any], list[str]]:
        params: dict[str, Any] = {}
        missing: list[str] = []
        id_used = False
        for param in contract.get("params", ()):
            name = str(param.get("name") or "")
            lowered = name.lower()
            enum = [str(item) for item in (param.get("enum") or ())]
            value: Any = None
            if lowered in KEYWORD_PARAM_NAMES or "keyword" in lowered:
                value = keyword
            elif lowered in VERTICAL_PARAM_NAMES and enum:
                if vertical and str(vertical) in enum:
                    value = str(vertical)
            elif lowered in ID_PARAM_NAMES or lowered.endswith("_id"):
                if not id_used and object_id is not None:
                    value = str(object_id)
                    id_used = True
            elif lowered in PAGE_PARAM_NAMES:
                value = None if page is None else str(int(page))
            elif lowered in OFFSET_PARAM_NAMES:
                value = None if offset is None else str(int(offset))
            elif lowered in CURSOR_PARAM_NAMES:
                value = cursor
            if extra and name in extra:
                value = extra[name]
            if value in (None, ""):
                if param.get("required"):
                    missing.append(name)
                continue
            params[name] = value
        if extra:
            for key, value in extra.items():
                if key not in params and value not in (None, ""):
                    params[key] = value
        return params, missing

    # -- 采集 -----------------------------------------------------------

    def _call_endpoint(
        self, resolved: Mapping[str, Any], params: Mapping[str, Any], *, intent: str
    ) -> Any:
        return self.client.call_tool(
            CALL_TOOL,
            {"endpoint_id": resolved["endpoint_id"], "params": dict(params)},
        )

    def _discovery_audit(self, resolved: Mapping[str, Any]) -> list[dict[str, Any]]:
        contract = resolved.get("contract") or {}
        return [
            {
                "step": "discover",
                "platform": resolved.get("platform"),
                "intent": resolved.get("intent"),
                "query": resolved.get("discovery_query"),
                "endpoint_id": resolved.get("endpoint_id"),
                "score": resolved.get("score"),
                "candidates": [
                    {"endpoint_id": item["endpoint_id"], "score": item["score"]}
                    for item in resolved.get("candidates", ())
                ],
            },
            {
                "step": "schema",
                "endpoint_id": resolved.get("endpoint_id"),
                "required": list(contract.get("required") or ()),
                "pagination": dict(contract.get("pagination") or {}),
                "path": contract.get("path", ""),
                "contract_checks": [dict(item) for item in resolved.get("contract_checks", ())],
            },
        ]

    def search(
        self,
        platform: str,
        query: str,
        *,
        limit: int = 20,
        page: int = 1,
        vertical: Optional[str] = None,
        raw_ref: str = "",
    ) -> ProviderResult:
        if not str(query or "").strip():
            raise ValueError("query must not be blank")
        if not 1 <= int(limit) <= MAX_ITEMS:
            raise ValueError(f"limit must be between 1 and {MAX_ITEMS}")
        reference = raw_ref or f"raw/justone-mcp-{platform}-search-1.json"
        try:
            resolved = self.resolve_endpoint(platform, "search")
        except (LookupError, RuntimeError) as exc:
            return self._failed(str(exc), reference, "端点发现失败，未产生记录")
        audit = self._discovery_audit(resolved)
        params, missing = self.build_params(
            resolved["contract"], keyword=query, page=page, vertical=vertical, extra={"limit": limit} if _accepts(resolved["contract"], "limit") else None
        )
        if missing:
            return self._failed(
                f"端点 {resolved['endpoint_id']} 的必需参数 {missing} 无法从 schema 映射",
                reference,
                "schema 参数映射不完整，未发起调用",
                audit=audit,
                gaps=(f"{platform} 搜索端点的必需参数无法映射：{missing}",),
            )
        call = self._call_endpoint(resolved, params, intent="search")
        audit.append(
            {
                "step": "call",
                "endpoint_id": resolved["endpoint_id"],
                "intent": "search",
                "params": dict(params),
                "ok": bool(call.ok),
                "from_cache": bool(call.from_cache),
                "error": call.error,
            }
        )
        if not call.ok:
            return self._failed(
                call.error or "调用失败", reference, "上游调用失败，未产生记录", audit=audit
            )
        mapped = self.bridge.ingest_response(call.data, platform, raw_ref=reference)
        return ProviderResult(
            provider=self.name,
            status=mapped.status,
            records=mapped.records,
            comments=mapped.comments,
            coverage=mapped.coverage,
            raw_payloads=(RawPayload(name=reference, content=json_bytes(call.data)),),
            error=mapped.error,
            detail=f"搜索端点 {resolved['endpoint_id']}：{mapped.detail}",
            calls=1,
            from_cache=1 if call.from_cache else 0,
            termination="complete" if mapped.records else "error",
            gaps=mapped.gaps,
            audit=tuple(audit),
            paid_calls=0 if call.from_cache else 1,
            cost={},
        )

    def comments(
        self,
        platform: str,
        object_id: str,
        *,
        raw_ref: str = "",
        thread_source_id: str = "",
        page_offset: Optional[int] = None,
    ) -> ProviderResult:
        if not str(object_id or "").strip():
            raise ValueError("object_id must not be blank")
        reference = raw_ref or f"raw/justone-mcp-{platform}-comments-{object_id}.json"
        try:
            resolved = self.resolve_endpoint(platform, "comments")
        except (LookupError, RuntimeError) as exc:
            return self._failed(str(exc), reference, "评论端点发现失败")
        audit = self._discovery_audit(resolved)
        params, missing = self.build_params(
            resolved["contract"], object_id=object_id, offset=page_offset
        )
        if missing:
            return self._failed(
                f"评论端点 {resolved['endpoint_id']} 缺少必需参数 {missing}",
                reference,
                "评论端点参数不完整，未发起调用",
                audit=audit,
                gaps=(f"{platform} 评论端点需要 {missing}",),
            )
        call = self._call_endpoint(resolved, params, intent="comments")
        audit.append(
            {
                "step": "call",
                "endpoint_id": resolved["endpoint_id"],
                "intent": "comments",
                "object_id": str(object_id),
                "params": dict(params),
                "ok": bool(call.ok),
                "from_cache": bool(call.from_cache),
                "error": call.error,
            }
        )
        if not call.ok:
            return self._failed(call.error or "调用失败", reference, "评论调用失败", audit=audit)
        mapped = self.bridge.ingest_response(
            call.data,
            platform,
            raw_ref=reference,
            kind="comments",
            thread_source_id=thread_source_id
            or stable_source_id(str(platform).strip().lower(), object_id=object_id),
        )
        audit.append({"step": "pagination", "next_step": _next_step(call.data)})
        return ProviderResult(
            provider=self.name,
            status=mapped.status,
            records=mapped.records,
            comments=mapped.comments,
            coverage=mapped.coverage,
            raw_payloads=(RawPayload(name=reference, content=json_bytes(call.data)),),
            error=mapped.error,
            detail=f"评论端点 {resolved['endpoint_id']}：{mapped.detail}",
            calls=1,
            from_cache=1 if call.from_cache else 0,
            termination="complete" if (mapped.records or mapped.comments) else "error",
            gaps=mapped.gaps,
            audit=tuple(audit),
            paid_calls=0 if call.from_cache else 1,
            cost={},
        )

    def balance(self) -> dict[str, Any]:
        if self.client is None or not getattr(self.client, "token_present", False):
            return {"error": "JUSTONEAPI_TOKEN 未设置"}
        call = self.client.call_tool(BALANCE_TOOL, {}, use_cache=False)
        if not call.ok:
            return {"error": call.error}
        payload = call.data if isinstance(call.data, Mapping) else {}
        if isinstance(payload.get("data"), Mapping):
            payload = payload["data"]
        balance = _float(payload.get("balance"))
        available = _float(payload.get("availableBalance"))
        return {
            "balance": balance,
            "available": available if available is not None else balance,
            "currency": str(payload.get("currency") or "CNY"),
        }

    def platforms(self) -> dict[str, Any]:
        if self.client is None:
            return {"error": "no MCP client"}
        call = self.client.call_tool(PLATFORMS_TOOL, {})
        return call.data if isinstance(call.data, Mapping) else {"error": call.error or "unexpected payload"}

    def collect(
        self,
        platform: str,
        query: str,
        *,
        limit: int = 20,
        top: int = 2,
        comments: bool = False,
        page: int = 1,
        vertical: Optional[str] = None,
        pages: int = 1,
    ) -> ProviderResult:
        """搜索 + 可选评论，带回端点发现、调用与余额审计。"""
        if top < 0:
            raise ValueError("top must be >= 0")
        if not 1 <= int(pages) <= MAX_PAGES:
            raise ValueError(f"pages must be between 1 and {MAX_PAGES}")
        balance_before = self.balance()
        audit: list[dict[str, Any]] = [
            {"step": "balance", "phase": "before", **balance_before},
        ]
        records: list[SourceRecord] = []
        comments_out: list[CommentRecord] = []
        payloads: list[RawPayload] = []
        gaps: list[str] = []
        failures: list[str] = []
        paid = 0
        from_cache = 0
        status = "ready"

        for page_index in range(1, max(1, pages) + 1):
            search_result = self.search(
                platform,
                query,
                limit=limit,
                page=page if pages == 1 else page_index,
                vertical=vertical,
                raw_ref=f"raw/justone-mcp-{platform}-search-p{page_index}.json",
            )
            audit.extend(search_result.audit)
            payloads.extend(search_result.raw_payloads)
            records.extend(search_result.records)
            comments_out.extend(search_result.comments)
            gaps.extend(search_result.gaps)
            paid += search_result.paid_calls
            from_cache += search_result.from_cache
            if search_result.error:
                failures.append(search_result.error)
            if search_result.status in ("unavailable", "degraded"):
                status = "degraded" if search_result.status == "degraded" else "unavailable"
                break

        if comments and top > 0 and records:
            # 评论目标优先选互动量高的对象：0 评论的内容浪费一次调用。
            candidates = [record for record in records if _comment_object_id(platform, record)]
            candidates.sort(key=_comment_target_rank, reverse=True)
            targets = candidates[:top]
            if not targets:
                gaps.append(f"{platform} 搜索结果中没有可用于评论采集的对象 ID")
            for record in targets:
                object_id = _comment_object_id(platform, record)
                comment_result = self.comments(
                    platform,
                    object_id,
                    raw_ref=f"raw/justone-mcp-{platform}-comments-{object_id}.json",
                    thread_source_id=record.source_id,
                )
                audit.extend(comment_result.audit)
                payloads.extend(comment_result.raw_payloads)
                comments_out.extend(comment_result.comments)
                gaps.extend(comment_result.gaps)
                paid += comment_result.paid_calls
                from_cache += comment_result.from_cache
                if comment_result.error:
                    failures.append(comment_result.error)
                if comment_result.status == "unavailable":
                    gaps.append(f"{platform} 对象 {object_id} 的评论未采集")
                    break

        balance_after = self.balance()
        audit.append({"step": "balance", "phase": "after", **balance_after})
        cost: dict[str, Any] = {
            "currency": balance_after.get("currency") or balance_before.get("currency") or "CNY",
            "balance_before": balance_before.get("balance"),
            "balance_after": balance_after.get("balance"),
        }
        if balance_before.get("balance") is not None and balance_after.get("balance") is not None:
            cost["spent"] = round(
                float(balance_before["balance"]) - float(balance_after["balance"]), 6
            )

        records = list(_dedupe_by_id(records))
        coverage_level = "failed" if not records else ("complete" if status == "ready" else "partial")
        coverage = Coverage(
            level=coverage_level,
            discovered_pages=max(1, pages),
            content_pages=max(1, pages) if records else 0,
            comment_pages=1 if comments_out else 0,
            includes_nested_comments=any(
                comment.parent_id and any(other.source_id == comment.parent_id for other in comments_out)
                for comment in comments_out
            ),
            upstream_reports_more=False,
            notes=f"Just One API MCP：发现→schema→调用；端点 ID 由运行时返回",
        )
        return ProviderResult(
            provider=self.name,
            status=status if records else "unavailable",
            records=tuple(records),
            comments=tuple(comments_out),
            coverage=coverage,
            raw_payloads=tuple(payloads),
            error="；".join(redact_secrets(item) for item in failures) or None,
            detail=f"采集 {len(records)} 条内容、{len(comments_out)} 条评论（页数 {pages}）",
            calls=paid,
            from_cache=from_cache,
            termination="complete" if records else "error",
            gaps=merge_gaps(gaps),
            audit=tuple(audit),
            paid_calls=paid,
            cost=cost,
        )

    # -- 内部 -----------------------------------------------------------

    def _failed(
        self,
        error: str,
        reference: str,
        detail: str,
        *,
        audit: Sequence[Mapping[str, Any]] = (),
        gaps: Sequence[str] = (),
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            status="unavailable",
            coverage=Coverage(level="failed", notes=detail),
            raw_payloads=(RawPayload(name=reference, content=b"{}"),),
            error=redact_secrets(error),
            detail=detail,
            calls=0,
            termination="error",
            gaps=merge_gaps(gaps) or (f"{self.name}：{detail}",),
            audit=tuple(dict(entry) for entry in audit),
        )


WRAPPER_KEYS = ("object", "note", "card", "item", "content", "data")


def _unwrap_search_item(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """搜索接口把正文放在包装键里（知乎 ``object``、小红书 ``note``）。

    只有在「包装键是 Mapping 且该 Mapping 自带 id/正文」时才摊平，避免把真正的
    内容对象（例如知乎回答本身也带 content 字段）再次套娃。
    """
    for key in WRAPPER_KEYS:
        inner = item.get(key)
        if not isinstance(inner, Mapping):
            continue
        if not any(inner.get(field) for field in ("id", "note_id", "answer_id", "bvid", "desc", "content", "title")):
            continue
        merged = dict(inner)
        for meta_key in ("index", "type", "model_type"):
            if meta_key in item and meta_key not in merged:
                merged[meta_key] = item[meta_key]
        if item.get("index") is not None:
            merged["_search_index"] = item.get("index")
        highlight = item.get("highlight")
        if isinstance(highlight, Mapping):
            merged["_search_highlight"] = {
                key: value for key, value in highlight.items() if isinstance(value, str)
            }
        return merged
    return item


def _xhs_images(value: Any) -> tuple[str, ...]:
    urls: list[str] = []
    for item in value or ():
        url = ""
        if isinstance(item, Mapping):
            url = str(item.get("url_size_large") or item.get("url") or item.get("original") or "").strip()
        elif isinstance(item, str):
            url = item.strip()
        if url and url not in urls:
            urls.append(url)
    return tuple(urls)


def _zhihu_public_url(value: Any, answer_id: str, question_id: str) -> str:
    """把 api.zhihu.com 链接改写为公开网页链接。"""
    text = str(value or "").strip()
    if "api.zhihu.com/answers/" in text:
        text = ""
    if text.startswith("http") and "api.zhihu.com" not in text:
        return text
    if answer_id and question_id:
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
    if answer_id:
        return f"https://www.zhihu.com/answer/{answer_id}"
    return ""


COMMENT_LIST_KEYS = ("replies", "comments", "comment_list", "list", "result", "items")


def _collect_comment_items(payload: Any, *, platform: str) -> list[Mapping[str, Any]]:
    """从评论信封中取出评论条目（含置顶评论），保持上游顺序。"""
    items: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    def add(candidate: Any) -> None:
        for item in candidate or ():
            if not isinstance(item, Mapping):
                continue
            key = str(
                item.get("rpid")
                or item.get("id")
                or item.get("comment_id")
                or item.get("content")
                or id(item)
            )
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(node, Mapping):
            return
        for key in COMMENT_LIST_KEYS:
            value = node.get(key)
            if isinstance(value, list) and value and isinstance(value[0], Mapping):
                looks_like_comment = any(
                    item.get("rpid") is not None
                    or item.get("id") is not None
                    or item.get("content") is not None
                    for item in value[:3]
                )
                if looks_like_comment:
                    add(value)
        for key in ("data", "top_replies", "top", "result"):
            if isinstance(node.get(key), Mapping):
                walk(node[key], depth + 1)
            elif isinstance(node.get(key), list):
                add(node[key])

    walk(payload if isinstance(payload, Mapping) else {"data": payload})
    return items


def _oid_thread_id(item: Mapping[str, Any]) -> str:
    oid = normalize_whitespace(item.get("oid") or item.get("oid_str"))
    if not oid:
        return "bilibili:unknown"
    return stable_source_id("bilibili", object_id=f"av{oid}" if oid.isdigit() else oid)


def _next_step(payload: Any) -> dict[str, Any]:
    """把 MCP 的 ``next_step`` 分页提示留痕（用于后续翻页，不猜测参数）。"""
    if not isinstance(payload, Mapping):
        return {}
    step = payload.get("next_step")
    if not isinstance(step, Mapping):
        return {}
    return {
        "action": str(step.get("action") or ""),
        "endpoint_id": str(step.get("endpoint_id") or ""),
        "params": dict(step.get("params") or {}),
        "hint": str(step.get("hint") or ""),
    }


def _first_int(*values: Any) -> int:
    for value in values:
        number = _int(value)
        if number:
            return number
    return 0


def _duration_seconds(value: Any) -> int:
    """B站搜索返回的时长是 ``MM:SS`` 或 ``HH:MM:SS`` 字符串。"""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    if ":" not in text:
        return _int(text)
    parts = [_int(part) for part in text.split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _absolute_url(value: Any) -> str:
    """把协议相对 URL（``//i0.hdslb.com/...``）补全为 https。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text


def _float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _accepts(contract: Mapping[str, Any], name: str) -> bool:
    lowered = name.lower()
    for param in contract.get("params", ()):
        if str(param.get("name") or "").lower() == lowered:
            return True
    return False


def _comment_target_rank(record: SourceRecord) -> tuple[int, int, int]:
    """评论采集目标排序键：评论数 → 点赞/赞同 → 已有正文长度。"""
    engagement = record.engagement if isinstance(record.engagement, Mapping) else {}
    comments = _int(
        engagement.get("comments") or engagement.get("replies") or engagement.get("comment_count")
    )
    likes = _int(
        engagement.get("likes") or engagement.get("votes") or engagement.get("views")
    )
    return (comments, likes, len(record.full_text or ""))


def _comment_object_id(platform: str, record: SourceRecord) -> str:
    """Pick the identifier the comment endpoint needs for this platform."""
    platform_key = str(platform or "").strip().lower()
    if platform_key == "bilibili":
        aid = record.extra.get("aid") if isinstance(record.extra, Mapping) else None
        if aid:
            return str(aid)
        return ""
    return record.source_id.split(":", 1)[-1] if ":" in record.source_id else ""


def _dedupe_by_id(records: Sequence[SourceRecord]) -> list[SourceRecord]:
    order: list[str] = []
    merged: dict[str, SourceRecord] = {}
    for record in records:
        if record.source_id in merged:
            merged[record.source_id] = merge_records(merged[record.source_id], record)
            continue
        order.append(record.source_id)
        merged[record.source_id] = record
    return [merged[source_id] for source_id in order]
