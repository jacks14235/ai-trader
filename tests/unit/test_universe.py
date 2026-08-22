from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from trader.risk.config import UniverseConfig
from trader.universe.models import (
    ActiveStockSignal,
    MarketMoversBatch,
    MostActiveBatch,
    MoverSignal,
    UniverseAsset,
)
from trader.universe.provider import AlpacaUniverseProvider
from trader.universe.scanner import UniverseScanner

AS_OF = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)


def config(**candidate_updates: int) -> UniverseConfig:
    candidate_selection = {
        "max_candidates": 5,
        "most_active_by_volume": 5,
        "most_active_by_trades": 5,
        "market_movers_per_side": 5,
        "exploration_candidates": 1,
    }
    candidate_selection.update(candidate_updates)
    return UniverseConfig.model_validate(
        {
            "source": "alpaca",
            "asset_class": "us_equity",
            "require_active": True,
            "require_tradable": True,
            "benchmark_symbols": ["SPY", "QQQ"],
            "event_symbols": ["SPY", "QQQ"],
            "excluded_symbols": [],
            "candidate_selection": candidate_selection,
        }
    )


def asset(
    symbol: str,
    *,
    asset_class: str = "us_equity",
    status: str = "active",
    tradable: bool = True,
) -> UniverseAsset:
    return UniverseAsset(
        symbol=symbol,
        name=f"{symbol} Inc.",
        asset_class=asset_class,
        status=status,
        tradable=tradable,
        exchange="NASDAQ",
        fractionable=True,
    )


class FakeProvider:
    def __init__(
        self,
        assets: tuple[UniverseAsset, ...],
        *,
        volume: tuple[ActiveStockSignal, ...] = (),
        trades: tuple[ActiveStockSignal, ...] = (),
        gainers: tuple[MoverSignal, ...] = (),
        losers: tuple[MoverSignal, ...] = (),
    ) -> None:
        self.assets = assets
        self.volume = volume
        self.trades = trades
        self.gainers = gainers
        self.losers = losers

    def list_active_us_equities(self) -> tuple[UniverseAsset, ...]:
        return self.assets

    def get_most_actives(self, *, by: str, top: int) -> MostActiveBatch:
        assert top == 5
        stocks = self.volume if by == "volume" else self.trades
        return MostActiveBatch(by=by, last_updated=AS_OF, stocks=stocks)

    def get_market_movers(self, *, top: int) -> MarketMoversBatch:
        assert top == 5
        return MarketMoversBatch(
            last_updated=AS_OF,
            gainers=self.gainers,
            losers=self.losers,
        )


def active(symbol: str, volume: float, trades: float) -> ActiveStockSignal:
    return ActiveStockSignal(symbol=symbol, volume=volume, trade_count=trades)


def mover(symbol: str, percent: float) -> MoverSignal:
    return MoverSignal(symbol=symbol, percent_change=percent, change=1, price=10)


def test_scanner_pins_holdings_deduplicates_signals_and_caps_slate() -> None:
    provider = FakeProvider(
        (
            asset("TSLA"),
            asset("SPY"),
            asset("BAD", tradable=False),
            asset("QQQ"),
            asset("NVDA"),
            asset("AAPL"),
            asset("CRYPTO", asset_class="crypto"),
            asset("OLD", status="inactive"),
        ),
        volume=(active("AAPL", 1000, 100), active("BAD", 900, 90)),
        trades=(active("AAPL", 1000, 100), active("TSLA", 800, 80)),
        gainers=(mover("NVDA", 12), mover("OUTSIDE", 9)),
        losers=(mover("TSLA", -8),),
    )

    scan = UniverseScanner(provider, config()).scan(
        as_of=AS_OF,
        portfolio_symbols=("AAPL",),
    )

    assert [asset.symbol for asset in scan.eligible_assets] == [
        "AAPL",
        "NVDA",
        "QQQ",
        "SPY",
        "TSLA",
    ]
    assert scan.candidate_count == 5
    assert {candidate.symbol for candidate in scan.candidates} == {
        "AAPL",
        "NVDA",
        "QQQ",
        "SPY",
        "TSLA",
    }
    aapl = next(candidate for candidate in scan.candidates if candidate.symbol == "AAPL")
    assert [signal.source for signal in aapl.signals] == [
        "PORTFOLIO",
        "MOST_ACTIVE_VOLUME",
        "MOST_ACTIVE_TRADES",
    ]
    assert scan.skipped_screener_symbols == 2
    assert len(scan.asset_content_hash) == 64


