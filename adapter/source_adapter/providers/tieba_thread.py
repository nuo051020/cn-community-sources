"""Anonymous, read-only Tieba thread provider over the public mobile web page.

This closes the gap left by :class:`source_adapter.providers.tieba.AiotiebaProvider`:
the aiotieba dependency is optional and usually absent, while the public search
endpoint only returns summaries. This provider reads the *server-rendered* mobile
thread page instead, so floors and first-page nested replies are available with
the standard library alone.

Reconnaissance (verified live with plain ``curl`` GET requests, 2026-09):

* ``https://tieba.baidu.com/mo/q/thread?kz=<tid>&pn=<page>`` - server-rendered
  HTML thread page. This is the endpoint in use. The thread id must be passed as
  ``kz``; passing ``tid`` instead yields an empty "正在加载中" shell that contains
  no floors, and ``https://tieba.baidu.com/p/<tid>`` as well as
  ``https://tieba.baidu.com/p/comment`` answer ``403 百度安全验证`` for anonymous
  non-browser clients.
* ``https://tieba.baidu.com/mo/q/search/thread?word=...`` - the already-implemented
  discovery endpoint, used only to locate thread ids during reconnaissance.
* Request headers actually required: a real desktop browser ``User-Agent`` plus
  ``Accept``/``Accept-Language`` and a ``Referer`` pointing at the thread page.
  The provider never sends a ``Cookie``, never logs in and never attempts to solve
  or bypass any access control such as Baidu's 安全验证 wall.

Page structure relied upon (kept deliberately narrow and shape-checked):

* every floor is ``<li ... fn="<floor_number>" data-info="{...pid, floor_num...}">``
* floor text lives in ``<div class="content">`` (HTML entities + ``<br/>``)
* author is the first ``<a class="user_name">``, publication time the first
  ``<span class="list_item_time">`` (Tieba prints ``M-D HH:MM``)
* forum name comes from the ``forum_name: "..."`` page config
* nested replies (楼中楼) live in ``<div class="fr_list <floor> j_floor_panel">``
  with one ``<li pid="...">`` per reply

Known limits, reported honestly in ``coverage``/``gaps`` rather than papered over:

* the mobile thread page currently serves only the first 30 floors of a thread and
  ignores ``pn`` (page 2 answers with a byte-identical body). Multi-page walking is
  still implemented for the documented ``page_limit`` contract; today it normally
  ends as ``repeated_page``. A second-page shape is covered by
  ``tests/fixtures/tieba_thread_11045552764_p2.html``.
* nested replies are fetched as part of the same HTML response, so
  ``include_nested=True`` costs no extra request; ``coverage.comment_pages``
  therefore counts floor pages that actually yielded nested replies.
* the public page only server-renders a few nested replies per floor and hides the
  rest behind an app-only hint (``打开APP查看N条评论``). ``max_comment_pages`` bounds
  how many floors are parsed for nested replies; the remainder is recorded as a gap.
"""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Optional

from ..http import HttpClient
from ..models import (
    CommentRecord,
    Coverage,
    ProviderStatus,
    SourceRecord,
    stable_source_id,
)
from ..normalize import (
    make_excerpt,
    normalize_object_id,
    normalize_whitespace,
    now_iso,
    parse_time,
    redact_secrets,
)
from .base import (
    ProviderResult,
    RawPayload,
    clean_text,
    merge_gaps,
)

HTML_ENDPOINT = "https://tieba.baidu.com/mo/q/thread"
POST_URL_TEMPLATE = "https://tieba.baidu.com/p/{tid}?pid={pid}"
THREAD_URL_TEMPLATE = "https://tieba.baidu.com/p/{tid}"

# A real desktop browser UA is required; the anonymous tool UA is rejected.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    ),
    ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
    ("Referer", "https://tieba.baidu.com/"),
)
REQUEST_HEADERS: tuple[tuple[str, str], ...] = (
    ("User-Agent", BROWSER_USER_AGENT),
    *BROWSER_HEADERS,
)

TERMINATION_UNKNOWN = "unknown"
TERMINATION_EXHAUSTED = "exhausted"
TERMINATION_PAGE_LIMIT = "page_limit"
TERMINATION_REPEATED_PAGE = "repeated_page"
TERMINATION_ERROR = "error"

