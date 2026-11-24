"""Generic, parameterized Steam review collector."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote

from ..http import HttpClient
from ..models import Coverage, ProviderStatus, SourceRecord
from ..normalize import make_excerpt, normalize_whitespace, now_iso
from .base import ProviderResult, RawPayload, json_bytes


def _timestamp(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def map_steam_reviews(payload: Mapping[str, Any], *, appid: str, app_name: str = "", retrieved_at: str | None = None, raw_ref: str = "") -> ProviderResult:
    """Map one official Steam appreviews response into traceable records."""
    retrieved = retrieved_at or now_iso()
    rows = payload.get("reviews") if isinstance(payload, Mapping) else []
    rows = rows if isinstance(rows, list) else []
    cursor = str(payload.get("cursor") or "")
    coverage = Coverage(level="partial" if cursor else "complete", discovered_pages=1, content_pages=1, upstream_reports_more=bool(cursor), notes="Steam 官方 appreviews 接口分页结果")
    records: list[SourceRecord] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            continue
        recommendation_id = str(item.get("recommendationid") or f"row-{index + 1}")
        author = item.get("author") if isinstance(item.get("author"), Mapping) else {}
        steam_id = str(author.get("steamid") or "")
        review_text = normalize_whitespace(str(item.get("review") or ""))
        if not review_text:
            continue
        url = f"https://steamcommunity.com/profiles/{quote(steam_id)}/recommended/{quote(str(appid))}/" if steam_id else f"https://steamcommunity.com/app/{quote(str(appid))}/reviews/"
        records.append(SourceRecord(
            source_id=f"steam:review:{appid}:{recommendation_id}", platform="steam", record_kind="post", source_type="store",
            title=app_name or f"Steam app {appid} 用户评测", url=url, author_display_name=steam_id,
            published_at=_timestamp(item.get("timestamp_created")), retrieved_at=retrieved,
            excerpt=make_excerpt(review_text), full_text=review_text, publisher="Steam", thread_id=f"steam:app:{appid}",
            engagement={"voted_up": bool(item.get("voted_up")), "votes_up": int(item.get("votes_up") or 0), "votes_funny": int(item.get("votes_funny") or 0), "weighted_vote_score": str(item.get("weighted_vote_score") or "")},
            collection_method="steam_appreviews_api", coverage=coverage, raw_ref=raw_ref,
            extra={"appid": str(appid), "recommendationid": recommendation_id, "language": str(item.get("language") or ""), "steam_purchase": bool(item.get("steam_purchase")), "received_for_free": bool(item.get("received_for_free")), "playtime_forever": int(author.get("playtime_forever") or 0)},
        ))
    return ProviderResult(provider="steam", status="ready" if records else "degraded", records=tuple(records), coverage=coverage, raw_payloads=(RawPayload(name=raw_ref or f"raw/steam-reviews-{appid}.json", content=json_bytes(payload)),), detail=f"映射 {len(records)} 条 Steam 评测", calls=1, termination="page_complete" if records else "no_records", gaps=("上游返回游标，需继续分页才能完整覆盖",) if cursor else ())


class SteamReviewProvider:
    name = "steam"

    def __init__(self, *, http: HttpClient | None = None, clock: Callable[[], str] = now_iso) -> None:
        self.http = http or HttpClient(provider="steam", spacing=2.0)
        self.clock = clock

    def doctor(self) -> ProviderStatus:
        return ProviderStatus(provider=self.name, status="ready", detail="官方 Steam appreviews 公共只读接口已内置", capabilities=("reviews", "pagination", "language_filter"), checked_at=self.clock())

    def collect(self, appid: str, *, app_name: str = "", language: str = "schinese", pages: int = 1, page_size: int = 100, filter: str = "recent") -> ProviderResult:
        appid = str(appid).strip()
        if not appid.isdigit(): raise ValueError("appid must contain digits only")
        if not 1 <= pages <= 20: raise ValueError("pages must be between 1 and 20")
        if not 1 <= page_size <= 100: raise ValueError("page_size must be between 1 and 100")
        if filter not in {"recent", "updated", "all"}: raise ValueError("filter must be recent, updated or all")
        records: dict[str, SourceRecord] = {}
        payloads: list[RawPayload] = []
        gaps: list[str] = []
        cursor, calls, from_cache, termination = "*", 0, 0, "page_limit"
        for page in range(1, pages + 1):
            response = self.http.get_json(f"https://store.steampowered.com/appreviews/{appid}", params={"json": 1, "filter": filter, "language": language, "review_type": "all", "purchase_type": "all", "num_per_page": page_size, "cursor": cursor})
            calls += 1
            from_cache += int(response.from_cache)
            if not response.ok or not isinstance(response.data, Mapping):
                return ProviderResult(provider=self.name, status="degraded" if records else "unavailable", records=tuple(records.values()), coverage=Coverage(level="partial" if records else "failed"), raw_payloads=tuple(payloads), error=response.error or f"Steam HTTP {response.status}", detail="Steam 评测采集未完整完成", calls=calls, from_cache=from_cache, termination="error", gaps=tuple(gaps))
            mapped = map_steam_reviews(response.data, appid=appid, app_name=app_name, retrieved_at=self.clock(), raw_ref=f"raw/steam-reviews-{appid}-p{page}.json")
            payloads.extend(mapped.raw_payloads)
            before = len(records)
            records.update({record.source_id: record for record in mapped.records})
            next_cursor = str(response.data.get("cursor") or "")
            if not next_cursor:
                termination = "complete"
                break
            if next_cursor == cursor or len(records) == before:
                termination = "repeated_page"
                gaps.append("分页游标或记录重复，已停止")
                break
            cursor = next_cursor
        if termination == "page_limit" and cursor:
            gaps.append(f"达到 pages={pages} 上限，上游仍有更多结果")
        coverage = Coverage(level="partial" if gaps else "complete", discovered_pages=len(payloads), content_pages=len(payloads), upstream_reports_more=bool(gaps), notes="Steam 官方 appreviews 分页采集")
        return ProviderResult(provider=self.name, status="ready" if records else "degraded", records=tuple(records.values()), coverage=coverage, raw_payloads=tuple(payloads), detail=f"采集 {len(records)} 条 Steam 评测", calls=calls, from_cache=from_cache, termination=termination, gaps=tuple(gaps))
