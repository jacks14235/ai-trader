import json
from datetime import UTC, datetime
from decimal import Decimal

from trader.research.collection import ResearchCollection
from trader.research.models import ResearchBatch, ResearchDocument, ResearchRequest
from trader.research.selection import select_deep_symbols
from trader.universe.models import UniverseAsset

AS_OF = datetime(2026, 9, 24, 19, 15, tzinfo=UTC)


def _asset(symbol: str, name: str | None = None) -> UniverseAsset:
    return UniverseAsset(
        symbol=symbol,
        name=name or f"{symbol} Corp.",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )


def _market_batch(symbol: str, *, price: float, volume: float) -> ResearchBatch:
    request = ResearchRequest.create(
        symbol=symbol,
        question_type="MARKET_CONTEXT",
        query=f"Market context for {symbol}",
        window_start=datetime(2026, 9, 1, tzinfo=UTC),
        window_end=AS_OF,
        priority=80,
    )
    snapshot = json.dumps(
        {symbol: {"latestTrade": {"p": price, "t": AS_OF.isoformat()}}},
        separators=(",", ":"),
    ).encode()
    bars = json.dumps(
        {symbol: [{"c": price, "v": volume, "t": "2026-09-23T04:00:00Z"}]},
        separators=(",", ":"),
    ).encode()
    documents = (
        ResearchDocument.create(
            question_ids=(request.question_id,),
            provider="alpaca",
            source_type="MARKET_DATA",
            source_tier="BROKER",
            source_name="Alpaca stock snapshot",
            provider_item_id=f"snapshot:{symbol}",
            url="https://data.alpaca.markets/v2/stocks/snapshots",
            author=None,
            published_at=AS_OF,
            retrieved_at=AS_OF,
            symbols=(symbol,),
            headline=f"{symbol} snapshot",
            normalized_text=snapshot.decode(),
            summary=None,
            raw_payload=snapshot,
        ),
        ResearchDocument.create(
            question_ids=(request.question_id,),
            provider="alpaca",
            source_type="MARKET_DATA",
            source_tier="BROKER",
            source_name="Alpaca adjusted daily bars",
            provider_item_id=f"bars:{symbol}",
            url="https://data.alpaca.markets/v2/stocks/bars",
            author=None,
            published_at=AS_OF,
            retrieved_at=AS_OF,
            symbols=(symbol,),
            headline=f"{symbol} bars",
            normalized_text=bars.decode(),
            summary=None,
            raw_payload=bars,
        ),
    )
    return ResearchBatch(
        provider="alpaca",
        retrieved_at=AS_OF,
        question_ids=(request.question_id,),
        documents=documents,
        request_count=2,
        response_bytes=sum(len(item.raw_payload) for item in documents),
    )


def test_deep_selection_uses_price_liquidity_and_fund_policy_but_keeps_pinned() -> None:
    assets = {
        "HOLD": _asset("HOLD"),
        "GOOD": _asset("GOOD"),
        "LOW": _asset("LOW"),
        "THIN": _asset("THIN"),
        "SOXL": _asset("SOXL", "Direxion Daily Semiconductor Bull 3X Shares"),
    }
    batches = (
        _market_batch("HOLD", price=2, volume=1_000),
        _market_batch("GOOD", price=50, volume=200_000),
        _market_batch("LOW", price=3, volume=10_000_000),
        _market_batch("THIN", price=20, volume=10_000),
        _market_batch("SOXL", price=70, volume=1_000_000),
    )
    collection = ResearchCollection(
        batches=batches,
        request_count=sum(batch.request_count for batch in batches),
        response_bytes=sum(batch.response_bytes for batch in batches),
    )

    result = select_deep_symbols(
        ordered_symbols=("HOLD", "LOW", "SOXL", "THIN", "GOOD"),
        assets=assets,
        fast_collection=collection,
        pinned_symbols=frozenset({"HOLD"}),
        max_symbols=3,
        min_price=Decimal("5"),
        min_average_daily_dollar_volume=Decimal("5000000"),
    )

    assert result.selected_symbols == ("HOLD", "GOOD")
    by_symbol = {item.symbol: item for item in result.assessments}
    assert "PINNED_FOR_PORTFOLIO_OR_EVENT_RESEARCH" in by_symbol["HOLD"].reason_codes
    assert by_symbol["GOOD"].reason_codes == ("POLICY_SCREEN_PASSED",)
    assert "BELOW_MIN_PRICE" in by_symbol["LOW"].reason_codes
    assert "BELOW_MIN_DOLLAR_VOLUME" in by_symbol["THIN"].reason_codes
    assert "PROHIBITED_LEVERAGED_OR_INVERSE_FUND" in by_symbol["SOXL"].reason_codes
