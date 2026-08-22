"""Alpaca paper broker adapter."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Asset as AlpacaAsset
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from .models import (
    Account,
    BrokerFill,
    BrokerOrder,
    MarketClock,
    OrderQueryStatus,
    Position,
    Quote,
    TradableAsset,
)
from .models import (
    OrderSide as BrokerOrderSide,
)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _decimal(value: object, field: str) -> Decimal:
    if value is None:
        raise RuntimeError(f"Alpaca response omitted required {field}")
    return Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _not_found(exc: APIError) -> bool:
    return bool(exc.status_code == 404)


class AlpacaPaperBroker:
    """Hard-coded paper endpoint adapter; this class cannot select Alpaca live trading."""

    def __init__(self, key: str, secret: str) -> None:
        self.trading = TradingClient(key, secret, paper=True)
        self.data = StockHistoricalDataClient(key, secret)

    @property
    def is_paper(self) -> bool:
        """Expose the execution environment without leaking credentials."""
        return True

    @property
    def paper_options_level_is_provider_managed(self) -> bool:
        """Alpaca currently forces newly created paper accounts to options level 3."""
        return True

    @staticmethod
    def _order(value: AlpacaOrder) -> BrokerOrder:
        if value.symbol is None or value.side is None:
            raise RuntimeError("Alpaca returned an order without a symbol or side")
        side = _enum_value(value.side)
        if side not in {"buy", "sell"}:
            raise RuntimeError(f"Alpaca returned unsupported order side {side!r}")
        return BrokerOrder(
            id=str(value.id),
            client_order_id=value.client_order_id,
            symbol=value.symbol,
            side=cast("BrokerOrderSide", side),
            status=_enum_value(value.status),
            qty=_optional_decimal(value.qty),
            limit_price=_optional_decimal(value.limit_price),
            filled_qty=_optional_decimal(value.filled_qty) or Decimal("0"),
            filled_avg_price=_optional_decimal(value.filled_avg_price),
            submitted_at=value.submitted_at,
            updated_at=value.updated_at,
        )

    def get_account(self) -> Account:
        account = cast("TradeAccount", self.trading.get_account())
        return Account(
            equity=_decimal(account.equity, "account equity"),
            cash=_decimal(account.cash, "account cash"),
            buying_power=_decimal(account.buying_power, "account buying power"),
            trading_blocked=bool(account.trading_blocked),
            shorting_enabled=bool(account.shorting_enabled),
            options_level=getattr(account, "options_trading_level", 0) or 0,
            multiplier=_decimal(account.multiplier, "account multiplier"),
        )

    def get_positions(self) -> list[Position]:
        positions = cast("list[AlpacaPosition]", self.trading.get_all_positions())
        return [
            Position(
                symbol=position.symbol,
                qty=_decimal(position.qty, "position quantity"),
                market_value=_decimal(position.market_value, "position market value"),
                current_price=_decimal(position.current_price, "position current price"),
            )
            for position in positions
        ]

    def get_open_orders(self) -> list[BrokerOrder]:
        return self.get_orders(status="open")

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]:
        query_status = {
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all": QueryOrderStatus.ALL,
        }[status]
        request = GetOrdersRequest(
            status=query_status,
            limit=limit,
            after=after,
            until=until,
            direction=Sort.DESC,
            nested=False,
        )
        orders = cast("list[AlpacaOrder]", self.trading.get_orders(request))
        return [self._order(order) for order in orders]

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            order = cast("AlpacaOrder", self.trading.get_order_by_client_id(client_order_id))
            return self._order(order)
        except APIError as exc:
            if _not_found(exc):
                return None
            raise

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]:
        """Read Alpaca's authoritative fill ledger from the paper trading API."""
        params: dict[str, str | int] = {"direction": "asc", "page_size": 100}
        if after is not None:
            params["after"] = after.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        fills: list[BrokerFill] = []
        previous_page_token: str | None = None
        while True:
            response = self.trading.get("/account/activities/FILL", params)
            if not isinstance(response, list):
                raise RuntimeError("Alpaca returned an invalid fills response")
            for item in response:
                if not isinstance(item, dict):
                    raise RuntimeError("Alpaca returned an invalid fill item")
                fills.append(self._fill(item))
            if len(response) < 100:
                break
            last_item = response[-1]
            if not isinstance(last_item, dict) or "id" not in last_item:
                raise RuntimeError("Alpaca fill page omitted its pagination token")
            page_token = str(last_item["id"])
            if page_token == previous_page_token:
                raise RuntimeError("Alpaca repeated a fill pagination token")
            params["page_token"] = page_token
            previous_page_token = page_token
        return fills

    def get_clock(self) -> MarketClock:
        clock = cast("AlpacaClock", self.trading.get_clock())
        return MarketClock(
            timestamp=clock.timestamp,
            is_open=clock.is_open,
            next_open=clock.next_open,
            next_close=clock.next_close,
        )

    @staticmethod
    def _fill(value: dict[str, Any]) -> BrokerFill:
        side = str(value["side"]).lower()
        if side not in {"buy", "sell"}:
            raise RuntimeError(f"Alpaca returned unsupported fill side {side!r}")
        return BrokerFill(
            id=str(value["id"]),
            order_id=str(value["order_id"]),
            symbol=str(value["symbol"]),
            side=cast("BrokerOrderSide", side),
            qty=_decimal(value["qty"], "fill quantity"),
            price=_decimal(value["price"], "fill price"),
            cumulative_qty=_decimal(value["cum_qty"], "fill cumulative quantity"),
            leaves_qty=_decimal(value["leaves_qty"], "fill leaves quantity"),
            transaction_time=datetime.fromisoformat(
                str(value["transaction_time"]).replace("Z", "+00:00")
            ),
            order_status=str(value["order_status"]).lower(),
        )

    def get_quote(self, symbol: str) -> Quote:
        normalized = symbol.upper().strip()
        quotes = self.data.get_stock_latest_quote(
            StockLatestQuoteRequest(
                symbol_or_symbols=normalized,
                feed=DataFeed.IEX,
            )
        )
        now = datetime.now(UTC)
        bars = self.data.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=normalized,
                start=now - timedelta(days=45),
                end=now,
                limit=30,
                timeframe=TimeFrame.Day,
                adjustment=Adjustment.ALL,
                feed=DataFeed.IEX,
                sort=Sort.DESC,
            )
        )
        symbol_bars = bars[normalized]
        if not symbol_bars:
            raise RuntimeError(f"Alpaca returned no daily bars for {normalized}")
        dollar_volumes = [
            _decimal(bar.close, "bar close") * _decimal(bar.volume, "bar volume")
            for bar in symbol_bars
        ]
        average_daily_dollar_volume = sum(dollar_volumes, Decimal("0")) / Decimal(
            len(dollar_volumes)
        )
        quote = quotes[normalized]
        return Quote(
            symbol=normalized,
            bid=quote.bid_price,
            ask=quote.ask_price,
            timestamp=quote.timestamp,
            feed="alpaca-iex",
            average_daily_dollar_volume=average_daily_dollar_volume,
        )

    def get_asset(self, symbol: str) -> TradableAsset:
        asset = cast("AlpacaAsset", self.trading.get_asset(symbol))
        return TradableAsset(
            symbol=asset.symbol,
            asset_class=_enum_value(asset.asset_class),
            status=_enum_value(asset.status),
            tradable=asset.tradable,
            exchange=str(getattr(asset.exchange, "value", asset.exchange)),
        )

    def submit_limit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: str,
        limit_price: str,
    ) -> BrokerOrder:
        if side not in {"buy", "sell"}:
            raise ValueError(f"unsupported order side {side!r}")
        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            limit_price=limit_price,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            extended_hours=False,
        )
        order = cast("AlpacaOrder", self.trading.submit_order(order_data=request))
        return self._order(order)

    def cancel_order(self, order_id: str) -> None:
        self.trading.cancel_order_by_id(order_id)

    def cancel_all_orders(self) -> None:
        self.trading.cancel_orders()
