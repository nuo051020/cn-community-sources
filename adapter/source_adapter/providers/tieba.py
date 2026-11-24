"""Tieba providers: public mobile search discovery plus optional aiotieba deep dives."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..http import HttpClient
from ..models import (
    CommentRecord,
    Coverage,
    ProviderStatus,
    SourceRecord,
    stable_source_id,
)
from ..normalize import make_excerpt, normalize_object_id, normalize_whitespace, now_iso, parse_time, redact_secrets
from .base import (
    ProviderResult,
    RawPayload,
    clean_text,
    first_attr,
    json_bytes,
    merge_gaps,
)

SEARCH_URL = "https://tieba.baidu.com/mo/q/search/thread"
SEARCH_PAGE_SIZE = 20
THREAD_URL_TEMPLATE = "https://tieba.baidu.com/p/{tid}"
POST_URL_TEMPLATE = "https://tieba.baidu.com/p/{tid}?pid={pid}"

DISCOVERY_NOTE = "公开移动端搜索仅返回摘要与主题帖元数据，正文需用 aiotieba 深挖"
CAP_NOTE = "达到页数上限（page_limit={limit}），上游仍报告更多结果"
CAP_GAP = "贴吧公开搜索达到页数上限，仍有更多结果未采集"
SKIP_GAP = "部分上游条目结构异常，已跳过且计入缺口"

TERMINATION_UNKNOWN = "unknown"
TERMINATION_EXHAUSTED = "exhausted"
TERMINATION_PAGE_LIMIT = "page_limit"
TERMINATION_ERROR = "error"


def public_search_coverage(
    *,
    has_more: bool,
    page: int,
    page_limit: Optional[int],
    page_size: int,
    skipped: int = 0,
) -> Coverage:
    """Coverage for one discovery page: always partial, never full text."""
    notes = [DISCOVERY_NOTE]
    if has_more and page_limit and page >= page_limit:
        notes.append(CAP_NOTE.format(limit=page_limit))
    if skipped:
        notes.append(f"跳过 {skipped} 条结构异常条目")
    return Coverage(
        level="partial",
        discovered_pages=1,
        upstream_reports_more=bool(has_more),
        notes="; ".join(notes),
    )


def _media_urls(media: Any) -> tuple[str, ...]:
    """Extract image or video URLs from the several shapes the API uses."""
    if not media:
        return ()
    candidates: list[Any] = []
    if isinstance(media, str):
        candidates = [media]
    elif isinstance(media, Mapping):
        for key in ("url", "src", "image", "thumb", "thumbnail", "cover"):
            if media.get(key):
                candidates.append(media[key])
        for key in ("images", "pics", "list", "items", "urls"):
            value = media.get(key)
            if isinstance(value, (list, tuple)):
                candidates.extend(value)
    elif isinstance(media, (list, tuple)):
        candidates = list(media)
    urls: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, Mapping):
            url = str(item.get("url") or item.get("src") or "").strip()
        else:
            url = str(getattr(item, "url", "") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return tuple(urls)


def _numeric(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class TiebaPublicProvider:
    """Discovery-only provider over the public mobile search JSON endpoint."""

    name = "tieba"

    def __init__(
        self,
        *,
        http: Optional[HttpClient] = None,
        clock: Callable[[], str] = now_iso,
        search_url: str = SEARCH_URL,
    ) -> None:
        self.http = http or HttpClient(provider=self.name)
        self.clock = clock
        self.search_url = search_url or SEARCH_URL

    def doctor(self) -> ProviderStatus:
        return ProviderStatus(
            provider=self.name,
            status="ready",
            detail=(
                "公开移动端搜索接口已配置，仅做本地就绪检查（未联网探测）；"
                "返回摘要级别证据，不声称完整正文"
            ),
            capabilities=("search",),
            checked_at=self.clock(),
        )

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        page_size: int = SEARCH_PAGE_SIZE,
        page_limit: Optional[int] = None,
        raw_ref: str = "",
    ) -> ProviderResult:
        if not str(query or "").strip():
            raise ValueError("query must not be blank")
        if page < 1:
            raise ValueError("page must be >= 1")
        if not 1 <= page_size <= 50:
            raise ValueError("page_size must be between 1 and 50")

        response = self.http.get_json(
            self.search_url,
            params={"word": query, "pn": page, "rn": page_size},
            headers=(("Accept", "application/json"), ("Referer", "https://tieba.baidu.com/")),
        )
        payload_name = raw_ref or f"raw/tieba-public-search-p{page}.json"
        if not response.ok:
            # A 2xx response we could not parse is a shape problem (degraded); a
            # transport or HTTP-level failure means the provider is unavailable.
            reached_upstream = bool(response.status and 200 <= response.status < 300)
            return ProviderResult(
                provider=self.name,
                status="degraded" if reached_upstream else "unavailable",
                coverage=Coverage(level="failed", notes="贴吧公开搜索响应无法解析"),
                error=response.error or "request failed",
                detail=(
                    "上游响应结构异常，未产生任何记录"
                    if reached_upstream
                    else "公开移动端搜索不可用，未产生任何记录"
                ),
                calls=1,
                from_cache=1 if response.from_cache else 0,
                termination=TERMINATION_ERROR,
                gaps=("贴吧公开搜索未产生发现层证据",),
            )

        payloads = [RawPayload(name=payload_name, content=response.body)]
        payload = response.data
        if not isinstance(payload, Mapping):
            return self._degraded(
                payloads,
                f"unexpected payload type: {type(payload).__name__}",
                "上游返回结构不是 JSON 对象",
                response,
            )
        upstream_error = payload.get("error")
        if upstream_error not in (None, "", "success") or _numeric(payload.get("no")) != 0:
            return self._degraded(
                payloads,
                f"upstream error: {upstream_error}",
                "上游报告错误",
                response,
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return self._degraded(payloads, "payload is missing the data object", "缺少 data 字段", response)
        post_list = data.get("post_list")
        if not isinstance(post_list, list):
            return self._degraded(
                payloads, "payload is missing the post_list array", "缺少 post_list 字段", response
            )

        retrieved_at = self.clock()
        has_more = bool(data.get("has_more"))
        current_page = _numeric(data.get("current_page")) or page
        coverage = public_search_coverage(
            has_more=has_more,
            page=page,
            page_limit=page_limit,
            page_size=page_size,
        )
        records: list[SourceRecord] = []
        skipped = 0
        for rank, item in enumerate(post_list, start=1):
            record = self._build_record(
                item,
                rank=rank,
                page=current_page,
                retrieved_at=retrieved_at,
                coverage=coverage,
                raw_ref=payload_name,
            )
            if record is None:
                skipped += 1
                continue
            records.append(record)

        gaps = []
        if has_more and page_limit and page >= page_limit:
            gaps.append(CAP_GAP)
        if skipped:
            gaps.append(SKIP_GAP)
        status = "degraded" if skipped else "ready"
        detail = f"解析 {len(records)} 条主题帖"
        if skipped:
            detail += f"，跳过 {skipped} 条无法解析的条目"
        if not records and not skipped:
            detail = "未找到匹配的主题帖"
        if skipped:
            coverage = public_search_coverage(
                has_more=has_more, page=page, page_limit=page_limit, page_size=page_size, skipped=skipped
            )
        return ProviderResult(
            provider=self.name,
            status=status,
            records=tuple(records),
            coverage=coverage,
            raw_payloads=tuple(payloads),
            detail=detail,
            calls=1,
            from_cache=1 if response.from_cache else 0,
            termination=TERMINATION_UNKNOWN,
            gaps=merge_gaps(gaps),
        )

    def _degraded(
        self,
        payloads: Sequence[RawPayload],
        error: str,
        detail: str,
        response: Any,
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            status="degraded",
            coverage=Coverage(level="failed", notes=detail),
            raw_payloads=tuple(payloads),
            error=redact_secrets(error),
            detail=detail,
            calls=1,
            from_cache=1 if getattr(response, "from_cache", False) else 0,
            termination=TERMINATION_ERROR,
            gaps=("贴吧公开搜索返回结构异常，未产生发现层记录",),
        )

    def _build_record(
        self,
        item: Any,
        *,
        rank: int,
        page: int,
        retrieved_at: str,
        coverage: Coverage,
        raw_ref: str,
    ) -> Optional[SourceRecord]:
        if not isinstance(item, Mapping):
            return None
        tid = normalize_object_id("tieba", item.get("tid"))
        title = normalize_whitespace(item.get("title"))
        if not tid or not title:
            return None
        user = item.get("user") if isinstance(item.get("user"), Mapping) else {}
        author = normalize_whitespace(
            user.get("show_nickname") or user.get("user_name") or user.get("nick_name")
        )
        content = normalize_whitespace(item.get("content"))
        return SourceRecord(
            source_id=stable_source_id("tieba", object_id=tid),
            platform="tieba",
            record_kind="thread",
            source_type="community",
            title=title,
            url=THREAD_URL_TEMPLATE.format(tid=tid),
            author_display_name=author,
            published_at=parse_time(item.get("create_time") or item.get("time")),
            retrieved_at=retrieved_at,
            excerpt=make_excerpt(content),
            full_text="",
            engagement={
                "replies": _numeric(item.get("post_num")),
                "likes": _numeric(item.get("like_num")),
                "shares": _numeric(item.get("share_num")),
                "views": _numeric(item.get("play_count")),
            },
            media=_media_urls(item.get("media")),
            collection_method="tieba_public_search_json",
            coverage=coverage,
            raw_ref=raw_ref,
            extra={
                "forum_name": normalize_whitespace(item.get("forum_name")),
                "forum_id": _numeric(item.get("forum_id")),
                "thread_types": _numeric(item.get("thread_types")),
                "search_rank": rank,
                "page": page,
                "discovery_only": True,
                "content_truncated_by_upstream": True,
            },
        )


def map_thread_page(
    page: Any,
    *,
    tid: str,
    thread_id: str,
    thread_title: str,
    raw_ref: str,
    retrieved_at: str,
    page_number: int = 1,
    collection_method: str = "aiotieba_get_posts",
) -> tuple[list[SourceRecord], list[CommentRecord], list[str], bool]:
    """Map one aiotieba post page into records, comments, skips and has_more."""
    objects = _page_objects(page)
    records: list[SourceRecord] = []
    comments: list[CommentRecord] = []
    skipped: list[str] = []
    placeholder = Coverage(level="unknown")
    for index, post in enumerate(objects):
        pid = normalize_object_id("tieba", first_attr(post, "pid", "id"))
        text = clean_text(first_attr(post, "text", "content", default=""))
        if not pid or not text:
            skipped.append(f"page {page_number} item {index + 1}: missing pid or text")
            continue
        user = getattr(post, "user", None)
        author = normalize_whitespace(
            first_attr(user, "show_name", "nick_name", "user_name", default="")
        )
        floor = first_attr(post, "floor", default=None)
        if isinstance(floor, bool) or not isinstance(floor, int):
            floor = None
        published_at = parse_time(first_attr(post, "create_time", "time", default=None))
        reply_num = _numeric(first_attr(post, "reply_num", default=0))
        is_opening_post = floor == 1 or pid == normalize_object_id("tieba", tid)
        title = thread_title or make_excerpt(text, 60)
        title_source = "provided" if thread_title else "opening_excerpt"
        if is_opening_post:
            source_id = thread_id
            record_kind = "thread"
            parent_id = None
            floor_number = floor if floor is not None else 1
        else:
            source_id = stable_source_id("tieba", object_id=pid)
            record_kind = "post"
            parent_id = thread_id
            floor_number = floor
        record = SourceRecord(
            source_id=source_id,
            platform="tieba",
            record_kind=record_kind,
            source_type="community",
            title=title,
            url=THREAD_URL_TEMPLATE.format(tid=tid),
            author_display_name=author,
            published_at=published_at,
            retrieved_at=retrieved_at,
            excerpt=make_excerpt(text),
            full_text=text,
            thread_id=thread_id,
            parent_id=parent_id,
            floor_number=floor_number,
            engagement={
                "replies": reply_num,
                "likes": _numeric(first_attr(post, "agree", "like_num", default=0)),
            },
            media=_media_urls(first_attr(post, "imgs", "media", default=None)),
            collection_method=collection_method,
            coverage=placeholder,
            raw_ref=raw_ref,
            extra={
                "page": page_number,
                "title_source": title_source,
                "opening_post": is_opening_post,
            },
        )
        records.append(record)
        for inline in _iter_inline_comments(post):
            comment = _build_comment(
                inline,
                tid=tid,
                thread_id=thread_id,
                parent_id=record.source_id,
                raw_ref=raw_ref,
                retrieved_at=retrieved_at,
                collection_method=collection_method,
            )
            if comment is None:
                skipped.append(f"page {page_number} floor {floor}: unparsable comment")
                continue
            comments.append(comment)
    return records, comments, skipped, bool(getattr(page, "has_more", False))


def _build_comment(
    item: Any,
    *,
    tid: str,
    thread_id: str,
    parent_id: str,
    raw_ref: str,
    retrieved_at: str,
    collection_method: str,
    fallback_parent: Optional[str] = None,
) -> Optional[CommentRecord]:
    pid = normalize_object_id("tieba", first_attr(item, "pid", "id"))
    text = clean_text(first_attr(item, "text", "content", default=""))
    if not pid or not text:
        return None
    user = getattr(item, "user", None)
    author = normalize_whitespace(first_attr(user, "show_name", "nick_name", "user_name", default=""))
    reply_to = normalize_object_id("tieba", first_attr(item, "reply_to_id", "reply_to", default=None))
    resolved_parent = parent_id
    if fallback_parent and reply_to:
        resolved_parent = stable_source_id("tieba", object_id=reply_to)
    return CommentRecord(
        source_id=stable_source_id("tieba", object_id=pid),
        platform="tieba",
        thread_id=thread_id,
        parent_id=resolved_parent,
        url=POST_URL_TEMPLATE.format(tid=tid, pid=pid),
        author_display_name=author,
        published_at=parse_time(first_attr(item, "create_time", "time", default=None)),
        retrieved_at=retrieved_at,
        full_text=text,
        engagement={
            "likes": _numeric(first_attr(item, "agree", "like_num", default=0)),
            "replies": _numeric(first_attr(item, "reply_num", default=0)),
        },
        collection_method=collection_method,
        coverage=Coverage(level="unknown"),
        raw_ref=raw_ref,
        extra={"nested": fallback_parent is None},
    )


def _iter_inline_comments(post: Any) -> Iterable[Any]:
    comments = getattr(post, "comments", None)
    if comments is None:
        return ()
    if isinstance(comments, Mapping):  # pragma: no cover - defensive only
        return tuple(comments.values())
    return tuple(comments)


def _page_objects(page: Any) -> list[Any]:
    for attribute in ("objs", "posts", "comments", "items"):
        value = getattr(page, attribute, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    if isinstance(page, (list, tuple)):
        return list(page)
    return []


class AiotiebaProvider:
    """Optional deep-dive provider: floors, inline comments and nested replies."""

    name = "tieba-aiotieba"

    def __init__(
        self,
        *,
        importer: Callable[[str], Any] = importlib.import_module,
        client_factory: Optional[Callable[[Any], Any]] = None,
        clock: Callable[[], str] = now_iso,
        page_size: int = 30,
    ) -> None:
        self.importer = importer
        self.client_factory = client_factory or (lambda module: module.Client())
        self.clock = clock
        self.page_size = page_size

    def doctor(self) -> ProviderStatus:
        try:
            self.importer("aiotieba")
        except ImportError:
            return ProviderStatus(
                provider=self.name,
                status="unavailable",
                detail=(
                    "可选依赖 aiotieba 未安装；公开搜索发现仍可用，"
                    "但楼层与楼中楼深挖不可用。安装：python3 -m pip install aiotieba"
                ),
                capabilities=(),
                checked_at=self.clock(),
            )
        return ProviderStatus(
            provider=self.name,
            status="ready",
            detail="aiotieba 已安装；按 TID 读取主题帖、楼层与楼中楼",
            capabilities=("thread", "comments", "nested_comments"),
            checked_at=self.clock(),
        )

    def fetch_thread(
        self,
        tid: str,
        *,
        page_limit: int = 1,
        include_nested: bool = False,
        thread_title: str = "",
        raw_ref_prefix: str = "raw/tieba-thread",
        retrieved_at: Optional[str] = None,
    ) -> ProviderResult:
        normalized_tid = normalize_object_id("tieba", tid)
        if not normalized_tid:
            raise ValueError("tid must not be blank")
        if page_limit < 1:
            raise ValueError("page_limit must be >= 1")
        try:
            module = self.importer("aiotieba")
        except ImportError as exc:
            return ProviderResult(
                provider=self.name,
                status="unavailable",
                coverage=Coverage(level="failed", notes="aiotieba 未安装，贴吧正文深挖不可用"),
                error=redact_secrets(f"optional dependency aiotieba is not installed: {exc}"),
                detail="跳过贴吧楼层与楼中楼采集",
                termination=TERMINATION_ERROR,
                gaps=("贴吧楼层与楼中楼未采集：可选依赖 aiotieba 未安装",),
            )
        try:
            return asyncio.run(
                self._collect(
                    module,
                    normalized_tid,
                    page_limit=page_limit,
                    include_nested=include_nested,
                    thread_title=thread_title,
                    raw_ref_prefix=raw_ref_prefix,
                    retrieved_at=retrieved_at or self.clock(),
                )
            )
        except Exception as exc:  # upstream SDK or network failure
            return ProviderResult(
                provider=self.name,
                status="degraded",
                coverage=Coverage(level="failed", notes="aiotieba 调用失败"),
                error=redact_secrets(f"{type(exc).__name__}: {exc}"),
                detail="贴吧深挖调用失败，未产生楼层记录",
                termination=TERMINATION_ERROR,
                gaps=("贴吧楼层与楼中楼采集失败",),
            )

    async def _collect(
        self,
        module: Any,
        tid: str,
        *,
        page_limit: int,
        include_nested: bool,
        thread_title: str,
        raw_ref_prefix: str,
        retrieved_at: str,
    ) -> ProviderResult:
        thread_id = stable_source_id("tieba", object_id=tid)
        records: list[SourceRecord] = []
        comments: list[CommentRecord] = []
        payloads: list[RawPayload] = []
        skipped: list[str] = []
        pages_fetched = 0
        comment_pages = 0
        nested_used = False
        termination = TERMINATION_UNKNOWN
        gaps: list[str] = []

        async with self.client_factory(module) as client:
            while pages_fetched < page_limit:
                pages_fetched += 1
                raw_ref = f"{raw_ref_prefix}-p{pages_fetched}.json"
                # aiotieba >= 4.7 types tid/pid as int; upstream ids are decimal strings.
                page = await client.get_posts(int(tid), pn=pages_fetched)
                payloads.append(RawPayload(name=raw_ref, content=json_bytes(page)))
                page_records, page_comments, page_skips, has_more = map_thread_page(
                    page,
                    tid=tid,
                    thread_id=thread_id,
                    thread_title=thread_title,
                    raw_ref=raw_ref,
                    retrieved_at=retrieved_at,
                    page_number=pages_fetched,
                )
                records.extend(page_records)
                comments.extend(page_comments)
                skipped.extend(page_skips)
                if not has_more:
                    termination = TERMINATION_EXHAUSTED
                    break
                termination = TERMINATION_PAGE_LIMIT

            if include_nested and termination != TERMINATION_ERROR:
                pending = [
                    comment
                    for comment in list(comments)
                    if _numeric(comment.engagement.get("replies")) > 1
                ]
                for comment in pending:
                    child_pid = comment.source_id.split(":", 1)[-1]
                    raw_ref = f"{raw_ref_prefix}-c{child_pid}.json"
                    try:
                        page = await client.get_comments(int(tid), int(child_pid), pn=1)
                    except Exception as exc:
                        skipped.append(f"nested comments for {child_pid}: {type(exc).__name__}")
                        gaps.append("部分楼中楼未采集：上游调用失败")
                        continue
                    comment_pages += 1
                    payloads.append(RawPayload(name=raw_ref, content=json_bytes(page)))
                    for item in _page_objects(page):
                        child = _build_comment(
                            item,
                            tid=tid,
                            thread_id=thread_id,
                            parent_id=comment.source_id,
                            fallback_parent=comment.source_id,
                            raw_ref=raw_ref,
                            retrieved_at=retrieved_at,
                            collection_method="aiotieba_get_comments",
                        )
                        if child is None:
                            skipped.append(f"nested comment under {child_pid}: unparsable")
                            continue
                        nested_used = True
                        comments.append(child)

        if termination == TERMINATION_PAGE_LIMIT:
            gaps.append(CAP_GAP)
        if skipped:
            gaps.append(SKIP_GAP)
        notes = []
        if termination == TERMINATION_PAGE_LIMIT:
            notes.append(CAP_NOTE.format(limit=page_limit))
        if skipped:
            notes.append(f"跳过 {len(skipped)} 条结构异常条目")
        if not records:
            level = "failed"
            notes.append("未解析出任何楼层")
        elif termination == TERMINATION_PAGE_LIMIT or skipped:
            level = "partial"
        else:
            level = "complete"
        coverage = Coverage(
            level=level,
            content_pages=pages_fetched,
            comment_pages=comment_pages,
            includes_nested_comments=nested_used,
            upstream_reports_more=termination == TERMINATION_PAGE_LIMIT,
            notes="; ".join(notes),
        )
        final_records = tuple(replace(record, coverage=coverage) for record in records)
        final_comments = tuple(replace(comment, coverage=coverage) for comment in comments)
        status = "degraded" if skipped else ("ready" if final_records else "unavailable")
        detail = f"读取 {len(final_records)} 条楼层、{len(final_comments)} 条评论"
        if skipped:
            detail += f"，跳过 {len(skipped)} 条结构异常条目"
        return ProviderResult(
            provider=self.name,
            status=status,
            records=final_records,
            comments=final_comments,
            coverage=coverage,
            raw_payloads=tuple(payloads),
            detail=detail,
            calls=pages_fetched + comment_pages,
            termination=termination,
            gaps=merge_gaps(gaps),
        )
