"""Unified, anonymous, read-only source adapter for Chinese community research.

The package intentionally depends only on the Python standard library. Optional
capabilities (Tieba deep dives through ``aiotieba``, AnySearch CLI, Just One API
MCP handoff) degrade into explicit ``unavailable`` or ``degraded`` states rather
than failing silently.
"""

from __future__ import annotations

from .cache import FileCache, safe_join
from .config import Config, load_config
from .export import (
    SchemaError,
    build_comment_tree,
    build_search_agent_input,
    export_search_agent_input,
    validate_search_agent_input,
)
from .models import (
    COVERAGE_LEVELS,
    PROVIDER_STATUSES,
    RECORD_KINDS,
    SOURCE_TYPES,
    CommentRecord,
    Coverage,
    ProviderStatus,
    SourceRecord,
    dedupe_records,
    merge_coverage,
    merge_records,
    platform_source_type,
    stable_source_id,
    worst_provider_status,
)
from .pipeline import ResearchPipeline
from .providers.base import ProviderResult, RawPayload
from .normalize import (
    make_excerpt,
    normalize_object_id,
    normalize_url,
    now_iso,
    parse_time,
    redact_secrets,
    slugify,
    strip_html,
)

__all__ = [
    "COVERAGE_LEVELS",
    "Config",
    "FileCache",
    "ProviderResult",
    "RawPayload",
    "ResearchPipeline",
    "SchemaError",
    "build_comment_tree",
    "build_search_agent_input",
    "export_search_agent_input",
    "load_config",
    "safe_join",
    "validate_search_agent_input",
    "PROVIDER_STATUSES",
    "RECORD_KINDS",
    "SOURCE_TYPES",
    "CommentRecord",
    "Coverage",
    "ProviderStatus",
    "SourceRecord",
    "dedupe_records",
    "make_excerpt",
    "merge_coverage",
    "merge_records",
    "normalize_object_id",
    "normalize_url",
    "now_iso",
    "parse_time",
    "platform_source_type",
    "redact_secrets",
    "slugify",
    "stable_source_id",
    "strip_html",
    "worst_provider_status",
]

__version__ = "0.1.0"
