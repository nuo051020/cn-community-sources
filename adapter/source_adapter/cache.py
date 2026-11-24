"""Atomic file cache and path-safety helpers.

Cache keys are derived from the provider plus normalized parameters, and every
write goes through a temporary file in the destination directory followed by
``os.replace`` so a crashed run can never leave a half-written response.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

_TEMP_COUNTER = itertools.count(1)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    body: bytes
    meta: Mapping[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class FileCache:
    """Content-addressed cache for raw upstream responses."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, provider: str, namespace: str, params: Mapping[str, Any]) -> str:
        seed = _canonical({"provider": provider, "namespace": namespace, "params": dict(params or {})})
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        safe = _safe_key(key)
        return self.root / safe[:2] / f"{safe}.bin"

    def meta_path_for(self, key: str) -> Path:
        safe = _safe_key(key)
        return self.root / safe[:2] / f"{safe}.meta.json"

    def has(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def get(self, key: str) -> Optional[CacheEntry]:
        path = self.path_for(key)
        if not path.is_file():
            return None
        body = path.read_bytes()
        meta: dict[str, Any] = {}
        meta_path = self.meta_path_for(key)
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        return CacheEntry(key=key, body=body, meta=meta, path=path)

    def put(self, key: str, body: bytes, meta: Optional[Mapping[str, Any]] = None) -> Path:
        path = atomic_write_bytes(self.path_for(key), body)
        if meta is not None:
            atomic_write_json(self.meta_path_for(key), dict(meta))
        return path

    def stats(self) -> dict[str, Any]:
        entries = [path for path in self.root.rglob("*.bin")]
        total = sum(path.stat().st_size for path in entries)
        return {"root": str(self.root), "entries": len(entries), "bytes": total}


def _safe_key(key: str) -> str:
    text = str(key or "").strip()
    if not text or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("cache key must be a lowercase hex digest")
    return text


def safe_join(root: Path, *parts: str) -> Path:
    """Join ``parts`` under ``root``, refusing traversal and symlink escapes."""
    root_path = Path(root)
    try:
        resolved_root = root_path.resolve()
    except OSError:  # pragma: no cover - defensive only
        resolved_root = root_path.absolute()
    relative = Path()
    for part in parts:
        text = str(part)
        if not text or "\x00" in text:
            raise ValueError("path component must not be empty or contain NUL")
        piece = Path(text)
        if piece.is_absolute():
            raise ValueError(f"absolute path components are refused: {text!r}")
        if any(component == ".." for component in piece.parts):
            raise ValueError(f"path traversal is refused: {text!r}")
        relative = relative / piece
    candidate = root_path / relative
    # Only directories *inside* the output root are checked for symlinks; the
    # root itself may legitimately live under a symlinked prefix such as macOS
    # ``/var`` -> ``/private/var``.
    current = root_path
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"symlinked output paths are refused: {current}")
    resolved_candidate = candidate.resolve()
    if resolved_root != resolved_candidate and resolved_root not in resolved_candidate.parents:
        raise ValueError(f"path escapes the output root: {candidate}")
    return candidate


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """Write bytes atomically via a sibling temp file and ``os.replace``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{next(_TEMP_COUNTER)}")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover - defensive only
                pass
    return target


def atomic_write_text(path: Path, text: str) -> Path:
    return atomic_write_bytes(path, str(text).encode("utf-8"))


def atomic_write_json(path: Path, payload: Any) -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return atomic_write_text(path, text)


def atomic_write_jsonl(path: Path, rows: Any) -> Path:
    lines = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    return atomic_write_text(path, lines)