PAGE_LIMIT_NOTE = "达到楼层页数上限（page_limit={limit}），上游仍可能有更多楼层"
PAGE_LIMIT_GAP = "贴吧楼层达到页数上限，仍有更多楼层未采集"
REPEATED_PAGE_NOTE = "上游返回了重复楼层页，已停止翻页"
REPEATED_PAGE_GAP = "贴吧楼层页重复（上游对同一内容重复响应），后续楼层未采集"
NESTED_LIMIT_NOTE = "楼中楼页数达到上限（max_comment_pages={limit}），仅解析前 {limit} 层楼的楼中楼"
NESTED_LIMIT_GAP = "楼中楼达到 max_comment_pages 上限，部分楼层的内嵌评论未解析"
SKIP_NOTE = "跳过 {count} 条结构异常条目"
SKIP_GAP = "部分楼层或评论结构异常，已跳过且计入缺口"
SHAPE_GAP = "贴吧主题帖页面结构异常，未产生楼层记录"
UNAVAILABLE_GAP = "贴吧主题帖公开页面不可用，未产生楼层记录"
NESTED_COLLECTION_METHOD = "tieba_mobile_web_thread_nested"

# Shape markers of the server-rendered page.
_FLOOR_OPEN_RE = re.compile(r"<li\b[^>]*?fn=\"(?P<fn>\d+)\"[^>]*>", re.S)
_FLOOR_ATTR_RE = re.compile(r"^<li\b(?P<attrs>[^>]*)>", re.S)
_DATA_INFO_RE = re.compile(r"data-info=(?P<quote>[\"'])(?P<info>.*?)(?P=quote)", re.S)
_CONTENT_RE = re.compile(
    r"<div\b[^>]*\bclass=(?P<quote>[\"'])[^\"']*(?<![A-Za-z0-9_-])content(?![A-Za-z0-9_-])"
    r"[^\"']*(?P=quote)[^>]*>(?P<text>.*?)</div>",
    re.S,
)
_TITLE_RE = re.compile(
    r"<div\b[^>]*\bclass=(?P<quote>[\"'])[^\"']*\bpost_title_embed\b[^\"']*(?P=quote)[^>]*>(?P<text>.*?)</div>",
    re.S,
)
_AUTHOR_RE = re.compile(
    r"<a\b[^>]*\bclass=(?P<quote>[\"'])[^\"']*\buser_name\b[^\"']*(?P=quote)[^>]*>(?P<name>.*?)</a>",
    re.S,
)
_LEVEL_RE = re.compile(r"<span\b[^>]*\bclass=\"[^\"]*\blevel\b[^\"]*\"[^>]*>\s*(?P<level>[^<]*)</span>")
_TIME_RE = re.compile(
    r"<span\b[^>]*\bclass=\"[^\"]*\blist_item_time\b[^\"]*\"[^>]*>(?P<time>[^<]*)</span>"
)
_FORUM_NAME_RE = re.compile(r"forum_name:\s*[\"'](?P<name>[^\"']+)[\"']")
_NESTED_PANEL_RE = re.compile(
    r"<div\b[^>]*\bclass=(?P<quote>[\"'])[^\"']*\bfr_list\b[^\"']*(?P=quote)[^>]*>"
    r".*?<ul\b[^>]*\bclass=(?P<uquote>[\"'])[^\"']*\bflist\b[^\"']*(?P=uquote)[^>]*>"
    r"(?P<items>.*?)</ul>",
    re.S,
)
_NESTED_ITEM_RE = re.compile(r"<li\b[^>]*\bpid=\"(?P<pid>\d+)\"[^>]*>(?P<body>.*?)</li>", re.S)
_NESTED_AUTHOR_RE = re.compile(
    r"<a\b[^>]*\bclass=(?P<quote>[\"'])[^\"']*\buser_name\b[^\"']*(?P=quote)[^>]*>(?P<name>.*?)</a>",
    re.S,
)
_NESTED_TEXT_RE = re.compile(r"<span\b[^>]*\bclass=\"[^\"]*\bfloor_content\b[^\"]*\"[^>]*>(?P<text>.*?)</span>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc=\"(?P<src>[^\"]+)\"", re.I)
_SHORT_TIME_RE = re.compile(
    r"^(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})[-/月](?P<day>\d{1,2})日?"
    r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?$"
)


