"""Read-only Alpaca market-data and news research provider."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from pydantic import JsonValue

from trader.research.artifacts import canonical_json_bytes
from trader.research.models import (
    ProviderName,
    QuestionType,
    ResearchBatch,
    ResearchDocument,
    ResearchRequest,
)
from trader.research.text import alpaca_news_text

ALPACA_STOCK_DATA_URL = "https://data.alpaca.markets/v2/stocks"
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"


class AlpacaResearchError(RuntimeError):
    """Raised when Alpaca returns an invalid or unusable research payload."""


class _StockClient(Protocol):
    def get_stock_snapshot(self, request_params: StockSnapshotRequest) -> object: ...

    def get_stock_bars(self, request_params: StockBarsRequest) -> object: ...


class _NewsClient(Protocol):
    def get_news(self, request_params: NewsRequest) -> object: ...


class AlpacaResearchProvider:
    """Collect bounded market context and broker news without trading access."""

    provider_name: ProviderName = "alpaca"
    supported_question_types: frozenset[QuestionType] = frozenset(
        {"MARKET_CONTEXT", "COMPANY_NEWS", "VALUATION_FACTS"}
    )

    def __init__(
        self,
        key: str | None = None,
        secret: str | None = None,
        *,
        stock_client: _StockClient | None = None,
        news_client: _NewsClient | None = None,
        feed: DataFeed = DataFeed.IEX,
        max_bars: int = 100,
        max_news_items: int = 20,
        max_monthly_bars: int = 60,
    ) -> None:
        if (stock_client is None or news_client is None) and (not key or not secret):
            raise ValueError("Alpaca credentials are required when clients are not injected")
        if not 1 <= max_bars <= 1_000:
            raise ValueError("max_bars must be between 1 and 1,000")
        if not 1 <= max_news_items <= 50:
            raise ValueError("max_news_items must be between 1 and 50")
        if not 12 <= max_monthly_bars <= 60:
            raise ValueError("max_monthly_bars must be between 12 and 60")
        self.stock_client = stock_client or StockHistoricalDataClient(
            key,
            secret,
            raw_data=True,
        )
        self.news_client = news_client or NewsClient(key, secret, raw_data=True)
        self.feed = feed
        self.max_bars = max_bars
        self.max_news_items = max_news_items
        self.max_monthly_bars = max_monthly_bars

    def estimated_request_count(self, request: ResearchRequest) -> int:
        if request.question_type == "MARKET_CONTEXT":
            return 2
        if request.question_type == "COMPANY_NEWS":
            return 1
        if request.question_type == "VALUATION_FACTS":
            return 1
        raise AlpacaResearchError(f"Alpaca research does not support {request.question_type}")

    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch:
        if request.question_type == "MARKET_CONTEXT":
            return self._market_context(request, retrieved_at)
        if request.question_type == "COMPANY_NEWS":
            return self._company_news(request, retrieved_at)
        if request.question_type == "VALUATION_FACTS":
            return self._monthly_prices(request, retrieved_at)
        raise AlpacaResearchError(f"Alpaca research does not support {request.question_type}")

    def _monthly_prices(
        self, request: ResearchRequest, retrieved_at: datetime | None
    ) -> ResearchBatch:
        try:
            response = self.stock_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=request.symbol,
                    start=request.window_start,
                    end=request.window_end,
                    limit=self.max_monthly_bars,
                    timeframe=TimeFrame.Month,
                    adjustment=Adjustment.ALL,
                    feed=self.feed,
                    sort=Sort.ASC,
                )
            )
        except Exception as exc:
            raise AlpacaResearchError(
                f"Alpaca monthly-price collection failed for {request.symbol}: {exc}"
            ) from exc
        retrieved_at = _retrieval_time(retrieved_at)
        payload = _json_mapping(response, "monthly stock bars")
        bars = payload.get(request.symbol, ())
        if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
            raise AlpacaResearchError("Alpaca returned invalid monthly bars")
        if len(bars) > self.max_monthly_bars:
            raise AlpacaResearchError("Alpaca returned more monthly bars than requested")
        raw = canonical_json_bytes(payload)
        document = ResearchDocument.create(
            question_ids=(request.question_id,),
            provider="alpaca",
            source_type="MARKET_DATA",
            source_tier="BROKER",
            source_name="Alpaca adjusted monthly bars",
            provider_item_id=_provider_item_id("monthly-bars", request.symbol, raw),
            url=f"{ALPACA_STOCK_DATA_URL}/bars",
            author=None,
            published_at=retrieved_at,
            retrieved_at=retrieved_at,
            symbols=(request.symbol,),
            headline=f"{request.symbol} Alpaca adjusted monthly bars",
            normalized_text=_normalized_json(payload),
            summary=None,
            raw_payload=raw,
            metadata={
                "endpoint": "stocks/bars",
                "timeframe": "1Month",
                "adjustment": "all",
                "bar_count": len(bars),
                "data_timestamps": list(_data_timestamps((payload,))),
            },
        )
        return ResearchBatch(
            provider="alpaca",
            retrieved_at=retrieved_at,
            question_ids=(request.question_id,),
            documents=(document,),
            request_count=1,
            response_bytes=len(raw),
        )

    def _market_context(
        self,
        request: ResearchRequest,
        retrieved_at: datetime | None,
    ) -> ResearchBatch:
        try:
            snapshot_response = self.stock_client.get_stock_snapshot(
                StockSnapshotRequest(
                    symbol_or_symbols=request.symbol,
                    feed=self.feed,
                )
            )
            bars_response = self.stock_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=request.symbol,
                    start=request.window_start,
                    end=request.window_end,
                    limit=self.max_bars,
                    timeframe=TimeFrame.Day,
                    adjustment=Adjustment.ALL,
                    feed=self.feed,
                    sort=Sort.ASC,
                )
            )
        except Exception as exc:
            raise AlpacaResearchError(
                f"Alpaca market-context collection failed for {request.symbol}: {exc}"
            ) from exc

        retrieved_at = _retrieval_time(retrieved_at)

        snapshot = _json_mapping(snapshot_response, "stock snapshot")
        symbol_snapshot = snapshot.get(request.symbol)
        if symbol_snapshot is not None and not isinstance(symbol_snapshot, Mapping):
            raise AlpacaResearchError("Alpaca returned an invalid symbol snapshot payload")
        bars = _json_mapping(bars_response, "stock bars")
        symbol_bars = bars.get(request.symbol)
        if symbol_bars is None:
            symbol_bars = ()
        if not isinstance(symbol_bars, Sequence) or isinstance(symbol_bars, (str, bytes)):
            raise AlpacaResearchError("Alpaca returned an invalid bars payload")
        if len(symbol_bars) > self.max_bars:
            raise AlpacaResearchError("Alpaca returned more bars than requested")

        snapshot_bytes = canonical_json_bytes(snapshot)
        bars_bytes = canonical_json_bytes(bars)
        timestamps = _data_timestamps((snapshot, bars))
        common_metadata: dict[str, JsonValue] = {
            "feed": self.feed.value,
            "query": request.query,
            "window_start": request.window_start.isoformat(),
            "window_end": request.window_end.isoformat(),
            "data_timestamps": list(timestamps),
            "symbol_snapshot_present": symbol_snapshot is not None,
        }
        documents = (
            ResearchDocument.create(
                question_ids=(request.question_id,),
                provider="alpaca",
                source_type="MARKET_DATA",
                source_tier="BROKER",
                source_name="Alpaca stock snapshot",
                provider_item_id=_provider_item_id("snapshot", request.symbol, snapshot_bytes),
                url=f"{ALPACA_STOCK_DATA_URL}/snapshots",
                author=None,
                published_at=retrieved_at,
                retrieved_at=retrieved_at,
                symbols=(request.symbol,),
                headline=f"{request.symbol} Alpaca stock snapshot",
                normalized_text=_normalized_json(snapshot),
                summary=None,
                raw_payload=snapshot_bytes,
                metadata={**common_metadata, "endpoint": "stocks/snapshots"},
                cost_usd=Decimal("0"),
            ),
            ResearchDocument.create(
                question_ids=(request.question_id,),
                provider="alpaca",
                source_type="MARKET_DATA",
                source_tier="BROKER",
                source_name="Alpaca adjusted daily bars",
                provider_item_id=_provider_item_id("daily-bars", request.symbol, bars_bytes),
                url=f"{ALPACA_STOCK_DATA_URL}/bars",
                author=None,
                published_at=retrieved_at,
                retrieved_at=retrieved_at,
                symbols=(request.symbol,),
                headline=f"{request.symbol} Alpaca adjusted daily bars",
                normalized_text=_normalized_json(bars),
                summary=None,
                raw_payload=bars_bytes,
                metadata={
                    **common_metadata,
                    "endpoint": "stocks/bars",
                    "timeframe": "1Day",
                    "adjustment": "all",
                    "bar_count": len(symbol_bars),
                },
                cost_usd=Decimal("0"),
            ),
        )
        return ResearchBatch(
            provider="alpaca",
            retrieved_at=retrieved_at,
            question_ids=(request.question_id,),
            documents=documents,
            request_count=2,
            response_bytes=len(snapshot_bytes) + len(bars_bytes),
            cost_usd=Decimal("0"),
        )

    def _company_news(
        self,
        request: ResearchRequest,
        retrieved_at: datetime | None,
    ) -> ResearchBatch:
        try:
            response = self.news_client.get_news(
                NewsRequest(
                    start=request.window_start,
                    end=request.window_end,
                    sort="desc",
                    symbols=request.symbol,
                    limit=self.max_news_items,
                    include_content=True,
                    exclude_contentless=False,
                )
            )
        except Exception as exc:
            raise AlpacaResearchError(
                f"Alpaca news collection failed for {request.symbol}: {exc}"
            ) from exc
        retrieved_at = _retrieval_time(retrieved_at)
        payload = _json_mapping(response, "news")
        raw_news = payload.get("news")
        if not isinstance(raw_news, Sequence) or isinstance(raw_news, (str, bytes)):
            raise AlpacaResearchError("Alpaca returned an invalid news list")
        if len(raw_news) > self.max_news_items:
            raise AlpacaResearchError("Alpaca returned more news items than requested")
        for item in raw_news:
            if not isinstance(item, Mapping):
                raise AlpacaResearchError("Alpaca returned an invalid news item")

        raw_payload = canonical_json_bytes(payload)
        published_at = _latest_news_timestamp(raw_news, retrieved_at)
        headline = (
            f"{request.symbol} Alpaca news ({len(raw_news)} items)"
            if raw_news
            else f"{request.symbol} Alpaca news (no results)"
        )
        document = ResearchDocument.create(
            question_ids=(request.question_id,),
            provider="alpaca",
            source_type="NEWS",
            source_tier="BROKER",
            source_name="Alpaca News",
            provider_item_id=_provider_item_id("news", request.symbol, raw_payload),
            url=ALPACA_NEWS_URL,
            author=None,
            published_at=published_at,
            retrieved_at=retrieved_at,
            symbols=(request.symbol,),
            headline=headline,
            normalized_text=alpaca_news_text(raw_news) or "No matching news items.",
            summary=None,
            raw_payload=raw_payload,
            metadata={
                "endpoint": "news",
                "query": request.query,
                "window_start": request.window_start.isoformat(),
                "window_end": request.window_end.isoformat(),
                "item_count": len(raw_news),
                "data_timestamps": list(_data_timestamps((payload,))),
            },
            cost_usd=Decimal("0"),
        )
        return ResearchBatch(
            provider="alpaca",
            retrieved_at=retrieved_at,
            question_ids=(request.question_id,),
            documents=(document,),
            request_count=1,
            response_bytes=len(raw_payload),
            cost_usd=Decimal("0"),
        )


def _retrieval_time(value: datetime | None) -> datetime:
    retrieved = value or datetime.now(UTC)
    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return retrieved.astimezone(UTC)


def _json_mapping(value: object, description: str) -> dict[str, object]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise AlpacaResearchError(f"Alpaca returned an invalid {description} response")
    return {str(key): item for key, item in value.items()}


def _provider_item_id(kind: str, symbol: str, payload: bytes) -> str:
    import hashlib

    return f"{kind}:{symbol}:{hashlib.sha256(payload).hexdigest()}"


def _normalized_json(value: object, *, max_chars: int = 100_000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[normalized text truncated; exact payload retained]"


def _latest_news_timestamp(items: Sequence[object], retrieved_at: datetime) -> datetime:
    timestamps = _data_timestamps((items,))
    if not timestamps:
        return retrieved_at
    parsed = max(_parse_timestamp(value) for value in timestamps)
    if parsed > retrieved_at:
        raise AlpacaResearchError("Alpaca news contains a timestamp after retrieval")
    return parsed


def _data_timestamps(values: Sequence[object]) -> tuple[str, ...]:
    found: set[str] = set()

    def visit(value: object, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key).lower())
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                visit(child, key)
            return
        if key in {"t", "timestamp", "created_at", "updated_at"} and isinstance(value, str):
            try:
                found.add(_parse_timestamp(value).isoformat())
            except ValueError:
                return

    for candidate in values:
        visit(candidate)
    return tuple(sorted(found))


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("data timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
