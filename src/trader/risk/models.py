"""Typed inputs and outputs for deterministic risk authorization."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.agent.models import SYMBOL_PATTERN, TradeProposal
from trader.broker.models import Account, BrokerOrder, Position, Quote, TradableAsset


class RiskPolicy(BaseModel):
    """Flattened, immutable policy produced only by the strict config loader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_max_equity_usd: Decimal
    minimum_cash_reserve_pct: Decimal
    max_invested_pct: Decimal
    max_single_position_pct: Decimal
    max_single_position_usd: Decimal
    max_positions: int
    min_order_usd: Decimal
    max_new_gross_exposure_per_day_usd: Decimal
    max_orders_per_day: int
    max_trades_per_symbol_per_day: int
    max_daily_drawdown_pct: Decimal
    max_weekly_drawdown_pct: Decimal
    max_peak_to_trough_drawdown_pct: Decimal
    min_price_usd: Decimal
    min_average_daily_dollar_volume_usd: Decimal
    max_limit_slippage_bps: Decimal
    market_data_max_age_seconds: int
    allowed_symbols: frozenset[str]

    @field_validator(
        "expected_max_equity_usd",
        "max_single_position_usd",
        "min_order_usd",
        "max_new_gross_exposure_per_day_usd",
        "min_price_usd",
        "min_average_daily_dollar_volume_usd",
    )
    @classmethod
    def finite_positive_money(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("monetary risk limits must be finite and positive")
        return value

    @field_validator(
        "minimum_cash_reserve_pct",
        "max_invested_pct",
        "max_single_position_pct",
        "max_daily_drawdown_pct",
        "max_weekly_drawdown_pct",
        "max_peak_to_trough_drawdown_pct",
    )
    @classmethod
    def valid_percentage(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value > 100:
            raise ValueError("percentage risk limits must be finite and between 0 and 100")
        return value

    @field_validator("max_limit_slippage_bps")
    @classmethod
    def valid_slippage(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0 or value > 1_000:
            raise ValueError("slippage must be finite and between 0 and 1,000 bps")
        return value

    @field_validator(
        "max_positions",
        "max_orders_per_day",
        "max_trades_per_symbol_per_day",
        "market_data_max_age_seconds",
        mode="before",
    )
    @classmethod
    def integer_not_boolean(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("integer risk limits must be integers")
        return value

    @field_validator(
        "max_positions",
        "max_orders_per_day",
        "max_trades_per_symbol_per_day",
        "market_data_max_age_seconds",
    )
    @classmethod
    def positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("integer risk limits must be positive")
        return value

    @field_validator("allowed_symbols")
    @classmethod
    def nonempty_universe(cls, value: frozenset[str]) -> frozenset[str]:
        if not value:
            raise ValueError("allowed symbol universe cannot be empty")
        normalized = frozenset(symbol.upper().strip() for symbol in value)
        if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized):
            raise ValueError("allowed symbol universe contains an invalid symbol")
        return normalized

    @model_validator(mode="after")
    def coherent_limits(self) -> "RiskPolicy":
        if self.minimum_cash_reserve_pct + self.max_invested_pct > 100:
            raise ValueError("cash reserve and maximum invested percentages exceed 100")
        if self.max_single_position_pct > self.max_invested_pct:
            raise ValueError("single-position percentage exceeds maximum invested percentage")
        if self.min_order_usd > self.max_single_position_usd:
            raise ValueError("minimum order exceeds maximum position size")
        if not (
            self.max_daily_drawdown_pct
            <= self.max_weekly_drawdown_pct
            <= self.max_peak_to_trough_drawdown_pct
        ):
            raise ValueError("drawdown limits must be ordered daily <= weekly <= peak")
        return self


class NormalizedOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    side: str
    qty: Decimal
    limit_price: Decimal
    notional: Decimal


class RiskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    approved: bool
    normalized_order: NormalizedOrder | None = None
    rejection_codes: list[str] = Field(default_factory=list)
    human_explanation: str


class RiskContext(BaseModel):
    """A complete, time-pinned snapshot used by the pure risk evaluator."""

    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    account: Account
    positions: list[Position]
    open_orders: list[BrokerOrder]
    quotes: dict[str, Quote]
    assets: dict[str, TradableAsset]
    proposals: list[TradeProposal]
    policy: RiskPolicy
    daily_drawdown_pct: Decimal
    weekly_drawdown_pct: Decimal
    peak_drawdown_pct: Decimal
    orders_today: int
    daily_new_gross_exposure_usd: Decimal
    trades_per_symbol_today: dict[str, int]
    symbols_traded_today: set[str] = Field(default_factory=set)
    broker_state_known: bool
    open_order_state_known: bool
    portfolio_state_known: bool
    market_is_open: bool
    paper_options_level_is_provider_managed: bool

    @field_validator("as_of")
    @classmethod
    def timezone_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @field_validator(
        "daily_drawdown_pct",
        "weekly_drawdown_pct",
        "peak_drawdown_pct",
        "daily_new_gross_exposure_usd",
    )
    @classmethod
    def finite_nonnegative_metric(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("risk context metrics must be finite and nonnegative")
        return value

    @field_validator("orders_today", mode="before")
    @classmethod
    def order_count_not_boolean(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("orders_today must be an integer")
        return value

    @field_validator("orders_today")
    @classmethod
    def nonnegative_order_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("orders_today cannot be negative")
        return value

    @field_validator("trades_per_symbol_today", mode="before")
    @classmethod
    def trade_counts_are_integers(cls, value: object) -> object:
        if not isinstance(value, dict) or any(
            type(count) is not int for count in value.values()
        ):
            raise ValueError("trade counts must be integers")
        return value

    @field_validator("trades_per_symbol_today")
    @classmethod
    def nonnegative_trade_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("trade counts cannot be negative")
        return {symbol.upper().strip(): count for symbol, count in value.items()}

    @field_validator(
        "broker_state_known",
        "open_order_state_known",
        "portfolio_state_known",
        "market_is_open",
        "paper_options_level_is_provider_managed",
        mode="before",
    )
    @classmethod
    def state_flags_are_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("state-known flags must be booleans")
        return value