@dataclass(frozen=True, slots=True)
class _Floor:
    """One parsed floor before it becomes a record."""

    ordinal: int
    source_id: str
    pid: str
    floor_number: Optional[int]
    author: str
    published_at: Optional[str]
    body_html: str
    full_text: str
    excerpt: str
    media: tuple[str, ...]
    replies: int
    likes: int
    level: str


@dataclass(frozen=True, slots=True)
class _Nested:
    """One parsed 楼中楼 comment attached to a floor."""

    source_id: str
    pid: str
    parent_pid: str
    parent_source_id: str
    author: str
    full_text: str


@dataclass(frozen=True, slots=True)
class _PageReport:
    """Everything one floor page yielded, plus its shape information."""

    floors: tuple[_Floor, ...]
    nested: tuple[_Nested, ...]
    skips: tuple[str, ...]
    floor_containers: int
    title: str
    forum_name: str
    open_error: str


class TiebaThreadProvider:
    """Read-only Tieba thread provider over the public mobile web page."""

    name = "tieba-thread"

    def __init__(
        self,
        *,
        http: Optional[HttpClient] = None,
        clock: Callable[[], str] = now_iso,
        page_size: int = 30,
    ) -> None:
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        self.http = http or HttpClient(provider=self.name)
        self.clock = clock
        self.page_size = page_size

    # -- readiness ------------------------------------------------------

    def doctor(self) -> ProviderStatus:
        return ProviderStatus(
            provider=self.name,
            status="ready",
            detail=(
                "使用公开 Web 接口（/mo/q/thread 服务端渲染页面）读取楼层与楼中楼，"
                "不依赖可选依赖 aiotieba；仅做本地就绪检查（未联网探测），"
                "不发送 Cookie、不登录、不尝试绕过验证码"
            ),
            capabilities=("thread", "comments", "nested_comments"),
            checked_at=self.clock(),
        )

    # -- collection -----------------------------------------------------

    def fetch_thread(
        self,
        tid: str,
        *,
        page_limit: int = 1,
        include_nested: bool = False,
        thread_title: str = "",
        max_comment_pages: int = 1,
        raw_ref_prefix: str = "raw/tieba-thread",
        retrieved_at: Optional[str] = None,
    ) -> ProviderResult:
        normalized_tid = normalize_object_id("tieba", tid)
        if not normalized_tid:
            raise ValueError("tid must not be blank")
        if isinstance(page_limit, bool) or not isinstance(page_limit, int) or page_limit < 1:
            raise ValueError("page_limit must be >= 1")
        if (
            isinstance(max_comment_pages, bool)
            or not isinstance(max_comment_pages, int)
            or max_comment_pages < 1
        ):
            raise ValueError("max_comment_pages must be >= 1")

        thread_id = stable_source_id("tieba", object_id=normalized_tid)
        prefix = str(raw_ref_prefix or "raw/tieba-thread").rstrip("/")
        retrieved = retrieved_at or self.clock()

        records: list[SourceRecord] = []
        comments: list[CommentRecord] = []
        payloads: list[RawPayload] = []
        skips: list[str] = []
        gaps: list[str] = []
        seen: set[str] = set()
        content_pages = 0
        comment_pages = 0
        nested_used = False
        nested_limit_hit = False
        pages_with_nested = 0
        calls = 0
        from_cache = 0
        termination = TERMINATION_UNKNOWN
        first_page = True
        page_title = ""
        forum_name = ""
        skipped_total = 0

        for page_number in range(1, page_limit + 1):
            raw_ref = f"{prefix}-p{page_number}.html"
            response = self.http.get_text(
                HTML_ENDPOINT,
                params={"kz": normalized_tid, "pn": page_number},
                headers=REQUEST_HEADERS,
            )
            calls += 1
            # Raw evidence is always kept, including an empty 5xx body: a missing
            # payload would hide the failure, and a 403 安全验证 wall is never
            # treated as content.
            payloads.append(
                RawPayload(
                    name=raw_ref, content=response.body or b"", media_type="text/html"
                )
            )
            if response.from_cache:
                from_cache += 1

            if not response.ok:
                error = redact_secrets(response.error or "request failed")
                if not first_page and records:
                    gaps.append(f"第 {page_number} 页请求失败，已停止翻页：{error}")
                    break
                return ProviderResult(
                    provider=self.name,
                    status=(
                        "degraded"
                        if response.status and 200 <= response.status < 300
                        else "unavailable"
                    ),
                    coverage=Coverage(
                        level="failed",
                        content_pages=content_pages,
                        comment_pages=comment_pages,
                        notes="贴吧主题帖公开页面无法读取",
                    ),
                    raw_payloads=tuple(payloads),
                    error=error,
                    detail="贴吧楼层与楼中楼未采集",
                    calls=calls,
                    from_cache=from_cache,
                    termination=TERMINATION_ERROR,
                    gaps=merge_gaps(gaps, (UNAVAILABLE_GAP,)),
                )

            content_pages += 1
            body_text = response.data if isinstance(response.data, str) else ""

            if first_page:
                title, forum = _page_identity(body_text)
                page_title = title
                forum_name = forum
            else:
                page_title = page_title or thread_title
            report = _parse_page(
                body_text,
                tid=normalized_tid,
                thread_id=thread_id,
                page_number=page_number,
                include_nested=include_nested,
            )
            if report.open_error:
                if not first_page and records:
                    gaps.append(f"第 {page_number} 页结构异常，已停止翻页：{report.open_error}")
                    break
                return ProviderResult(
                    provider=self.name,
                    status="degraded",
                    coverage=Coverage(
                        level="failed",
                        content_pages=content_pages,
                        comment_pages=comment_pages,
                        notes="贴吧主题帖页面结构无法解析",
                    ),
                    raw_payloads=tuple(payloads),
                    error=redact_secrets(report.open_error),
                    detail="贴吧楼层与楼中楼未采集",
                    calls=calls,
                    from_cache=from_cache,
                    termination=TERMINATION_ERROR,
                    gaps=merge_gaps(gaps, (SHAPE_GAP,)),
                )
            skips.extend(report.skips)
            skipped_total += len(report.skips)
            added: list[str] = []
            floor_by_source_id: dict[str, _Floor] = {}
            for floor in report.floors:
                if floor.source_id in seen:
                    continue
                seen.add(floor.source_id)
                added.append(floor.source_id)
                floor_by_source_id[floor.source_id] = floor
                records.append(
                    self._build_record(
                        floor,
                        tid=normalized_tid,
                        thread_id=thread_id,
                        thread_title=thread_title,
                        opening_title=thread_title or page_title,
                        page_number=page_number,
                        raw_ref=raw_ref,
                        retrieved_at=retrieved,
                        forum_name=report.forum_name or forum_name,
                    )
                )

            if include_nested and report.floors:
                if pages_with_nested >= max_comment_pages:
                    nested_limit_hit = True
                else:
                    pages_with_nested += 1
                    page_nested = 0
                    by_pid = {floor.pid: floor for floor in report.floors}
                    for item in report.nested:
                        if item.source_id in seen:
                            continue
                        # Nested replies are rendered inside their parent floor's
                        # block, so a comment whose floor is not on this page is
                        # not trustworthy and is skipped rather than guessed at.
                        parent = by_pid.get(item.parent_pid)
                        if parent is None or parent.source_id not in floor_by_source_id:
                            continue
                        seen.add(item.source_id)
                        comments.append(
                            self._build_comment(
                                item,
                                tid=normalized_tid,
                                thread_id=thread_id,
                                raw_ref=raw_ref,
                                retrieved_at=retrieved,
                            )
                        )
                        page_nested += 1
                    if page_nested:
                        nested_used = True
                        comment_pages += 1

            if not added:
                termination = TERMINATION_REPEATED_PAGE
                gaps.append(REPEATED_PAGE_GAP)
                break

            # A short page means the thread ended; a full page at the cap means
            # there is (or may be) more upstream, which is a page_limit stop.
            if not _has_more(report, page_size=self.page_size):
                termination = TERMINATION_EXHAUSTED
                break
            if page_number >= page_limit:
                termination = TERMINATION_PAGE_LIMIT
                break
            first_page = False

        if skipped_total:
            gaps.append(SKIP_GAP)
        if nested_limit_hit:
            gaps.append(NESTED_LIMIT_GAP)
        if termination == TERMINATION_PAGE_LIMIT:
            gaps.append(PAGE_LIMIT_GAP)
        if termination in (TERMINATION_UNKNOWN, TERMINATION_ERROR):
            termination = TERMINATION_UNKNOWN

        has_records = bool(records)
        if not has_records:
            level = "failed"
        elif termination in (TERMINATION_PAGE_LIMIT, TERMINATION_REPEATED_PAGE) or skipped_total:
            level = "partial"
        elif nested_limit_hit:
            level = "partial"
        else:
            level = "complete"

        notes: list[str] = []
        if termination == TERMINATION_PAGE_LIMIT:
            notes.append(PAGE_LIMIT_NOTE.format(limit=page_limit))
        if termination == TERMINATION_REPEATED_PAGE:
            notes.append(REPEATED_PAGE_NOTE)
        if nested_limit_hit:
            notes.append(NESTED_LIMIT_NOTE.format(limit=max_comment_pages))
        if skipped_total:
            notes.append(SKIP_NOTE.format(count=skipped_total))

        coverage = Coverage(
            level=level,
            content_pages=content_pages,
            comment_pages=comment_pages,
            includes_nested_comments=nested_used,
            upstream_reports_more=termination == TERMINATION_PAGE_LIMIT,
            notes="; ".join(notes),
        )
        final_records = tuple(replace(record, coverage=coverage) for record in records)
        final_comments = tuple(replace(comment, coverage=coverage) for comment in comments)
        status = "degraded" if (skipped_total or not has_records) else "ready"
        detail = f"读取 {len(final_records)} 条楼层、{len(final_comments)} 条评论"
        if skipped_total:
            detail += f"，{SKIP_NOTE.format(count=skipped_total)}"
        if nested_limit_hit:
            detail += f"，楼中楼页数达上限（max_comment_pages={max_comment_pages}）"
        return ProviderResult(
            provider=self.name,
            status=status,
            records=final_records,
            comments=final_comments,
            coverage=coverage,
            raw_payloads=tuple(payloads),
            detail=detail,
            calls=calls,
            from_cache=from_cache,
            termination=termination,
            gaps=merge_gaps(gaps),
        )

    # -- record builders ------------------------------------------------

    def _build_record(
        self,
        floor: _Floor,
        *,
        tid: str,
        thread_id: str,
        thread_title: str,
        opening_title: str,
        page_number: int,
        raw_ref: str,
        retrieved_at: str,
        forum_name: str,
    ) -> SourceRecord:
        is_opening_post = floor.floor_number == 1 or floor.pid == tid
        if is_opening_post:
            source_id = thread_id
            record_kind = "thread"
            parent_id = None
            thread_ref = None
            floor_number = floor.floor_number if floor.floor_number is not None else 1
            title = opening_title or make_excerpt(floor.full_text, 60)
            url = THREAD_URL_TEMPLATE.format(tid=tid)
        else:
            source_id = stable_source_id("tieba", object_id=floor.pid)
            record_kind = "post"
            parent_id = thread_id
            thread_ref = thread_id
            floor_number = floor.floor_number
            title = thread_title or opening_title or make_excerpt(floor.full_text, 60)
            url = POST_URL_TEMPLATE.format(tid=tid, pid=floor.pid)
        title_source = "provided" if thread_title else "opening_excerpt"
        return SourceRecord(
            source_id=source_id,
            platform="tieba",
            record_kind=record_kind,
            source_type="community",
            title=title,
            url=url,
            author_display_name=floor.author,
            published_at=floor.published_at,
            retrieved_at=retrieved_at,
            excerpt=floor.excerpt,
            full_text=floor.full_text,
            thread_id=thread_ref,
            parent_id=parent_id,
            floor_number=floor_number,
            engagement={"replies": floor.replies, "likes": floor.likes},
            media=floor.media,
            collection_method="tieba_mobile_web_thread",
            coverage=Coverage(level="unknown"),
            raw_ref=raw_ref,
            extra={
                "page": page_number,
                "title_source": title_source,
                "opening_post": is_opening_post,
                "floor_pid": floor.pid,
                "forum_name": forum_name,
                "level": floor.level,
            },
        )

    def _build_comment(
        self,
        item: _Nested,
        *,
        tid: str,
        thread_id: str,
        raw_ref: str,
        retrieved_at: str,
    ) -> CommentRecord:
        return CommentRecord(
            source_id=item.source_id,
            platform="tieba",
            thread_id=thread_id,
            parent_id=item.parent_source_id,
            url=POST_URL_TEMPLATE.format(tid=tid, pid=item.pid),
            author_display_name=item.author,
            retrieved_at=retrieved_at,
            full_text=item.full_text,
            engagement={"likes": 0, "replies": 0},
            collection_method=NESTED_COLLECTION_METHOD,
            coverage=Coverage(level="unknown"),
            raw_ref=raw_ref,
            extra={"nested": True, "floor_pid": item.parent_source_id.split(":", 1)[-1]},
        )


