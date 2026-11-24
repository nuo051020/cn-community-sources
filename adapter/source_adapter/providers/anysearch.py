"""AnySearch bridge: drives the installed AnySearch skill CLI.

The adapter never reads or copies the API key. It invokes the CLI that already
owns key handling and normalizes its stdout (Markdown by default, JSON when the
service returns structured content).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ..models import Coverage, ProviderStatus, SourceRecord, stable_source_id
from ..normalize import make_excerpt, normalize_whitespace, now_iso, parse_time, redact_secrets
from .base import ProviderResult, RawPayload

DEFAULT_SKILL_DIR = Path.home() / ".codex" / "skills" / "anysearch"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_OUTPUT = 500_000
DISCOVERY_NOTE = "AnySearch 仅返回搜索摘要，正文请使用 anysearch-extract 或网页正文提取"
TRUNCATION_NOTE = "CLI 输出被截断（超出 max_output）"
BLOCKED_NOTE = "正文提取疑似被登录墙或安全验证拦截，不作为正文证据"
MIN_USEFUL_BODY = 200
BLOCKED_MARKERS = (
    "滑动验证",
    "安全验证",
    "请登录",
    "登录后",
    "登录以继续",
    "开启 javascript",
    "enable javascript",
    "just a moment",
    "attention required",
    "访问过于频繁",
    "内容不存在",
)

_HEADER_RE = re.compile(r"^###\s+(?P<rank>\d+)\s*[.、]\s*(?P<title>.*)$")
_URL_LINE_RE = re.compile(r"^\s*[-*]\s*\*\*URL\*\*\s*:\s*(?P<url>\S+)\s*$", re.IGNORECASE)
_META_KEY_RE = re.compile(
    r"(?P<key>date|rating_count|rating|duration|views|price|author|site)\s*:\s*", re.IGNORECASE
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,2}\s+(?P<title>\S.*)$")


def parse_anysearch_markdown(text: str) -> list[dict[str, Any]]:
    """Parse the CLI's Markdown result list into structured items."""
    results: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    def flush() -> None:
        if current is None:
            return
        snippet, meta = _split_meta(normalize_whitespace(current.pop("snippet")))
        current["snippet"] = snippet
        current["meta"] = meta
        results.append(current)

    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        header = _HEADER_RE.match(line.strip())
        if header:
            flush()
            current = {
                "rank": int(header.group("rank")),
                "title": normalize_whitespace(header.group("title")),
                "url": "",
                "snippet": "",
            }
            continue
        if current is None:
            continue
        url_line = _URL_LINE_RE.match(line)
        if url_line:
            current["url"] = url_line.group("url").strip()
            continue
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            stripped = stripped[2:]
        if stripped:
            current["snippet"] = f"{current['snippet']} {stripped}".strip()
    flush()
    return results


def parse_anysearch_json(text: str) -> list[dict[str, Any]]:
    """Parse a JSON result payload, if the CLI ever returns one."""
    import json

    data = json.loads(text)
    items = data
    if isinstance(data, Mapping):
        for key in ("results", "items", "data", "list"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [data]
    if not isinstance(items, list):
        return []
    parsed: list[dict[str, Any]] = []
    for rank, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        parsed.append(
            {
                "rank": rank,
                "title": normalize_whitespace(item.get("title") or item.get("name")),
                "url": str(item.get("url") or item.get("link") or "").strip(),
                "snippet": normalize_whitespace(
                    item.get("snippet") or item.get("content") or item.get("description") or item.get("text")
                ),
                "meta": {"date_text": str(item.get("date") or item.get("published_at") or "").strip()}
                if (item.get("date") or item.get("published_at"))
                else {},
            }
        )
    return parsed


def _split_meta(text: str) -> tuple[str, dict[str, str]]:
    """Split trailing ``date:``/``rating:`` metadata off a result snippet."""
    match = _META_KEY_RE.search(text)
    if not match:
        return text, {}
    snippet = text[: match.start()].strip()
    blob = text[match.start() :]
    meta: dict[str, str] = {}
    matches = list(_META_KEY_RE.finditer(blob))
    for index, item in enumerate(matches):
        key = item.group("key").lower()
        start = item.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(blob)
        value = blob[start:end].strip().strip(",")
        if value:
            meta["date_text" if key == "date" else key] = value
    return snippet, meta


def _looks_blocked(body: str) -> bool:
    """判断提取结果是否只是登录墙/安全验证页，避免把拦截页当成正文证据。"""
    text = str(body or "").strip()
    if len(text) < MIN_USEFUL_BODY:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in BLOCKED_MARKERS)


