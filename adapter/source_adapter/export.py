"""Export normalized records into the existing search-agent input contract.

The contract is ``游戏选题搜索知识库-工作版/02 给AI读的/search-agent/input.schema.json``
version ``1.0.0``. This module both builds that payload and validates it, so a
schema drift fails loudly instead of silently producing an unusable bundle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .cache import atomic_write_json
from .models import SOURCE_TYPES, CommentRecord, SourceRecord
from .normalize import make_excerpt, normalize_whitespace

SCHEMA_VERSION = "1.0.0"
MODES = ("discover", "compare", "validate")
CONSTRAINT_KEYS = (
    "platforms",
    "regions",
    "audiences",
    "date_from",
    "date_to",
    "must_include",
    "must_exclude",
)
RESULT_KEYS = (
    "source_id",
    "title",
    "source_type",
    "url",
    "local_path",
    "publisher",
    "author",
    "published_at",
    "retrieved_at",
    "excerpt",
    "full_text",
    "metadata",
)
TOP_LEVEL_KEYS = (
    "schema_version",
    "request_id",
    "query",
    "mode",
    "locale",
    "constraints",
    "results",
)
_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\S+$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")


class SchemaError(ValueError):
    """Raised when a payload violates the search-agent input contract."""


def build_search_agent_input(
    query: str,
    records: Iterable[SourceRecord],
    *,
    request_id: str,
    mode: str = "discover",
    locale: str = "zh-CN",
    constraints: Optional[Mapping[str, Any]] = None,
    comments: Iterable[CommentRecord] = (),
) -> dict[str, Any]:
    """Build a schema-compatible evidence bundle from normalized records."""
    if not str(query or "").strip():
        raise SchemaError("query must not be blank")
    if not str(request_id or "").strip():
        raise SchemaError("request_id must not be blank")
    if mode not in MODES:
        raise SchemaError(f"mode must be one of {MODES}, got {mode!r}")

    comment_counts: dict[str, int] = {}
    for comment in comments:
        key = comment.thread_id or comment.parent_id or ""
        if key:
            comment_counts[key] = comment_counts.get(key, 0) + 1

    results: list[dict[str, Any]] = []
    for record in records:
        if record.is_comment():
            continue
        results.append(_to_result(record, comment_counts))

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": str(request_id),
        "query": str(query),
        "mode": mode,
        "locale": locale,
        "results": results,
    }
    if constraints:
        payload["constraints"] = _clean_constraints(constraints)
    validate_search_agent_input(payload)
    return payload


def _to_result(record: SourceRecord, comment_counts: Mapping[str, int]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "platform": record.platform,
        "record_kind": record.record_kind,
        "thread_id": record.thread_id,
        "parent_id": record.parent_id,
        "floor_number": record.floor_number,
        "collection_method": record.collection_method,
        "coverage": record.coverage.to_dict(),
        "raw_ref": record.raw_ref,
        "engagement": dict(record.engagement),
        "media": list(record.media),
        "status": record.status,
        "comment_count": comment_counts.get(record.source_id, 0),
    }
    if record.extra:
        metadata["extra"] = dict(record.extra)

    excerpt = normalize_whitespace(record.excerpt)
    if not (excerpt or record.full_text or record.url):
        # A search result may expose only a title. The title is observed text,
        # so it is used as the excerpt and the substitution is flagged.
        excerpt = normalize_whitespace(record.title)
        metadata["excerpt_source"] = "title"

    item: dict[str, Any] = {
        "source_id": record.source_id,
        "title": record.title or excerpt or record.url,
        "source_type": record.source_type,
        "excerpt": excerpt,
    }
    if record.url:
        item["url"] = record.url
    if record.publisher:
        item["publisher"] = record.publisher
    if record.author_display_name:
        item["author"] = record.author_display_name
    if record.published_at:
        item["published_at"] = record.published_at
    if record.retrieved_at:
        item["retrieved_at"] = record.retrieved_at
    if record.full_text:
        item["full_text"] = record.full_text
    item["metadata"] = metadata
    return item


def _clean_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key in CONSTRAINT_KEYS:
        value = constraints.get(key)
        if value in (None, "", [], {}):
            continue
        cleaned[key] = value
    unknown = set(constraints) - set(CONSTRAINT_KEYS)
    if unknown:
        raise SchemaError(f"unknown constraint keys: {sorted(unknown)}")
    return cleaned


def validate_search_agent_input(payload: Any) -> None:
    """Validate a payload against the search-agent input schema (1.0.0)."""
    if not isinstance(payload, Mapping):
        raise SchemaError("payload must be a JSON object")
    unknown = set(payload) - set(TOP_LEVEL_KEYS)
    if unknown:
        raise SchemaError(f"unexpected top-level keys: {sorted(unknown)}")
    for key in ("schema_version", "request_id", "query", "results"):
        if key not in payload:
            raise SchemaError(f"missing required top-level key: {key}")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {payload['schema_version']!r}"
        )
    for key in ("request_id", "query"):
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            raise SchemaError(f"{key} must be a non-empty string")
    if "mode" in payload and payload["mode"] not in MODES:
        raise SchemaError(f"mode must be one of {MODES}, got {payload['mode']!r}")
    if "locale" in payload and not isinstance(payload["locale"], str):
        raise SchemaError("locale must be a string")
    if "constraints" in payload:
        constraints = payload["constraints"]
        if not isinstance(constraints, Mapping):
            raise SchemaError("constraints must be an object")
        unknown_constraints = set(constraints) - set(CONSTRAINT_KEYS)
        if unknown_constraints:
            raise SchemaError(f"unexpected constraint keys: {sorted(unknown_constraints)}")
    results = payload["results"]
    if not isinstance(results, list):
        raise SchemaError("results must be an array")
    for index, item in enumerate(results):
        _validate_result(item, index)


def _validate_result(item: Any, index: int) -> None:
    where = f"results[{index}]"
    if not isinstance(item, Mapping):
        raise SchemaError(f"{where} must be an object")
    unknown = set(item) - set(RESULT_KEYS)
    if unknown:
        raise SchemaError(f"{where} has unexpected keys: {sorted(unknown)}")
    for key in ("source_id", "title", "source_type"):
        if key not in item:
            raise SchemaError(f"{where} is missing required key: {key}")
    for key in ("source_id", "title"):
        value = item[key]
        if not isinstance(value, str) or not value.strip():
            raise SchemaError(f"{where}.{key} must be a non-empty string")
    if item["source_type"] not in SOURCE_TYPES:
        raise SchemaError(
            f"{where}.source_type must be one of {SOURCE_TYPES}, got {item['source_type']!r}"
        )
    if "url" in item and not _URI_RE.match(str(item["url"])):
        raise SchemaError(f"{where}.url must be an absolute URI, got {item['url']!r}")
    for key in ("published_at", "retrieved_at"):
        if key in item and not _DATETIME_RE.match(str(item[key])):
            raise SchemaError(f"{where}.{key} must be an ISO-8601 date-time, got {item[key]!r}")
    if "metadata" in item and not isinstance(item["metadata"], Mapping):
        raise SchemaError(f"{where}.metadata must be an object")
    for key in ("excerpt", "full_text", "local_path", "url"):
        if key in item and item[key] not in (None, ""):
            return
    raise SchemaError(f"{where} needs one of excerpt, full_text, local_path or url")


def export_search_agent_input(path: Path, payload: Mapping[str, Any]) -> Path:
    """Validate and atomically write a search-agent input bundle."""
    validate_search_agent_input(payload)
    return atomic_write_json(Path(path), payload)


def build_comment_tree(comments: Sequence[CommentRecord], root_id: str) -> list[dict[str, Any]]:
    """Build a nested comment tree rooted at ``root_id``.

    Comments caught in a parent cycle are surfaced as roots instead of being
    nested, so malformed upstream data can never hide a comment.
    """
    by_id = {comment.source_id: comment for comment in comments}
    cyclic = {comment.source_id for comment in comments if _in_cycle(comment, by_id)}
    children: dict[str, list[CommentRecord]] = {}
    roots: list[CommentRecord] = []
    for comment in comments:
        parent = comment.parent_id
        if comment.source_id in cyclic:
            roots.append(comment)
            continue
        if (
            parent
            and parent != root_id
            and parent != comment.source_id
            and parent in by_id
            and parent not in cyclic
        ):
            children.setdefault(parent, []).append(comment)
        else:
            roots.append(comment)

    def node(comment: CommentRecord, seen: frozenset[str]) -> dict[str, Any]:
        next_seen = seen | {comment.source_id}
        return {
            "record": comment.to_dict(),
            "children": [
                node(child, next_seen)
                for child in children.get(comment.source_id, [])
                if child.source_id not in next_seen
            ],
        }

    return [node(comment, frozenset()) for comment in roots]


def _in_cycle(comment: CommentRecord, by_id: Mapping[str, CommentRecord]) -> bool:
    seen = {comment.source_id}
    parent = comment.parent_id
    while parent and parent in by_id:
        if parent in seen:
            return True
        seen.add(parent)
        parent = by_id[parent].parent_id
    return False


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


__all__ = [
    "SCHEMA_VERSION",
    "SchemaError",
    "build_comment_tree",
    "build_search_agent_input",
    "export_search_agent_input",
    "load_json",
    "validate_search_agent_input",
]