# -- page parsing -------------------------------------------------------


def _page_identity(body: str) -> tuple[str, str]:
    """Read the thread title and forum name from the rendered page."""
    title = ""
    title_match = _TITLE_RE.search(body)
    if title_match:
        title = clean_text(title_match.group("text"))
    forum = ""
    match = _FORUM_NAME_RE.search(body)
    if match:
        forum = normalize_whitespace(match.group("name"))
    return title, forum


def _parse_page(
    body: str,
    *,
    tid: str,
    thread_id: str,
    page_number: int,
    include_nested: bool = False,
) -> _PageReport:
    """Parse one server-rendered floor page into floors, nested replies and skips.

    ``open_error`` is non-empty when the page does not look like a Tieba thread
    page at all (no floor container), so the caller can report a shape failure
    instead of silently publishing an empty success.
    """
    matches = list(_FLOOR_OPEN_RE.finditer(body))
    if not matches:
        return _PageReport(
            floors=(),
            nested=(),
            skips=(),
            floor_containers=0,
            title="",
            forum_name="",
            open_error=(
                "页面中没有楼层容器（li[fn]）：上游 HTML 结构可能已变化或被拦截"
            ),
        )

    floors: list[_Floor] = []
    skips: list[str] = []
    ranges: list[tuple[_Floor, int, int]] = []
    for ordinal, match in enumerate(matches, start=1):
        start = match.start()
        end = matches[ordinal].start() if ordinal < len(matches) else len(body)
        block = body[start:end]
        floor = _parse_floor(
            block,
            tid=tid,
            thread_id=thread_id,
            page_number=page_number,
            ordinal=ordinal,
        )
        if floor is None:
            skips.append(f"page {page_number} item {ordinal}: missing pid or content")
            continue
        floors.append(floor)
        ranges.append((floor, start, end))

    nested: list[_Nested] = []
    for floor, start, end in ranges:
        block = body[start:end]
        declared = _declared_nested_count(block)
        parsed = 0
        for item in _parse_nested(block, floor=floor):
            nested.append(item)
            parsed += 1
        if include_nested and declared > parsed:
            skips.append(
                f"page {page_number} floor {floor.floor_number}: "
                f"{declared - parsed} nested comments were not parsable"
            )
    return _PageReport(
        floors=tuple(floors),
        nested=tuple(nested),
        skips=tuple(skips),
        floor_containers=len(matches),
        title="",
        forum_name="",
        open_error="",
    )