def test_exploration_is_stable_for_same_eastern_trading_date() -> None:
    provider = FakeProvider(tuple(asset(symbol) for symbol in ("SPY", "QQQ", "AAA", "BBB")))
    scanner = UniverseScanner(
        provider,
        config(max_candidates=4, exploration_candidates=2),
    )

    morning = scanner.scan(as_of=AS_OF, portfolio_symbols=())
    afternoon = scanner.scan(
        as_of=datetime(2026, 8, 21, 21, 30, tzinfo=UTC),
        portfolio_symbols=(),
    )

    morning_exploration = {
        candidate.symbol
        for candidate in morning.candidates
        if any(signal.source == "EXPLORATION" for signal in candidate.signals)
    }
    afternoon_exploration = {
        candidate.symbol
        for candidate in afternoon.candidates
        if any(signal.source == "EXPLORATION" for signal in candidate.signals)
    }
    assert morning_exploration == afternoon_exploration
    assert len(morning_exploration) == 2


def test_exploration_slots_are_not_crowded_out_by_ranked_signals() -> None:
    provider = FakeProvider(
        tuple(asset(symbol) for symbol in ("SPY", "QQQ", "AAA", "BBB", "CCC", "DDD")),
        volume=(
            active("AAA", 1000, 100),
            active("BBB", 900, 90),
            active("CCC", 800, 80),
        ),
    )

    scan = UniverseScanner(
        provider,
        config(max_candidates=4, exploration_candidates=1),
    ).scan(as_of=AS_OF, portfolio_symbols=())

    assert len(scan.candidates) == 4
    assert any(
        signal.source == "EXPLORATION"
        for candidate in scan.candidates
        for signal in candidate.signals
    )
    assert {"SPY", "QQQ"}.issubset(candidate.symbol for candidate in scan.candidates)


def test_scanner_fails_closed_if_held_symbol_is_not_eligible() -> None:
    provider = FakeProvider((asset("SPY"), asset("QQQ"), asset("OLD", status="inactive")))

    with pytest.raises(RuntimeError, match="OLD"):
        UniverseScanner(provider, config()).scan(
            as_of=AS_OF,
            portfolio_symbols=("OLD",),
        )


def test_scanner_rejects_duplicate_provider_assets() -> None:
    provider = FakeProvider((asset("SPY"), asset("QQQ"), asset("SPY")))

    with pytest.raises(RuntimeError, match="duplicate asset SPY"):
        UniverseScanner(provider, config()).scan(as_of=AS_OF, portfolio_symbols=())


class TradingStub:
    def __init__(self) -> None:
        self.request: object | None = None

    def get_all_assets(self, request: object) -> list[object]:
        self.request = request
        return [
            SimpleNamespace(
                symbol="SPY",
                name="SPDR S&P 500 ETF",
                asset_class="us_equity",
                status="active",
                tradable=True,
                exchange="ARCA",
                fractionable=True,
                marginable=True,
                shortable=True,
                easy_to_borrow=True,
            )
        ]


class ScreenerStub:
    def __init__(self) -> None:
        self.most_active_request: object | None = None
        self.movers_request: object | None = None

    def get_most_actives(self, request: object) -> object:
        self.most_active_request = request
        return SimpleNamespace(
            last_updated=AS_OF,
            most_actives=[SimpleNamespace(symbol="SPY", volume=1000, trade_count=50)],
        )

    def get_market_movers(self, request: object) -> object:
        self.movers_request = request
        return SimpleNamespace(
            last_updated=AS_OF,
            gainers=[SimpleNamespace(symbol="SPY", percent_change=1, change=1, price=100)],
            losers=[],
        )


def test_alpaca_provider_uses_scoped_asset_and_screener_requests() -> None:
    trading = TradingStub()
    screener = ScreenerStub()
    provider = AlpacaUniverseProvider.__new__(AlpacaUniverseProvider)
    provider.trading = cast(Any, trading)
    provider.screener = cast(Any, screener)

    assets = provider.list_active_us_equities()
    active_batch = provider.get_most_actives(by="volume", top=5)
    mover_batch = provider.get_market_movers(top=5)

    asset_request = cast(Any, trading.request)
    assert asset_request.status.value == "active"
    assert asset_request.asset_class.value == "us_equity"
    assert assets[0].symbol == "SPY"
    assert assets[0].fractionable is True
    assert cast(Any, screener.most_active_request).top == 5
    assert active_batch.stocks[0].volume == 1000
    assert cast(Any, screener.movers_request).market_type.value == "stocks"
    assert mover_batch.gainers[0].percent_change == 1
