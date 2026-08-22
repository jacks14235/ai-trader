"""Broker-neutral value objects."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

OrderSide = Literal["buy", "sell"]
OrderQueryStatus = Literal["open", "closed", "all"]


class Account(BaseModel):
    model_config = ConfigDict(frozen=True)

    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    trading_blocked: bool = False
    shorting_enabled: bool = False
    options_level: int = 0
    multiplier: Decimal = Decimal("1")


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    qty: Decimal
    market_value: Decimal
    current_price: Decimal


class BrokerOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    status: str
    qty: Decimal | None = None
    limit_price: Decimal | None = None
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Decimal | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None


class BrokerFill(BaseModel):
    """An execution activity returned by the broker's account ledger."""

    model_config = ConfigDict(frozen=True)

    id: str
    order_id: str
    symbol: str
    side: OrderSide
    qty: Decimal
    price: Decimal
    cumulative_qty: Decimal
    leaves_qty: Decimal
    transaction_time: datetime
    order_status: str


class TradableAsset(BaseModel):
    """Broker-authoritative instrument metadata used by deterministic risk checks."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    asset_class: str
    status: str
    tradable: bool
    exchange: str | None = None


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    feed: str = "unknown"
    average_daily_dollar_volume: Decimal | None = None


class MarketClock(BaseModel):
    """Broker-authoritative regular-session state used by the risk engine."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime
