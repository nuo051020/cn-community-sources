"""Non-sensitive configuration loading.

``config.toml`` (see ``config.example.toml``) may only carry non-sensitive
parameters. Secrets always come from the environment or the Codex secure store
and are never accepted here.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config.toml"
EXAMPLE_CONFIG_PATH = PACKAGE_ROOT / "config.example.toml"

# Secret-looking keys are refused outright so a token can never be persisted
# in the configuration file by accident.
FORBIDDEN_KEYS = (
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "bearer",
    "cookie",
    "session",
    "credential",
)

ALLOWED_SECTIONS = ("run", "providers")

RUN_KEYS = {
    "output_root": str,
    "cache_dir": str,
    "concurrency": int,
    "request_spacing_seconds": float,
    "max_retries": int,
    "max_response_bytes": int,
    "anysearch_timeout_seconds": float,
    "anysearch_max_output": int,
    "anysearch_skill_dir": str,
    "tieba_search_url": str,
    "tieba_page_size": int,
    "tieba_page_limit": int,
    "aiotieba_page_limit": int,
    "aiotieba_include_nested": bool,
    "justone_server": str,
    "justone_mcp_url": str,
    "justone_token_env": str,
    "justone_max_calls": int,
    "justone_discover_limit": int,
}


@dataclass(frozen=True, slots=True)
class Config:
    output_root: str = "outputs"
    cache_dir: str = ".cache"
    concurrency: int = 1
    request_spacing_seconds: float = 2.0
    max_retries: int = 3
    max_response_bytes: int = 2_000_000
    anysearch_timeout_seconds: float = 60.0
    anysearch_max_output: int = 500_000
    anysearch_skill_dir: str = ""
    tieba_search_url: str = "https://tieba.baidu.com/mo/q/search/thread"
    tieba_page_size: int = 20
    tieba_page_limit: int = 1
    aiotieba_page_limit: int = 1
    aiotieba_include_nested: bool = False
    justone_server: str = "justoneapi"
    justone_mcp_url: str = "https://mcp.justoneapi.com/mcp"
    justone_token_env: str = "JUSTONEAPI_TOKEN"
    justone_max_calls: int = 20
    justone_discover_limit: int = 8
    source_path: str = ""

    def __post_init__(self) -> None:
        if self.concurrency not in (1, 2):
            raise ValueError("run.concurrency must be 1 or 2（并发上限为 2）")
        if self.request_spacing_seconds != 0 and not 1.5 <= self.request_spacing_seconds <= 3.0:
            raise ValueError("run.request_spacing_seconds must be between 1.5 and 3.0")
        if not 0 <= self.max_retries <= 5:
            raise ValueError("run.max_retries must be between 0 and 5")
        if self.max_response_bytes < 1024:
            raise ValueError("run.max_response_bytes must be at least 1024")
        if self.anysearch_timeout_seconds <= 0:
            raise ValueError("run.anysearch_timeout_seconds must be positive")
        if self.anysearch_max_output < 1024:
            raise ValueError("run.anysearch_max_output must be at least 1024")
        if not 1 <= self.tieba_page_size <= 50:
            raise ValueError("run.tieba_page_size must be between 1 and 50")
        if self.tieba_page_limit < 1:
            raise ValueError("run.tieba_page_limit must be >= 1")
        if not 1 <= self.justone_max_calls <= 200:
            raise ValueError("run.justone_max_calls must be between 1 and 200")
        if not 1 <= self.justone_discover_limit <= 20:
            raise ValueError("run.justone_discover_limit must be between 1 and 20")

    def resolve(self, value: str) -> Path:
        """Resolve a configured path relative to the package root."""
        path = Path(value)
        return path if path.is_absolute() else PACKAGE_ROOT / path


def load_config(path: Optional[Path] = None) -> Config:
    """Load ``config.toml``; a missing file yields the defaults."""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    if not target.is_file():
        return Config()
    with open(target, "rb") as handle:
        raw = tomllib.load(handle)
    return config_from_mapping(raw, source_path=str(target))


def config_from_mapping(raw: Mapping[str, Any], *, source_path: str = "") -> Config:
    unknown_sections = set(raw) - set(ALLOWED_SECTIONS)
    if unknown_sections:
        raise ValueError(f"unknown config sections: {sorted(unknown_sections)}")
    flat: dict[str, Any] = {}
    for section, values in raw.items():
        if not isinstance(values, Mapping):
            raise ValueError(f"config section {section!r} must be a table")
        for key, value in values.items():
            _reject_secret(section, key)
            flat[key] = value
    unknown_keys = set(flat) - set(RUN_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown config keys: {sorted(unknown_keys)}")
    converted: dict[str, Any] = {}
    for key, value in flat.items():
        expected = RUN_KEYS[key]
        if expected is bool:
            if not isinstance(value, bool):
                raise ValueError(f"config key {key!r} must be a boolean")
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"config key {key!r} must be an integer")
        elif expected is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"config key {key!r} must be a number")
            value = float(value)
        elif not isinstance(value, str):
            raise ValueError(f"config key {key!r} must be a string")
        converted[key] = value
    config = Config(**converted, source_path=source_path)
    return config


def _reject_secret(section: str, key: str) -> None:
    lowered = str(key).lower()
    for forbidden in FORBIDDEN_KEYS:
        if forbidden in lowered:
            raise ValueError(
                f"config key {section}.{key} looks like a secret; "
                "secrets must come from the environment or Codex secure configuration"
            )


def with_overrides(config: Config, **overrides: Any) -> Config:
    """Return a copy with non-empty overrides applied (CLI flags win)."""
    applied = {key: value for key, value in overrides.items() if value not in (None, "", ())}
    return replace(config, **applied) if applied else config
