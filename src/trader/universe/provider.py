"""Alpaca-backed asset and market-screener boundary."""

from typing import cast

from alpaca.data.enums import MarketType, MostActivesBy
from alpaca.data.historical import ScreenerClient
from alpaca.data.models.screener import MostActives, Movers
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.models import Asset as AlpacaAsset
from alpaca.trading.requests import GetAssetsRequest

from trader.universe.models import (
    ActiveStockSignal,
    MarketMoversBatch,
    MostActiveBatch,
    MoverSignal,
    UniverseAsset,
)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


class AlpacaUniverseProvider:
    """Read-only provider for active U.S. equities and Alpaca's stock screeners."""

    def __init__(self, key: str, secret: str) -> None:
        self.trading = TradingClient(key, secret, paper=True)
        self.screener = ScreenerClient(key, secret)

    def list_active_us_equities(self) -> tuple[UniverseAsset, ...]:
        request = GetAssetsRequest(
            status=AssetStatus.ACTIVE,
            asset_class=AssetClass.US_EQUITY,
        )
        assets = cast("list[AlpacaAsset]", self.trading.get_all_assets(request))
        return tuple(self._asset(asset) for asset in assets)

    def get_most_actives(
        self,
        *,
        by: str,
        top: int,
    ) -> MostActiveBatch:
        selection = {
            "volume": MostActivesBy.VOLUME,
            "trades": MostActivesBy.TRADES,
        }.get(by)
        if selection is None:
            raise ValueError(f"unsupported most-active ranking {by!r}")
        response = cast(
            "MostActives",
            self.screener.get_most_actives(MostActivesRequest(top=top, by=selection)),
        )
        return MostActiveBatch(
            by=selection.value,
            last_updated=response.last_updated,
            stocks=tuple(
                ActiveStockSignal(
                    symbol=stock.symbol,
                    volume=stock.volume,
                    trade_count=stock.trade_count,
                )
                for stock in response.most_actives
            ),
        )

    def get_market_movers(self, *, top: int) -> MarketMoversBatch:
        response = cast(
            "Movers",
            self.screener.get_market_movers(
                MarketMoversRequest(top=top, market_type=MarketType.STOCKS)
            ),
        )
        return MarketMoversBatch(
            last_updated=response.last_updated,
            gainers=tuple(
                MoverSignal(
                    symbol=mover.symbol,
                    percent_change=mover.percent_change,
                    change=mover.change,
                    price=mover.price,
                )
                for mover in response.gainers
            ),
            losers=tuple(
                MoverSignal(
                    symbol=mover.symbol,
                    percent_change=mover.percent_change,
                    change=mover.change,
                    price=mover.price,
                )
                for mover in response.losers
            ),
        )

    @staticmethod
    def _asset(asset: AlpacaAsset) -> UniverseAsset:
        return UniverseAsset(
            symbol=asset.symbol,
            name=asset.name,
            asset_class=_enum_value(asset.asset_class),
            status=_enum_value(asset.status),
            tradable=asset.tradable,
            exchange=str(getattr(asset.exchange, "value", asset.exchange)),
            fractionable=asset.fractionable,
            marginable=asset.marginable,
            shortable=asset.shortable,
            easy_to_borrow=asset.easy_to_borrow,
        )
