"""Provider primitives shared by Tieba, AnySearch and the Just One bridge."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable

from ..models import (
    PROVIDER_STATUSES,
    CommentRecord,
    Coverage,
    ProviderStatus,
    SourceRecord,
)


@dataclass(frozen=True, slots=True)
class RawPayload:
    """An untouched upstream response kept next to the normalized output."""

    name: str
    content: bytes
    media_type: str = "application/json"

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "media_type": self.media_type,
            "bytes": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Normalized outcome of one provider call."""

    provider: str
    status: str
    records: tuple[SourceRecord, ...] = ()
    comments: tuple[CommentRecord, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    raw_payloads: tuple[RawPayload, ...] = ()
    error: Optional[str] = None
    detail: str = ""
    calls: int = 0
    from_cache: int = 0
    termination: str = "unknown"
    gaps: tuple[str, ...] = ()
    audit: tuple[Mapping[str, Any], ...] = ()
    paid_calls: int = 0
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in PROVIDER_STATUSES:
            raise ValueError(
                f"provider status must be one of {PROVIDER_STATUSES}, got {self.status!r}"
            )

    def has_evidence(self) -> bool:
        return bool(self.records or self.comments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "error": self.error,
            "detail": self.detail,
            "calls": self.calls,
            "from_cache": self.from_cache,
            "termination": self.termination,
            "gaps": list(self.gaps),
            "coverage": self.coverage.to_dict(),
            "records": len(self.records),
            "comments": len(self.comments),
            "raw_refs": [payload.to_dict() for payload in self.raw_payloads],
            "paid_calls": self.paid_calls,
            "cost": dict(self.cost),
            "audit": [dict(entry) for entry in self.audit],
        }


@runtime_checkable
class Provider(Protocol):
    name: str

    def doctor(self) -> ProviderStatus:  # pragma: no cover - protocol only
        ...


def jsonable(value: Any) -> Any:
    """Best-effort conversion of SDK objects into JSON-serializable values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return jsonable(dataclasses.asdict(value))
        except Exception:  # pragma: no cover - defensive only
            pass
    for attribute in ("__dict__", "_asdict"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, Mapping):
            return {str(key): jsonable(item) for key, item in candidate.items() if not str(key).startswith("_")}
        if callable(candidate):
            try:
                return jsonable(candidate())
            except Exception:  # pragma: no cover - defensive only
                pass
    return str(value)


def json_bytes(value: Any) -> bytes:
    """Serialize an SDK object to canonical UTF-8 JSON bytes."""
    return json.dumps(jsonable(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def first_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first present, non-empty attribute from ``names``."""
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, "", (), [], {}):
            return value
    return default


def clean_text(value: Any) -> str:
    """Normalize a possibly-HTML upstream text field into plain text."""
    from ..normalize import normalize_whitespace, strip_html

    if value is None:
        return ""
    text = str(value)
    if "<" in text and ">" in text:
        text = strip_html(text)
    return normalize_whitespace(text)


def merge_gaps(*groups: Iterable[str]) -> tuple[str, ...]:
    """Union gap messages while keeping their first-seen order."""
    ordered: list[str] = []
    for group in groups:
        for item in group or ():
            if item and item not in ordered:
                ordered.append(item)
    return tuple(ordered)