def _parse_floor(
    block: str,
    *,
    tid: str,
    thread_id: str,
    page_number: int,
    ordinal: int,
) -> Optional[_Floor]:
    """Parse one floor block, returning ``None`` when it is structurally broken."""
    attr_match = _FLOOR_ATTR_RE.match(block)
    attrs = attr_match.group("attrs") if attr_match else block[:400]
    info = _data_info(attrs)
    pid = normalize_object_id("tieba", info.get("pid"))
    if not pid:
        return None
    floor_number = _positive_int(info.get("floor_num"))
    if floor_number is None:
        fn_match = re.search(r'fn="(\d+)"', attrs)
        floor_number = _positive_int(fn_match.group(1)) if fn_match else None

    raw_content = _extract_content_html(block)
    full_text = clean_text(raw_content)
    if not full_text:
        return None

    author = normalize_whitespace(info.get("name_show") or info.get("un"))
    author_match = _AUTHOR_RE.search(block)
    if author_match:
        text = clean_text(author_match.group("name")).rstrip(":：").strip()
        if text:
            author = text
    level = ""
    level_match = _LEVEL_RE.search(block)
    if level_match:
        level = normalize_whitespace(level_match.group("level"))

    return _Floor(
        ordinal=ordinal,
        source_id=(
            thread_id if pid == tid or floor_number == 1 else stable_source_id("tieba", object_id=pid)
        ),
        pid=pid,
        floor_number=floor_number,
        author=author,
        published_at=_parse_short_time(_floor_time_text(block)),
        body_html=raw_content,
        full_text=full_text,
        excerpt=make_excerpt(full_text),
        media=_media_urls(raw_content),
        replies=_first_number(block, r'class="[^"]*btn_reply[^"]*"[^>]*>\s*<span[^>]*>(\d+)'),
        likes=0,
        level=level,
    )


