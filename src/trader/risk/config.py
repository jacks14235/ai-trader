"""Fail-closed loading for the human-owned risk and universe configuration."""

import re
from collections.abc import Collection
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.risk.models import RiskPolicy

SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrokerAccountConfig(StrictConfigModel):
    no_shorting: Literal[True]
    max_margin_multiplier: Literal[1]
    max_options_trading_level: Literal[0]
    disable_overnight_trading: Literal[True]

    @field_validator("no_shorting", "disable_overnight_trading", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("broker safety flags must be booleans")
        return value

    @field_validator("max_margin_multiplier", "max_options_trading_level", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("broker account limits must be integers")
        return value


class PortfolioConfig(StrictConfigModel):
    expected_max_equity_usd: Decimal
    minimum_cash_reserve_pct: Decimal
    max_invested_pct: Decimal


class PositionsConfig(StrictConfigModel):
    max_single_position_pct: Decimal
    max_single_position_usd: Decimal
    max_positions: int
    min_order_usd: Decimal

    @field_validator("max_positions", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("max_positions must be an integer")
        return value


class ActivityConfig(StrictConfigModel):
    max_new_gross_exposure_per_day_usd: Decimal
    max_orders_per_day: int
    max_trades_per_symbol_per_day: int

    @field_validator("max_orders_per_day", "max_trades_per_symbol_per_day", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("activity counts must be integers")
        return value


class LossLimitsConfig(StrictConfigModel):
    max_daily_drawdown_pct: Decimal
    max_weekly_drawdown_pct: Decimal
    max_peak_to_trough_drawdown_pct: Decimal


class InstrumentsConfig(StrictConfigModel):
    equities: bool
    etfs: bool
    options: Literal[False]
    crypto: Literal[False]
    shorting: Literal[False]
    margin: Literal[False]
    leveraged_etfs: Literal[False]
    inverse_etfs: Literal[False]
    otc: Literal[False]
    penny_stocks: Literal[False]

    @field_validator("*", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("instrument flags must be booleans")
        return value

    @model_validator(mode="after")
    def supported_long_only_instrument(self) -> "InstrumentsConfig":
        if not self.equities and not self.etfs:
            raise ValueError("at least one supported long-only instrument must be enabled")
        return self


class OrdersConfig(StrictConfigModel):
    extended_hours: Literal[False]
    allow_market_orders: Literal[False]
    default_order_type: Literal["limit"]
    max_limit_slippage_bps: Decimal

    @field_validator("extended_hours", "allow_market_orders", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("order safety flags must be booleans")
        return value


class LiquidityConfig(StrictConfigModel):
    min_price_usd: Decimal
    min_average_daily_dollar_volume_usd: Decimal
    market_data_max_age_seconds: int

    @field_validator("market_data_max_age_seconds", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("market-data age must be an integer")
        return value


class CircuitBreakersConfig(StrictConfigModel):
    reject_if_broker_state_unknown: Literal[True]
    reject_if_market_data_stale: Literal[True]
    reject_if_database_write_fails: Literal[True]
    reject_if_open_order_state_unknown: Literal[True]

    @field_validator("*", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("circuit breaker flags must be booleans")
        return value


class CandidateSelectionConfig(StrictConfigModel):
    """Bounded deterministic inputs to the daily research candidate slate."""

    max_candidates: int = Field(ge=1, le=100)
    most_active_by_volume: int = Field(ge=1, le=100)
    most_active_by_trades: int = Field(ge=1, le=100)
    market_movers_per_side: int = Field(ge=1, le=50)
    exploration_candidates: int = Field(ge=0, le=25)

    @field_validator("*", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("candidate selection limits must be integers")
        return value


class RiskConfig(StrictConfigModel):
    """The exact on-disk risk.yaml shape; every safety section is required."""

    mode: Literal["paper"]
    broker_account: BrokerAccountConfig
    portfolio: PortfolioConfig
    positions: PositionsConfig
    activity: ActivityConfig
    loss_limits: LossLimitsConfig
    instruments: InstrumentsConfig
    orders: OrdersConfig
    liquidity: LiquidityConfig
    circuit_breakers: CircuitBreakersConfig

    @model_validator(mode="after")
    def coherent_effective_policy(self) -> "RiskConfig":
        self.to_policy(allowed_symbols=("SPY",))
        return self

    def to_policy(
        self,
        universe: "UniverseConfig | None" = None,
        *,
        allowed_symbols: Collection[str] | None = None,
    ) -> RiskPolicy:
        """Build an immutable policy for one bounded, already-discovered symbol set."""
        if allowed_symbols is None:
            if universe is None:
                raise ValueError("a universe or explicit allowed symbol set is required")
            allowed_symbols = universe.benchmark_symbols
        return RiskPolicy(
            expected_max_equity_usd=self.portfolio.expected_max_equity_usd,
            minimum_cash_reserve_pct=self.portfolio.minimum_cash_reserve_pct,
            max_invested_pct=self.portfolio.max_invested_pct,
            max_single_position_pct=self.positions.max_single_position_pct,
            max_single_position_usd=self.positions.max_single_position_usd,
            max_positions=self.positions.max_positions,
            min_order_usd=self.positions.min_order_usd,
            max_new_gross_exposure_per_day_usd=(
                self.activity.max_new_gross_exposure_per_day_usd
            ),
            max_orders_per_day=self.activity.max_orders_per_day,
            max_trades_per_symbol_per_day=self.activity.max_trades_per_symbol_per_day,
            max_daily_drawdown_pct=self.loss_limits.max_daily_drawdown_pct,
            max_weekly_drawdown_pct=self.loss_limits.max_weekly_drawdown_pct,
            max_peak_to_trough_drawdown_pct=(
                self.loss_limits.max_peak_to_trough_drawdown_pct
            ),
            min_price_usd=self.liquidity.min_price_usd,
            min_average_daily_dollar_volume_usd=(
                self.liquidity.min_average_daily_dollar_volume_usd
            ),
            max_limit_slippage_bps=self.orders.max_limit_slippage_bps,
            market_data_max_age_seconds=self.liquidity.market_data_max_age_seconds,
            allowed_symbols=frozenset(allowed_symbols),
        )


class UniverseConfig(StrictConfigModel):
    source: Literal["alpaca"]
    asset_class: Literal["us_equity"]
    require_active: Literal[True]
    require_tradable: Literal[True]
    benchmark_symbols: tuple[str, ...]
    event_symbols: tuple[str, ...]
    excluded_symbols: tuple[str, ...]
    candidate_selection: CandidateSelectionConfig

    @field_validator("benchmark_symbols", "event_symbols", "excluded_symbols")
    @classmethod
    def valid_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(symbol.upper().strip() for symbol in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbol lists cannot contain duplicates")
        if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized):
            raise ValueError("invalid symbol in universe")
        return normalized

    @model_validator(mode="after")
    def coherent_symbol_sets(self) -> "UniverseConfig":
        if not self.benchmark_symbols or not self.event_symbols:
            raise ValueError("benchmark and event symbol lists cannot be empty")
        pinned = set(self.benchmark_symbols).union(self.event_symbols)
        if pinned.intersection(self.excluded_symbols):
            raise ValueError("pinned benchmark/event symbols cannot be excluded")
        if len(pinned) > self.candidate_selection.max_candidates:
            raise ValueError("candidate cap is smaller than the pinned symbol set")
        if (
            len(self.benchmark_symbols) + self.candidate_selection.exploration_candidates
            > self.candidate_selection.max_candidates
        ):
            raise ValueError("candidate cap cannot preserve benchmarks and exploration slots")
        return self


def _read_yaml(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load configuration {path}: {exc}") from exc


def load_risk_config(path: Path) -> RiskConfig:
    """Load the complete risk file or raise; no defaults are supplied."""
    return RiskConfig.model_validate(_read_yaml(path))


def load_universe_config(path: Path) -> UniverseConfig:
    """Load and normalize the complete symbol universe or raise."""
    return UniverseConfig.model_validate(_read_yaml(path))


def load_risk_policy(
    risk_path: Path,
    universe_path: Path,
    *,
    allowed_symbols: Collection[str] | None = None,
) -> RiskPolicy:
    """Load both human-owned files and return one immutable effective policy."""
    return load_risk_config(risk_path).to_policy(
        load_universe_config(universe_path),
        allowed_symbols=allowed_symbols,
    )
