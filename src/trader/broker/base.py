"""Narrow broker boundary; reasoning code never receives this object."""

from datetime import datetime
from typing import Protocol

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


class Broker(Protocol):
    @property
    def is_paper(self) -> bool: ...

    @property
    def paper_options_level_is_provider_managed(self) -> bool: ...

    def get_account(self) -> Account: ...

    def get_positions(self) -> list[Position]: ...

    def get_open_orders(self) -> list[BrokerOrder]: ...

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]: ...

    def get_clock(self) -> MarketClock: ...

    def get_quote(self, symbol: str) -> Quote: ...

    def get_asset(self, symbol: str) -> TradableAsset: ...

    def submit_limit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: str,
        limit_price: str,
    ) -> BrokerOrder: ...

    def cancel_order(self, order_id: str) -> None: ...

    def cancel_all_orders(self) -> None: ...