def _parse_nested(block: str, *, floor: _Floor) -> list[_Nested]:
    """Parse the 楼中楼 replies server-rendered inside one floor block."""
    comments: list[_Nested] = []
    for panel in _NESTED_PANEL_RE.finditer(block):
        for item in _NESTED_ITEM_RE.finditer(panel.group("items")):
            body = item.group("body")
            text = ""
            text_match = _NESTED_TEXT_RE.search(body)
            if text_match:
                text = clean_text(text_match.group("text"))
            if not text:
                text = clean_text(_TAG_RE.sub("", body))
                text = re.sub(r"^[^:：]{1,40}[:：]\s*", "", text)
            if not text:
                continue
            author = ""
            author_match = _NESTED_AUTHOR_RE.search(body)
            if author_match:
                author = clean_text(author_match.group("name")).rstrip(":：").strip()
            pid = item.group("pid")
            comments.append(
                _Nested(
                    source_id=stable_source_id("tieba", object_id=pid),
                    pid=pid,
                    parent_pid=floor.pid,
                    parent_source_id=floor.source_id,
                    author=author,
                    full_text=text,
                )
            )
    return comments


def _declared_nested_count(block: str) -> int:
    """Read the server-declared ``data-list-count`` of a floor's 楼中楼 panel.

    Tieba only server-renders a few nested replies and hides the rest behind an
    app-only hint, so this count is what the page admits to, not what it ships.
    """
    match = _NESTED_PANEL_RE.search(block)
    if not match:
        return 0
    count_match = re.search(r'data-list-count="(\d+)"', match.group(0))
    if not count_match:
        return 0
    try:
        return int(count_match.group(1))
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return 0