def _first_heading(text: str) -> str:
    for line in str(text or "").splitlines():
        match = _HEADING_RE.match(line)
        if match:
            return normalize_whitespace(match.group("title"))
    return ""


@dataclass(frozen=True, slots=True)
class _CliRun:
    ok: bool
    status: str
    stdout: str = ""
    error: Optional[str] = None
    truncated: bool = False
    command_source: str = ""


class AnySearchProvider:
    """Search and page-extraction through the installed AnySearch CLI."""

    name = "anysearch"

    def __init__(
        self,
        *,
        skill_dir: Optional[Path] = None,
        command: Optional[Sequence[str]] = None,
        runner: Optional[Callable[..., Any]] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
        clock: Callable[[], str] = now_iso,
        cache: Any = None,
        offline: bool = False,
    ) -> None:
        self.skill_dir = Path(skill_dir) if skill_dir else None
        self.explicit_command = list(command) if command else None
        self.runner = runner or subprocess.run
        self.env = dict(env) if env is not None else dict(os.environ)
        self.timeout = timeout
        self.max_output = max_output
        self.clock = clock
        self.cache = cache
        self.offline = offline

    # -- discovery ------------------------------------------------------

    def resolved_skill_dir(self) -> Path:
        if self.skill_dir:
            return self.skill_dir
        override = (self.env.get("ANYSEARCH_SKILL_DIR") or "").strip()
        if override:
            return Path(override)
        return DEFAULT_SKILL_DIR

    def resolved_command(self) -> tuple[Optional[list[str]], str]:
        """Resolve the CLI argv prefix and where it came from."""
        if self.explicit_command:
            return list(self.explicit_command), "explicit"
        from_env = (self.env.get("ANYSEARCH_COMMAND") or "").strip()
        if from_env:
            return shlex.split(from_env), "env:ANYSEARCH_COMMAND"
        skill_dir = self.resolved_skill_dir()
        runtime_conf = skill_dir / "runtime.conf"
        if runtime_conf.is_file():
            try:
                for line in runtime_conf.read_text(encoding="utf-8").splitlines():
                    if line.strip().lower().startswith("command:"):
                        value = line.split(":", 1)[1].strip()
                        if value:
                            return shlex.split(value), "runtime.conf"
            except OSError:
                pass
        bundled = skill_dir / "scripts" / "anysearch_cli.py"
        if bundled.is_file():
            return [sys.executable, str(bundled)], "bundled-python"
        return None, "none"

    def _key_configured(self) -> bool:
        """Check key *presence* only; the value is never read into memory."""
        if (self.env.get("ANYSEARCH_API_KEY") or "").strip():
            return True
        env_file = self.resolved_skill_dir() / ".env"
        if not env_file.is_file():
            return False
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                if key.strip() == "ANYSEARCH_API_KEY" and value.strip().strip("\"'"):
                    return True
        except OSError:
            return False
        return False

    def doctor(self) -> ProviderStatus:
        command, source = self.resolved_command()
        if not command:
            return ProviderStatus(
                provider=self.name,
                status="unavailable",
                detail=(
                    "未找到 AnySearch CLI：请安装 anysearch skill，"
                    "或设置 ANYSEARCH_COMMAND / 传入 --anysearch-command"
                ),
                capabilities=(),
                checked_at=self.clock(),
            )
        capabilities = ("search", "extract")
        if self._key_configured():
            return ProviderStatus(
                provider=self.name,
                status="ready",
                detail=(
                    f"AnySearch CLI 可用（来源：{source}）；已检测到 API key 存在"
                    "（只做存在性检查，不记录、不回显、不转发其值）"
                ),
                capabilities=capabilities,
                checked_at=self.clock(),
            )
        return ProviderStatus(
            provider=self.name,
            status="degraded",
            detail=f"AnySearch CLI 可用（来源：{source}），但未配置 API key；匿名额度可用，速率受限",
            capabilities=capabilities,
            checked_at=self.clock(),
        )

    # -- operations -----------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        domain: str = "",
        sub_domain: str = "",
        raw_ref: str = "raw/anysearch-search-1.md",
    ) -> ProviderResult:
        if not str(query or "").strip():
            raise ValueError("query must not be blank")
        if not 1 <= int(max_results) <= 10:
            raise ValueError("max_results must be between 1 and 10")
        argv = ["search", query, "--max_results", str(int(max_results))]
        if domain:
            argv += ["--domain", domain]
        if sub_domain:
            argv += ["--sub_domain", sub_domain]

        run = self._run(argv, raw_ref=raw_ref)
        if not run.ok:
            return ProviderResult(
                provider=self.name,
                status=run.status,
                coverage=Coverage(level="failed", notes="AnySearch 搜索未返回可用结果"),
                error=run.error,
                detail="AnySearch 搜索失败，未产生记录",
                calls=1,
                termination="error",
                gaps=("AnySearch 搜索不可用，未采集通用发现层证据",),
            )

        try:
            items = parse_anysearch_json(run.stdout)
        except ValueError:
            items = []
        if not items:
            items = parse_anysearch_markdown(run.stdout)

        retrieved_at = self.clock()
        records: list[SourceRecord] = []
        for item in items:
            record = self._build_search_record(
                item, query=query, retrieved_at=retrieved_at, raw_ref=raw_ref
            )
            if record is not None:
                records.append(record)

        notes = [DISCOVERY_NOTE]
        if run.truncated:
            notes.append(TRUNCATION_NOTE)
        coverage = Coverage(
            level="partial",
            discovered_pages=1,
            notes="; ".join(notes),
        )
        status = "ready" if records else "degraded"
        detail = f"解析 {len(records)} 条搜索结果"
        error = None
        gaps: tuple[str, ...] = ()
        if not records:
            detail = "CLI 返回内容中未解析出结果"
            error = "AnySearch CLI 输出未包含可解析的结果块"
            gaps = ("AnySearch 输出结构异常，未产生记录",)
        return ProviderResult(
            provider=self.name,
            status=status,
            records=tuple(records),
            coverage=coverage,
            raw_payloads=(
                RawPayload(name=raw_ref, content=run.stdout.encode("utf-8"), media_type="text/markdown"),
            ),
            error=error,
            detail=detail,
            calls=1,
            termination="unknown",
            gaps=gaps,
        )

    def extract(self, url: str, *, raw_ref: str = "raw/anysearch-extract-1.md") -> ProviderResult:
        target = str(url or "").strip()
        if not target.lower().startswith(("http://", "https://")):
            raise ValueError("url must be an absolute http(s) URL")
        run = self._run(["extract", target], raw_ref=raw_ref)
        if not run.ok:
            return ProviderResult(
                provider=self.name,
                status=run.status,
                coverage=Coverage(level="failed", notes="AnySearch 正文提取失败"),
                error=run.error,
                detail="AnySearch 正文提取失败，未产生记录",
                calls=1,
                termination="error",
                gaps=("AnySearch 正文提取不可用",),
            )
        body = run.stdout.strip()
        if not body:
            return ProviderResult(
                provider=self.name,
                status="degraded",
                coverage=Coverage(level="failed", notes="AnySearch 返回空正文"),
                error="AnySearch 正文提取返回空输出",
                detail="未产生正文记录",
                calls=1,
                termination="error",
                gaps=("AnySearch 正文提取返回空内容",),
            )
        heading = _first_heading(body)
        title = heading or target
        blocked = _looks_blocked(body)
        notes = ["AnySearch 正文提取（上游截断上限 50,000 字符）"]
        if run.truncated:
            notes.append(TRUNCATION_NOTE)
        if blocked:
            notes.append(BLOCKED_NOTE)
        record = SourceRecord(
            source_id=stable_source_id("anysearch", url=target),
            platform=self.name,
            record_kind="page",
            source_type="article",
            title=title,
            url=target,
            published_at=None,
            retrieved_at=self.clock(),
            excerpt=make_excerpt(body),
            full_text=body,
            collection_method="anysearch_cli_extract",
            coverage=Coverage(
                level="partial" if blocked else "complete",
                content_pages=1,
                notes="; ".join(notes),
            ),
            raw_ref=raw_ref,
            extra={
                "title_source": "heading" if heading else "url",
                "body_chars": len(body),
                "blocked_suspected": blocked,
            },
        )
        return ProviderResult(
            provider=self.name,
            status="degraded" if blocked else "ready",
            records=(record,),
            coverage=record.coverage,
            raw_payloads=(
                RawPayload(name=raw_ref, content=run.stdout.encode("utf-8"), media_type="text/markdown"),
            ),
            detail=(
                f"提取 1 个网页正文（{len(body)} 字）"
                + ("，疑似拦截页" if blocked else "")
            ),
            calls=1,
            termination="complete",
            gaps=(BLOCKED_NOTE,) if blocked else (),
        )

    # -- internals ------------------------------------------------------

    def _run(self, argv: Sequence[str], *, raw_ref: str) -> _CliRun:
        command, source = self.resolved_command()
        if not command:
            return _CliRun(
                ok=False,
                status="unavailable",
                error="未找到 AnySearch CLI，无法执行",
                command_source="none",
            )
        full_argv = [*command, *argv]
        cache_key = self._cache_key(full_argv)
        if self.cache is not None and cache_key is not None:
            entry = self.cache.get(cache_key)
            if entry is not None:
                return _CliRun(ok=True, status="ready", stdout=entry.body.decode("utf-8", "replace"), command_source=source)
            if self.offline:
                return _CliRun(
                    ok=False,
                    status="unavailable",
                    error="offline cache miss: no stored AnySearch response for this request",
                    command_source=source,
                )
        elif self.offline:
            return _CliRun(
                ok=False,
                status="unavailable",
                error="offline mode requires a cache",
                command_source=source,
            )
        try:
            completed = self.runner(
                full_argv, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as exc:
            return _CliRun(
                ok=False,
                status="unavailable",
                error=redact_secrets(f"AnySearch CLI 不可用：{exc}"),
                command_source=source,
            )
        except subprocess.TimeoutExpired:
            return _CliRun(
                ok=False,
                status="degraded",
                error=f"AnySearch CLI timeout after {self.timeout}s",
                command_source=source,
            )
        except OSError as exc:
            return _CliRun(
                ok=False,
                status="unavailable",
                error=redact_secrets(f"AnySearch CLI 无法执行：{exc}"),
                command_source=source,
            )
        stdout = completed.stdout or ""
        truncated = len(stdout) > self.max_output
        if truncated:
            stdout = stdout[: self.max_output]
        if completed.returncode != 0:
            stderr = redact_secrets((completed.stderr or "").strip())[:500]
            return _CliRun(
                ok=False,
                status="degraded",
                error=f"AnySearch CLI 退出码 {completed.returncode}：{stderr}",
                command_source=source,
            )
        if not stdout.strip():
            return _CliRun(
                ok=False,
                status="degraded",
                error="AnySearch CLI 返回空输出",
                command_source=source,
            )
        if self.cache is not None and cache_key is not None:
            self.cache.put(
                cache_key,
                stdout.encode("utf-8"),
                {
                    "provider": self.name,
                    "argv": [part for part in full_argv],
                    "retrieved_at": now_iso(),
                    "raw_ref": raw_ref,
                },
            )
        return _CliRun(ok=True, status="ready", stdout=stdout, truncated=truncated, command_source=source)

    def _cache_key(self, full_argv: Sequence[str]) -> Optional[str]:
        if self.cache is None:
            return None
        return self.cache.key(self.name, "cli", {"argv": list(full_argv)})

    def _build_search_record(
        self, item: Mapping[str, Any], *, query: str, retrieved_at: str, raw_ref: str
    ) -> Optional[SourceRecord]:
        title = normalize_whitespace(item.get("title"))
        url = str(item.get("url") or "").strip()
        if not title and not url:
            return None
        if not title:
            title = url
        publisher = ""
        if " : " in title:
            publisher = title.rsplit(" : ", 1)[1].strip()
        meta = dict(item.get("meta") or {})
        date_text = str(meta.get("date_text") or "").strip()
        extra: dict[str, Any] = {"query": query, "engine": "anysearch", "search_rank": item.get("rank")}
        if date_text:
            extra["date_text"] = date_text
        for key in ("rating", "rating_count", "duration", "views", "price", "author", "site"):
            if meta.get(key):
                extra[key] = meta[key]
        if not url:
            extra["title_source"] = "title_only"
        return SourceRecord(
            source_id=stable_source_id("anysearch", url=url, title=title, author=query)
            if url
            else stable_source_id("anysearch", title=title, author=query),
            platform=self.name,
            record_kind="page",
            source_type="article",
            title=title,
            url=url,
            publisher=publisher,
            retrieved_at=retrieved_at,
            excerpt=normalize_whitespace(item.get("snippet")),
            published_at=parse_time(date_text) if date_text else None,
            engagement={"search_rank": int(item.get("rank") or 0)},
            collection_method="anysearch_cli_search",
            coverage=Coverage(
                level="partial",
                discovered_pages=1,
                upstream_reports_more=False,
                notes=DISCOVERY_NOTE,
            ),
            raw_ref=raw_ref,
            extra=extra,
        )
