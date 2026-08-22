"""Deterministic selection of a small research slate from a broad asset universe."""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.risk.config import UniverseConfig
from trader.universe.models import (
    CandidateSignal,
    MarketMoversBatch,
    MostActiveBatch,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)

EASTERN = ZoneInfo("America/New_York")
MAX_PROVIDER_ASSETS = 20_000
SOURCE_WEIGHTS = {
    "PORTFOLIO": 10_000,
    "BENCHMARK": 9_000,
    "MOST_ACTIVE_VOLUME": 4_000,
    "MOST_ACTIVE_TRADES": 3_500,
    "TOP_GAINER": 3_000,
    "TOP_LOSER": 3_000,
    "EXPLORATION": 100,
}


class UniverseProvider(Protocol):
    def list_active_us_equities(self) -> tuple[UniverseAsset, ...]: ...

    def get_most_actives(self, *, by: str, top: int) -> MostActiveBatch: ...

    def get_market_movers(self, *, top: int) -> MarketMoversBatch: ...


class CandidateScanner(Protocol):
    def scan(
        self,
        *,
        as_of: datetime,
        portfolio_symbols: tuple[str, ...],
    ) -> UniverseScan: ...


class UniverseScanner:
    """Create one reproducible, auditable daily candidate slate."""

    def __init__(self, provider: UniverseProvider, config: UniverseConfig) -> None:
        self.provider = provider
        self.config = config

    def scan(
        self,
        *,
        as_of: datetime,
        portfolio_symbols: tuple[str, ...] = (),
    ) -> UniverseScan:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("universe scan time must be timezone-aware")
        normalized_portfolio = tuple(symbol.upper().strip() for symbol in portfolio_symbols)
        if len(normalized_portfolio) != len(set(normalized_portfolio)):
            raise ValueError("portfolio symbols cannot contain duplicates")

        assets = self.provider.list_active_us_equities()
        if not assets or len(assets) > MAX_PROVIDER_ASSETS:
            raise RuntimeError("Alpaca returned an empty or oversized asset universe")
        eligible = self._eligible_assets(assets)
        by_symbol = {asset.symbol: asset for asset in eligible}
        pinned = set(normalized_portfolio).union(self.config.benchmark_symbols)
        missing_pinned = sorted(pinned.difference(by_symbol))
        if missing_pinned:
            raise RuntimeError(
                "pinned portfolio/benchmark symbols are not active tradable U.S. equities: "
                + ", ".join(missing_pinned)
            )
        if len(pinned) > self.config.candidate_selection.max_candidates:
            raise RuntimeError("candidate cap is smaller than current pinned positions")

        selection = self.config.candidate_selection
        active_volume = self.provider.get_most_actives(
            by="volume", top=selection.most_active_by_volume
        )
        active_trades = self.provider.get_most_actives(
            by="trades", top=selection.most_active_by_trades
        )
        movers = self.provider.get_market_movers(top=selection.market_movers_per_side)

        signals: dict[str, list[CandidateSignal]] = defaultdict(list)
        for symbol in normalized_portfolio:
            signals[symbol].append(CandidateSignal(source="PORTFOLIO"))
        for symbol in self.config.benchmark_symbols:
            signals[symbol].append(CandidateSignal(source="BENCHMARK"))

        skipped = 0
        skipped += self._add_active_signals(
            signals, by_symbol, active_volume, "MOST_ACTIVE_VOLUME", "volume"
        )
        skipped += self._add_active_signals(
            signals, by_symbol, active_trades, "MOST_ACTIVE_TRADES", "trade_count"
        )
        skipped += self._add_mover_signals(signals, by_symbol, movers)

        exploration_pool = sorted(
            set(by_symbol).difference(signals),
            key=lambda symbol: self._exploration_key(as_of, symbol),
        )
        for rank, symbol in enumerate(
            exploration_pool[: selection.exploration_candidates], start=1
        ):
            signals[symbol].append(CandidateSignal(source="EXPLORATION", rank=rank))

        candidates = tuple(
            self._candidate(symbol, by_symbol[symbol], symbol_signals)
            for symbol, symbol_signals in signals.items()
        )
        pinned_candidates = sorted(
            (candidate for candidate in candidates if candidate.symbol in pinned),
            key=self._candidate_order,
        )
        exploration_candidates = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.symbol not in pinned
                and any(signal.source == "EXPLORATION" for signal in candidate.signals)
            ),
            key=lambda candidate: next(
                signal.rank or 0
                for signal in candidate.signals
                if signal.source == "EXPLORATION"
            ),
        )
        other_candidates = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.symbol not in pinned
                and not any(signal.source == "EXPLORATION" for signal in candidate.signals)
            ),
            key=self._candidate_order,
        )
        remaining = selection.max_candidates - len(pinned_candidates)
        reserved_exploration = exploration_candidates[:remaining]
        remaining -= len(reserved_exploration)
        selected = tuple(
            sorted(
                pinned_candidates
                + other_candidates[:remaining]
                + reserved_exploration,
                key=self._candidate_order,
            )
        )
        if not selected:
            raise RuntimeError("candidate scanner produced an empty research slate")

        canonical_assets = json.dumps(
            [asset.model_dump(mode="json") for asset in eligible],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return UniverseScan(
            as_of=as_of.astimezone(UTC),
            asset_content_hash=hashlib.sha256(canonical_assets).hexdigest(),
            eligible_assets=eligible,
            candidates=selected,
            most_active_volume_updated_at=active_volume.last_updated,
            most_active_trades_updated_at=active_trades.last_updated,
            market_movers_updated_at=movers.last_updated,
            skipped_screener_symbols=skipped,
        )

    def _eligible_assets(
        self,
        assets: tuple[UniverseAsset, ...],
    ) -> tuple[UniverseAsset, ...]:
        excluded = set(self.config.excluded_symbols)
        by_symbol: dict[str, UniverseAsset] = {}
        for asset in assets:
            if asset.symbol in by_symbol:
                raise RuntimeError(f"Alpaca returned duplicate asset {asset.symbol}")
            by_symbol[asset.symbol] = asset
        return tuple(
            asset
            for asset in sorted(by_symbol.values(), key=lambda item: item.symbol)
            if asset.asset_class == self.config.asset_class
            and (not self.config.require_active or asset.status == "active")
            and (not self.config.require_tradable or asset.tradable)
            and asset.symbol not in excluded
        )

    @staticmethod
    def _add_active_signals(
        signals: dict[str, list[CandidateSignal]],
        assets: dict[str, UniverseAsset],
        batch: MostActiveBatch,
        source: str,
        metric_name: str,
    ) -> int:
        skipped = 0
        for rank, stock in enumerate(batch.stocks, start=1):
            if stock.symbol not in assets:
                skipped += 1
                continue
            value = stock.volume if metric_name == "volume" else stock.trade_count
            signals[stock.symbol].append(
                CandidateSignal(
                    source=source,
                    rank=rank,
                    metric_name=metric_name,
                    metric_value=value,
                )
            )
        return skipped

    @staticmethod
    def _add_mover_signals(
        signals: dict[str, list[CandidateSignal]],
        assets: dict[str, UniverseAsset],
        batch: MarketMoversBatch,
    ) -> int:
        skipped = 0
        for source, movers in (("TOP_GAINER", batch.gainers), ("TOP_LOSER", batch.losers)):
            for rank, mover in enumerate(movers, start=1):
                if mover.symbol not in assets:
                    skipped += 1
                    continue
                signals[mover.symbol].append(
                    CandidateSignal(
                        source=source,
                        rank=rank,
                        metric_name="percent_change",
                        metric_value=mover.percent_change,
                    )
                )
        return skipped

    @staticmethod
    def _exploration_key(as_of: datetime, symbol: str) -> tuple[str, str]:
        trading_date = as_of.astimezone(EASTERN).date().isoformat()
        return hashlib.sha256(f"{trading_date}\0{symbol}".encode()).hexdigest(), symbol

    @staticmethod
    def _candidate(
        symbol: str,
        asset: UniverseAsset,
        signals: list[CandidateSignal],
    ) -> ResearchCandidate:
        ordered_signals = tuple(
            sorted(
                signals,
                key=lambda signal: (
                    -SOURCE_WEIGHTS[signal.source],
                    signal.rank or 0,
                    signal.source,
                ),
            )
        )
        score = sum(
            SOURCE_WEIGHTS[signal.source] - (signal.rank or 0) for signal in ordered_signals
        )
        return ResearchCandidate(
            symbol=symbol,
            score=score,
            asset=asset,
            signals=ordered_signals,
        )

    @staticmethod
    def _candidate_order(candidate: ResearchCandidate) -> tuple[int, str]:
        return -candidate.score, candidate.symbol