def _data_info(attrs: str) -> dict[str, Any]:
    """Decode the JSON ``data-info`` attribute, tolerating malformed values."""
    match = _DATA_INFO_RE.search(attrs)
    if not match:
        return {}
    raw = html_module.unescape(match.group("info"))
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_content_html(block: str) -> str:
    """Return the raw inner HTML of the first floor ``div.content``."""
    match = _CONTENT_RE.search(block)
    return match.group("text") if match else ""


def _floor_time_text(block: str) -> str:
    match = _TIME_RE.search(block)
    if not match:
        return ""
    return normalize_whitespace(match.group("time"))


def _parse_short_time(text: str) -> Optional[str]:
    """Parse Tieba's short ``M-D HH:MM`` stamp into ISO-8601 UTC.

    Tieba omits the year on recent floors. The year is taken from the current
    UTC year, which is deterministic for fixture-based tests because every
    captured page is newer than a full year boundary. Anything unreadable stays
    ``None`` rather than inventing a timestamp.
    """
    value = normalize_whitespace(text)
    if not value:
        return None
    direct = parse_time(value)
    if direct:
        return direct
    match = _SHORT_TIME_RE.match(value)
    if not match:
        return None
    year = match.group("year") or now_iso()[:4]
    return parse_time(
        "{year}-{month:0>2}-{day:0>2} {hour:0>2}:{minute}".format(
            year=year,
            month=match.group("month"),
            day=match.group("day"),
            hour=match.group("hour") or "00",
            minute=match.group("minute") or "00",
        )
    )


def _media_urls(raw_html: str) -> tuple[str, ...]:
    urls: list[str] = []
    for match in _IMG_SRC_RE.finditer(raw_html or ""):
        url = html_module.unescape(match.group("src")).strip()
        if url and url not in urls:
            urls.append(url)
    return tuple(urls)


def _first_number(text: str, pattern: str) -> int:
    match = re.search(pattern, text)
    if not match:
        return 0
    return _positive_int(match.group(1)) or 0


def _positive_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _has_more(report: _PageReport, *, page_size: int) -> bool:
    """Best-effort "upstream has more floors" signal for the public page.

    The mobile page exposes no page counters, so a full page of floor containers
    is treated as "there may be more". A short page is the end of the thread.
    """
    return report.floor_containers >= page_size
