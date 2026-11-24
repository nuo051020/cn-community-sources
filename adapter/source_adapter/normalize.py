"""Text, URL, time and secret normalization helpers.

Everything in this module is pure and dependency-free so it can be unit tested
without a network or a clock.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only carry campaign or referral tracking noise.
_TRACKING_PREFIXES = ("utm_", "spm", "share_", "from_", "scm_")
_TRACKING_EXACT = frozenset(
    {"from", "fr", "refer", "referer", "referrer", "msource", "is_jump", "sourcefrom"}
)
_DEFAULT_PORTS = {"http": "80", "https": "443"}

_EPOCH_RE = re.compile(r"^\d{9,16}$")
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
)

_CJK_RANGES = (
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xAC00, 0xD7AF),
)

_SECRET_KEY_WORD = r"(?:api[_-]?key|apikey|access[_-]?key|token|secret|password|passwd|credential)"
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{4,}")
_JSON_KV_RE = re.compile(
    r"(?i)([\"']?[A-Za-z0-9_]*" + _SECRET_KEY_WORD + r"[\"']?\s*[:=]\s*)([\"'])([^\"']{1,})([\"'])"
)
_PLAIN_KV_RE = re.compile(
    r"(?i)([\"']?[A-Za-z0-9_]*" + _SECRET_KEY_WORD + r"[\"']?\s*[:=]\s*)"
    r"(?!bearer\b)(?!<redacted>)([^\s\"',}]{4,})"
)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9._\-]{16,}\b")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

REDACTED = "<redacted>"


def sha256_hex(text: str) -> str:
    """Return the hex SHA-256 digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_whitespace(text: Any) -> str:
    """Collapse all whitespace runs into single spaces and trim."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def truncate_text(text: Any, limit: int, suffix: str = "…") -> str:
    """Trim ``text`` to ``limit`` characters, appending ``suffix`` when cut."""
    value = "" if text is None else str(text)
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + suffix


def make_excerpt(text: Any, limit: int = 200) -> str:
    """Build a normalized, length-bounded excerpt from arbitrary text."""
    return truncate_text(normalize_whitespace(text), limit)


def normalize_url(url: Any) -> str:
    """Normalize a URL for identity and cache keys.

    Lowercases the scheme and host, drops userinfo, default ports, fragments and
    tracking parameters, sorts the remaining query parameters and trims a
    trailing slash from the path.
    """
    raw = normalize_whitespace(url)
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    else:
        netloc = host
    pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in _TRACKING_EXACT or lowered.startswith(_TRACKING_PREFIXES):
            continue
        pairs.append((key, value))
    pairs.sort()
    path = parts.path or ""
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path == "/":
        path = ""
    return urlunsplit((scheme, netloc, path, urlencode(pairs), ""))


def normalize_object_id(platform: str, object_id: Any) -> str:
    """Normalize a platform object id without destroying case-sensitive ids."""
    value = normalize_whitespace(object_id)
    if not value:
        return ""
    if ":" in value and not value.lower().startswith(("http:", "https:")):
        head, _, tail = value.partition(":")
        if tail and head and not any(ch.isdigit() for ch in head):
            value = tail.strip()
    lowered = value.lower()
    if lowered.startswith("bv"):
        return "BV" + value[2:]
    if lowered.startswith("av"):
        return "av" + value[2:]
    return value


def normalize_query_params(params: Optional[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Return deterministic, stringified query parameters for cache keys."""
    if not params:
        return ()
    items = []
    for key, value in params.items():
        items.append((str(key), "" if value is None else str(value)))
    items.sort()
    return tuple(items)


def parse_time(value: Any) -> Optional[str]:
    """Parse unix timestamps or common date strings into ``...Z`` ISO-8601 UTC.

    Returns ``None`` for anything that cannot be read as an absolute time, such
    as the relative strings search engines print.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = normalize_whitespace(value)
    if not text:
        return None
    if _EPOCH_RE.match(text):
        return _from_epoch(float(text))
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_epoch(value: float) -> Optional[str]:
    if value <= 0:
        return None
    if value >= 1e11:  # milliseconds
        value = value / 1000.0
    try:
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_html(text: Any) -> str:
    """Convert a small HTML fragment into plain text with line breaks."""
    if text is None:
        return ""
    value = str(text)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6]|blockquote)\s*>", "\n", value)
    value = re.sub(r"(?i)<(?:p|div|li|tr|h[1-6]|blockquote)[^>]*>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    lines = [normalize_whitespace(line) for line in value.split("\n")]
    return "\n".join(line for line in lines if line)


def slugify(text: Any, limit: int = 40) -> str:
    """Build a filesystem-safe slug that keeps CJK characters readable."""
    value = normalize_whitespace(text).lower()
    if not value:
        return "run"
    chars = []
    for char in value:
        if char.isalnum() or _is_cjk(char):
            chars.append(char)
        else:
            chars.append("-")
    slug = re.sub(r"-+", "-", "".join(chars)).strip("-")
    if not slug:
        return "run"
    return slug[:limit].strip("-") or "run"


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def iter_chunks(items: Iterable[Any], size: int) -> Iterable[list]:
    """Yield lists of at most ``size`` items."""
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def redact_secrets(text: Any) -> str:
    """Remove credential-looking values from logs, errors and reports."""
    if text is None:
        return ""
    value = str(text)
    value = _BEARER_RE.sub("Bearer " + REDACTED, value)
    value = _JSON_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(4)}", value)
    value = _PLAIN_KV_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    value = _SK_RE.sub(REDACTED, value)
    value = _JWT_RE.sub(REDACTED, value)
    value = _LONG_HEX_RE.sub(REDACTED, value)
    return value


def normalize_for_nfc(text: Any) -> str:
    """Normalize unicode composition so identical text hashes identically."""
    return unicodedata.normalize("NFC", "" if text is None else str(text))
