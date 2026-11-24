"""Immutable evidence model for the unified source adapter.

A :class:`SourceRecord` is the single normalized shape for every collected
object: a Tieba thread, a Bilibili video, a Zhihu answer, an extracted web page
or a single comment. :class:`CommentRecord` is the lighter shape used while
building comment trees; it converts into a ``SourceRecord`` for storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional

from .normalize import (
    make_excerpt,
    normalize_object_id,
    normalize_url,
    sha256_hex,
)

COVERAGE_LEVELS = ("complete", "partial", "unknown", "failed")
COVERAGE_RANK = {"failed": 0, "unknown": 1, "partial": 2, "complete": 3}

PROVIDER_STATUSES = ("ready", "degraded", "unavailable")
PROVIDER_STATUS_RANK = {"unavailable": 0, "degraded": 1, "ready": 2}

RECORD_STATUSES = ("ok", "degraded", "failed")
RECORD_STATUS_RANK = {"failed": 0, "degraded": 1, "ok": 2}

RECORD_KINDS = ("thread", "post", "answer", "question", "video", "page", "comment")

SOURCE_TYPES = (
    "official",
    "report",
    "article",
    "interview",
    "store",
    "database",
    "community",
    "video",
    "knowledge_base",
    "other",
)

PLATFORM_SOURCE_TYPE = {
    "tieba": "community",
    "zhihu": "community",
    "weibo": "community",
    "xiaohongshu": "community",
    "douban": "community",
    "bilibili": "video",
    "douyin": "video",
    "kuaishou": "video",
    "anysearch": "article",
    "web": "article",
}


def platform_source_type(platform: str) -> str:
    """Map a platform name onto the search-agent source type enum."""
    return PLATFORM_SOURCE_TYPE.get(str(platform).strip().lower(), "other")


@dataclass(frozen=True, slots=True)
class Coverage:
    """How complete a record is relative to the upstream object."""

    level: str = "unknown"
    discovered_pages: int = 0
    content_pages: int = 0
    comment_pages: int = 0
    includes_nested_comments: bool = False
    upstream_reports_more: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if self.level not in COVERAGE_LEVELS:
            raise ValueError(f"coverage level must be one of {COVERAGE_LEVELS}, got {self.level!r}")
        for name in ("discovered_pages", "content_pages", "comment_pages"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"coverage {name} must be a non-negative integer, got {value!r}")

    def rank(self) -> int:
        return COVERAGE_RANK[self.level]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "discovered_pages": self.discovered_pages,
            "content_pages": self.content_pages,
            "comment_pages": self.comment_pages,
            "includes_nested_comments": self.includes_nested_comments,
            "upstream_reports_more": self.upstream_reports_more,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Coverage":
        data = dict(payload or {})
        return cls(
            level=data.get("level", "unknown"),
            discovered_pages=int(data.get("discovered_pages", 0) or 0),
            content_pages=int(data.get("content_pages", 0) or 0),
            comment_pages=int(data.get("comment_pages", 0) or 0),
            includes_nested_comments=bool(data.get("includes_nested_comments", False)),
            upstream_reports_more=bool(data.get("upstream_reports_more", False)),
            notes=str(data.get("notes") or ""),
        )


def merge_coverage(first: Coverage, second: Coverage) -> Coverage:
    """Merge two coverage reports, keeping the most complete evidence."""
    winner, loser = (first, second) if first.rank() >= second.rank() else (second, first)
    notes = []
    for candidate in (first.notes, second.notes):
        if candidate and candidate not in notes:
            notes.append(candidate)
    return Coverage(
        level=winner.level,
        discovered_pages=max(first.discovered_pages, second.discovered_pages),
        content_pages=max(first.content_pages, second.content_pages),
        comment_pages=max(first.comment_pages, second.comment_pages),
        includes_nested_comments=first.includes_nested_comments or second.includes_nested_comments,
        upstream_reports_more=first.upstream_reports_more or second.upstream_reports_more,
        notes="; ".join(notes),
    )


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One normalized piece of evidence with provenance and completeness."""

    source_id: str
    platform: str
    record_kind: str
    source_type: str
    title: str
    url: str = ""
    author_display_name: str = ""
    published_at: Optional[str] = None
    retrieved_at: Optional[str] = None
    excerpt: str = ""
    full_text: str = ""
    publisher: str = ""
    thread_id: Optional[str] = None
    parent_id: Optional[str] = None
    floor_number: Optional[int] = None
    engagement: Mapping[str, Any] = field(default_factory=dict)
    media: tuple[str, ...] = ()
    collection_method: str = ""
    coverage: Coverage = field(default_factory=Coverage)
    raw_ref: str = ""
    status: str = "ok"
    error: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_id or "").strip():
            raise ValueError("source_id must not be blank")
        if not str(self.platform or "").strip():
            raise ValueError("platform must not be blank")
        if self.record_kind not in RECORD_KINDS:
            raise ValueError(f"record_kind must be one of {RECORD_KINDS}, got {self.record_kind!r}")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {SOURCE_TYPES}, got {self.source_type!r}")
        if self.status not in RECORD_STATUSES:
            raise ValueError(f"status must be one of {RECORD_STATUSES}, got {self.status!r}")
        if self.record_kind != "comment" and not str(self.title or "").strip():
            raise ValueError("title must not be blank for non-comment records")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("coverage must be a Coverage instance")
        if self.record_kind == "comment" and not (self.excerpt or self.full_text or self.url):
            if self.status != "failed":
                raise ValueError(
                    "comment record needs excerpt, full_text or url unless it is a failed record"
                )
        if self.floor_number is not None:
            if isinstance(self.floor_number, bool) or not isinstance(self.floor_number, int):
                raise ValueError("floor_number must be an integer or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "platform": self.platform,
            "record_kind": self.record_kind,
            "source_type": self.source_type,
            "title": self.title,
            "url": self.url,
            "author_display_name": self.author_display_name,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "excerpt": self.excerpt,
            "full_text": self.full_text,
            "publisher": self.publisher,
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "floor_number": self.floor_number,
            "engagement": dict(self.engagement),
            "media": list(self.media),
            "collection_method": self.collection_method,
            "coverage": self.coverage.to_dict(),
            "raw_ref": self.raw_ref,
            "status": self.status,
            "error": self.error,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceRecord":
        data = dict(payload or {})
        return cls(
            source_id=str(data.get("source_id", "")),
            platform=str(data.get("platform", "")),
            record_kind=str(data.get("record_kind", "post")),
            source_type=str(data.get("source_type", "other")),
            title=str(data.get("title", "")),
            url=str(data.get("url") or ""),
            author_display_name=str(data.get("author_display_name") or ""),
            published_at=data.get("published_at"),
            retrieved_at=data.get("retrieved_at"),
            excerpt=str(data.get("excerpt") or ""),
            full_text=str(data.get("full_text") or ""),
            publisher=str(data.get("publisher") or ""),
            thread_id=data.get("thread_id"),
            parent_id=data.get("parent_id"),
            floor_number=data.get("floor_number"),
            engagement=dict(data.get("engagement") or {}),
            media=tuple(data.get("media") or ()),
            collection_method=str(data.get("collection_method") or ""),
            coverage=Coverage.from_dict(data.get("coverage") or {}),
            raw_ref=str(data.get("raw_ref") or ""),
            status=str(data.get("status", "ok")),
            error=data.get("error"),
            extra=dict(data.get("extra") or {}),
        )

    def is_comment(self) -> bool:
        return self.record_kind == "comment"


@dataclass(frozen=True, slots=True)
class CommentRecord:
    """A reply or floor with an explicit parent relationship."""

    source_id: str
    platform: str
    thread_id: str
    full_text: str
    parent_id: Optional[str] = None
    floor_number: Optional[int] = None
    url: str = ""
    author_display_name: str = ""
    published_at: Optional[str] = None
    retrieved_at: Optional[str] = None
    engagement: Mapping[str, Any] = field(default_factory=dict)
    media: tuple[str, ...] = ()
    collection_method: str = ""
    coverage: Coverage = field(default_factory=Coverage)
    raw_ref: str = ""
    status: str = "ok"
    error: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_id or "").strip():
            raise ValueError("comment source_id must not be blank")
        if not str(self.platform or "").strip():
            raise ValueError("comment platform must not be blank")
        if not str(self.thread_id or "").strip():
            raise ValueError("comment thread_id must not be blank")
        if self.status not in RECORD_STATUSES:
            raise ValueError(f"status must be one of {RECORD_STATUSES}, got {self.status!r}")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("coverage must be a Coverage instance")

    def to_source_record(self) -> SourceRecord:
        return SourceRecord(
            source_id=self.source_id,
            platform=self.platform,
            record_kind="comment",
            source_type=platform_source_type(self.platform),
            title="",
            url=self.url,
            author_display_name=self.author_display_name,
            published_at=self.published_at,
            retrieved_at=self.retrieved_at,
            excerpt=make_excerpt(self.full_text),
            full_text=self.full_text,
            thread_id=self.thread_id,
            parent_id=self.parent_id,
            floor_number=self.floor_number,
            engagement=dict(self.engagement),
            media=tuple(self.media),
            collection_method=self.collection_method,
            coverage=self.coverage,
            raw_ref=self.raw_ref,
            status=self.status,
            error=self.error,
            extra=dict(self.extra),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_source_record().to_dict()


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Local capability report for one provider."""

    provider: str
    status: str
    detail: str = ""
    capabilities: tuple[str, ...] = ()
    checked_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in PROVIDER_STATUSES:
            raise ValueError(
                f"provider status must be one of {PROVIDER_STATUSES}, got {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "capabilities": list(self.capabilities),
            "checked_at": self.checked_at,
        }


def worst_provider_status(statuses: Iterable[ProviderStatus]) -> str:
    """Return the weakest status across providers."""
    weakest_rank = None
    for status in statuses:
        rank = PROVIDER_STATUS_RANK[status.status]
        if weakest_rank is None or rank < weakest_rank:
            weakest_rank = rank
    if weakest_rank is None:
        return "unavailable"
    for name, rank in PROVIDER_STATUS_RANK.items():
        if rank == weakest_rank:
            return name
    return "unavailable"


def stable_source_id(
    platform: str,
    *,
    object_id: Any = None,
    url: Any = None,
    title: Any = None,
    author: Any = None,
    published_at: Any = None,
) -> str:
    """Build a deterministic record id, preferring the platform object id."""
    platform_key = str(platform or "").strip().lower()
    if not platform_key:
        raise ValueError("platform must not be blank")
    normalized_id = normalize_object_id(platform_key, object_id) if object_id is not None else ""
    if normalized_id:
        return f"{platform_key}:{normalized_id}"
    normalized_url = normalize_url(url)
    if normalized_url:
        return f"{platform_key}:url:{sha256_hex(normalized_url)[:16]}"
    seed = "\x1f".join(
        [
            platform_key,
            str(title or "").strip(),
            str(author or "").strip(),
            str(published_at or "").strip(),
        ]
    )
    if seed == platform_key + "\x1f\x1f\x1f":
        raise ValueError("cannot build a stable id without an object id, url or text identity")
    return f"{platform_key}:h:{sha256_hex(seed)[:16]}"


def _richness(record: SourceRecord) -> tuple[int, int, int, int, int]:
    return (
        len(record.full_text or ""),
        len(record.excerpt or ""),
        record.coverage.rank(),
        len(record.media),
        len(record.title or ""),
    )


def _merge_engagement(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(first)
    for key, value in (second or {}).items():
        if key not in merged:
            merged[key] = value
            continue
        current = merged[key]
        numeric = (
            isinstance(current, (int, float))
            and not isinstance(current, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        if numeric:
            merged[key] = max(current, value)
    return merged


def merge_records(first: SourceRecord, second: SourceRecord) -> SourceRecord:
    """Merge two versions of the same record, keeping the richer evidence."""
    if first.source_id != second.source_id:
        raise ValueError(
            f"cannot merge records with different ids: {first.source_id!r} vs {second.source_id!r}"
        )
    winner, loser = (first, second) if _richness(first) >= _richness(second) else (second, first)
    raw_refs = [
        ref for ref in (first.raw_ref, second.raw_ref) if ref
    ]
    raw_ref = "; ".join(dict.fromkeys(raw_refs))
    if first.status == "ok" or second.status == "ok":
        status = "ok"
    else:
        status = winner.status if RECORD_STATUS_RANK[winner.status] >= RECORD_STATUS_RANK[loser.status] else loser.status
    error = None if status == "ok" else (winner.error or loser.error)
    coverage = merge_coverage(first.coverage, second.coverage)
    extra = dict(loser.extra)
    extra.update(dict(winner.extra))

    def pick(attribute: str, fallback_default: Any = "") -> Any:
        value = getattr(winner, attribute)
        if value not in (None, "", (), {}):
            return value
        other = getattr(loser, attribute)
        return other if other not in (None, "", (), {}) else fallback_default

    return replace(
        winner,
        url=pick("url"),
        author_display_name=pick("author_display_name"),
        publisher=pick("publisher"),
        published_at=pick("published_at", None),
        retrieved_at=_later_timestamp(first.retrieved_at, second.retrieved_at),
        excerpt=pick("excerpt"),
        full_text=pick("full_text"),
        thread_id=pick("thread_id", None),
        parent_id=pick("parent_id", None),
        floor_number=pick("floor_number", None),
        engagement=_merge_engagement(first.engagement, second.engagement),
        media=pick("media", ()),
        collection_method=pick("collection_method"),
        coverage=coverage,
        raw_ref=raw_ref,
        status=status,
        error=error,
        extra=extra,
    )


def _later_timestamp(first: Optional[str], second: Optional[str]) -> Optional[str]:
    values = [value for value in (first, second) if value]
    if not values:
        return None
    return max(values)


def dedupe_records(records: Iterable[SourceRecord]) -> list[SourceRecord]:
    """Merge duplicate records, preserving first-seen order."""
    order: list[str] = []
    merged: dict[str, SourceRecord] = {}
    for record in records:
        if record.source_id in merged:
            merged[record.source_id] = merge_records(merged[record.source_id], record)
            continue
        order.append(record.source_id)
        merged[record.source_id] = record
    return [merged[source_id] for source_id in order]
