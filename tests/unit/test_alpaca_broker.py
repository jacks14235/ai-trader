from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from alpaca.data.enums import DataFeed
from alpaca.trading.enums import QueryOrderStatus

import trader.broker.alpaca as alpaca_module
from trader.broker.alpaca import AlpacaPaperBroker


class TradingStub:
    def __init__(self) -> None:
        self.request: object | None = None
        self.canceled: list[str] = []
        self.cancel_all_calls = 0
        self.activity_response: object = []
        self.asset_response: object | None = None
        self.clock_response: object | None = None

    def get_orders(self, request: object) -> list[object]:
        self.request = request
        return []

    def get(self, path: str, params: dict[str, object]) -> object:
        assert path == "/account/activities/FILL"
        assert params["page_size"] == 100
        return self.activity_response

    def cancel_order_by_id(self, order_id: str) -> None:
        self.canceled.append(order_id)

    def cancel_orders(self) -> None:
        self.cancel_all_calls += 1

    def get_asset(self, symbol: str) -> object:
        assert symbol == "SPY"
        assert self.asset_response is not None
        return self.asset_response

    def get_clock(self) -> object:
        assert self.clock_response is not None
        return self.clock_response


class DataStub:
    def __init__(self) -> None:
        timestamp = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)
        self.quote = SimpleNamespace(
            bid_price=Decimal("9.99"),
            ask_price=Decimal("10.01"),
            timestamp=timestamp,
        )
        self.bars = [
            SimpleNamespace(close=Decimal("10"), volume=Decimal("1000000")),
            SimpleNamespace(close=Decimal("12"), volume=Decimal("1000000")),
        ]
        self.quote_request: object | None = None
        self.bars_request: object | None = None

    def get_stock_latest_quote(self, request: object) -> dict[str, object]:
        self.quote_request = request
        return {"SPY": self.quote}

    def get_stock_bars(self, request: object) -> dict[str, object]:
        self.bars_request = request
        return {"SPY": self.bars}


def broker_with_trading(stub: TradingStub) -> AlpacaPaperBroker:
    broker = AlpacaPaperBroker.__new__(AlpacaPaperBroker)
    broker.trading = cast("Any", stub)
    broker.data = cast("Any", None)
    return broker


def test_constructor_can_only_select_paper_trading(monkeypatch: Any) -> None:
    trading_calls: list[dict[str, object]] = []

    def trading_client(key: str, secret: str, **kwargs: object) -> object:
        trading_calls.append({"key": key, "secret": secret, **kwargs})
        return object()

    monkeypatch.setattr(alpaca_module, "TradingClient", trading_client)
    monkeypatch.setattr(alpaca_module, "StockHistoricalDataClient", lambda *_args: object())

    broker = AlpacaPaperBroker("paper-key", "paper-secret")

    assert broker.is_paper is True
    assert broker.paper_options_level_is_provider_managed is True
    assert trading_calls == [{"key": "paper-key", "secret": "paper-secret", "paper": True}]


def test_all_order_query_and_cancellation() -> None:
    stub = TradingStub()
    broker = broker_with_trading(stub)

    assert broker.get_orders(status="all") == []
    request = cast("Any", stub.request)
    assert request.status is QueryOrderStatus.ALL
    assert request.limit == 500

    broker.cancel_order("order-1")
    broker.cancel_all_orders()
    assert stub.canceled == ["order-1"]
    assert stub.cancel_all_calls == 1


def test_fill_activity_conversion() -> None:
    stub = TradingStub()
    stub.activity_response = [
        {
            "id": "fill-1",
            "order_id": "order-1",
            "symbol": "AAA",
            "side": "buy",
            "qty": "0.5",
            "price": "10.25",
            "cum_qty": "0.5",
            "leaves_qty": "0.5",
            "transaction_time": "2026-08-20T14:30:00Z",
            "order_status": "partially_filled",
        }
    ]
    broker = broker_with_trading(stub)

    fills = broker.get_fills(after=datetime(2026, 8, 20, tzinfo=UTC))

    assert len(fills) == 1
    assert fills[0].qty == Decimal("0.5")
    assert fills[0].leaves_qty == Decimal("0.5")
    assert fills[0].transaction_time.tzinfo is not None


def test_order_conversion_preserves_fill_state() -> None:
    timestamp = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)
    raw = SimpleNamespace(
        id="order-1",
        client_order_id="cid-1",
        symbol="AAA",
        side="buy",
        status="partially_filled",
        qty="2",
        limit_price="10",
        filled_qty="1",
        filled_avg_price="9.99",
        submitted_at=timestamp,
        updated_at=timestamp,
    )

    order = AlpacaPaperBroker._order(cast("Any", raw))

    assert order.filled_qty == Decimal("1")
    assert order.filled_avg_price == Decimal("9.99")
    assert order.status == "partially_filled"


def test_asset_conversion_preserves_equity_safety_metadata() -> None:
    stub = TradingStub()
    stub.asset_response = SimpleNamespace(
        symbol="SPY",
        asset_class="us_equity",
        status="active",
        tradable=True,
        exchange="ARCA",
    )
    broker = broker_with_trading(stub)

    asset = broker.get_asset("SPY")

    assert asset.symbol == "SPY"
    assert asset.asset_class == "us_equity"
    assert asset.status == "active"
    assert asset.tradable is True
    assert asset.exchange == "ARCA"


def test_clock_conversion_is_broker_authoritative() -> None:
    stub = TradingStub()
    timestamp = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)
    stub.clock_response = SimpleNamespace(
        timestamp=timestamp,
        is_open=True,
        next_open=timestamp,
        next_close=datetime(2026, 8, 20, 20, tzinfo=UTC),
    )
    broker = broker_with_trading(stub)

    clock = broker.get_clock()

    assert clock.is_open is True
    assert clock.timestamp == timestamp


def test_quote_includes_bounded_iex_average_daily_dollar_volume() -> None:
    broker = broker_with_trading(TradingStub())
    data = DataStub()
    broker.data = cast("Any", data)

    quote = broker.get_quote("spy")

    assert quote.symbol == "SPY"
    assert quote.feed == "alpaca-iex"
    assert quote.average_daily_dollar_volume == Decimal("11000000")
    quote_request = cast("Any", data.quote_request)
    bars_request = cast("Any", data.bars_request)
    assert quote_request.feed is DataFeed.IEX
    assert bars_request.feed is DataFeed.IEX
    assert bars_request.limit == 30
