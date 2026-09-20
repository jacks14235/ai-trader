"""Value objects for simulated books and their modeled executions."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.agent.models import SYMBOL_PATTERN
from trader.broker.models import Account, MarketClock, Position, Quote, TradableAsset

BookStatus = Literal["active", "paused", "retired"]
FillOutcome = Literal[
    "FILLED",
    "LIMIT_NOT_MARKETABLE",
    "INSUFFICIENT_CASH",
    "INSUFFICIENT_POSITION",
    "NO_QUOTE",
    "STALE_QUOTE",
]


class MarketDataSource(Protocol):
    """Read-only market access.

    Deliberately narrower than `Broker`: it has no order methods at all, so a component typed
    against it cannot submit, cancel, or reconcile anything even though the Alpaca adapter
    happens to satisfy it.
    """

    def get_quote(self, symbol: str) -> Quote: ...

    def get_asset(self, symbol: str) -> TradableAsset: ...

    def get_clock(self) -> MarketClock: ...


class BookModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FillAssumptions(BookModel):
    """The human-owned modeling choices that turn a quote into a fill.

    These are assumptions, not observations. They are recorded on every fill so a book's results
    can be re-derived, and so a reviewer can tell modeling optimism from strategy skill.
    """

    slippage_bps: Decimal = Decimal("0")
    commission_per_share: Decimal = Decimal("0")
    max_quote_age_seconds: int = 900
    cross_the_spread: bool = True

    @field_validator("slippage_bps")
    @classmethod
    def bounded_slippage(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0 or value > 1_000:
            raise ValueError("simulated slippage must be between 0 and 1,000 bps")
        return value

    @field_validator("commission_per_share")
    @classmethod
    def bounded_commission(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0 or value > 1:
            raise ValueError("simulated commission per share must be between 0 and 1")
        return value

    @field_validator("max_quote_age_seconds")
    @classmethod
    def positive_age(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("maximum quote age must be positive")
        return value


class BookPosition(BookModel):
    symbol: str
    qty: Decimal
    average_entry_price: Decimal

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("book position symbol must be a valid equity symbol")
        return normalized

    @model_validator(mode="after")
    def long_only(self) -> "BookPosition":
        if self.qty <= 0:
            raise ValueError("a book position must be a positive long quantity")
        if self.average_entry_price <= 0:
            raise ValueError("a book position must have a positive entry price")
        return self

    def cost_basis(self) -> Decimal:
        return self.qty * self.average_entry_price


class BookState(BookModel):
    """A book's holdings, derived by replaying its fills from ``starting_cash``."""

    book_id: str
    name: str
    starting_cash: Decimal
    cash: Decimal
    positions: tuple[BookPosition, ...] = ()
    realized_pnl: Decimal = Decimal("0")
    fill_count: int = Field(default=0, ge=0)

    def held(self) -> dict[str, BookPosition]:
        return {position.symbol: position for position in self.positions}

    def market_value(self, prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for position in self.positions:
            price = prices.get(position.symbol)
            if price is None:
                raise ValueError(f"cannot value book {self.name}: no price for {position.symbol}")
            total += position.qty * price
        return total

    def equity(self, prices: dict[str, Decimal]) -> Decimal:
        return self.cash + self.market_value(prices)

    def account(self, prices: dict[str, Decimal]) -> Account:
        """Present the book as an `Account` so the same risk engine can evaluate it.

        Buying power is cash: a book has no margin, which matches the paper account's own
        long-only, unlevered configuration.
        """
        return Account(
            equity=self.equity(prices),
            cash=self.cash,
            buying_power=self.cash,
            trading_blocked=False,
            shorting_enabled=False,
            options_level=0,
            multiplier=Decimal("1"),
        )

    def broker_positions(self, prices: dict[str, Decimal]) -> tuple[Position, ...]:
        return tuple(
            Position(
                symbol=position.symbol,
                qty=position.qty,
                market_value=position.qty * prices[position.symbol],
                current_price=prices[position.symbol],
            )
            for position in self.positions
        )


class SimulatedFillResult(BookModel):
    """One settlement attempt against a book, filled or explicitly not."""

    proposal_id: str
    symbol: str
    side: Literal["buy", "sell"]
    outcome: FillOutcome
    qty: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    quote_bid: Decimal | None = None
    quote_ask: Decimal | None = None
    quote_at: datetime | None = None
    detail: str = ""

    @property
    def filled(self) -> bool:
        return self.outcome == "FILLED"

    def cash_delta(self) -> Decimal:
        """Signed cash effect of this fill, negative for a purchase."""
        if not self.filled:
            return Decimal("0")
        gross = self.qty * self.price
        return -(gross + self.commission) if self.side == "buy" else gross - self.commission

    def summary(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "symbol": self.symbol,
            "side": self.side,
            "outcome": self.outcome,
            "qty": str(self.qty),
            "price": str(self.price),
            "detail": self.detail,
        }


class ReferencePointSummary(BookModel):
    """Compact result of one deterministic comparison curve at a book cutoff."""

    kind: Literal["CASH", "SPY_BUY_HOLD"]
    status: Literal["COMPLETED", "FAILED"]
    equity: Decimal | None = None
    cash: Decimal | None = None
    error: str | None = None

    def summary(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "equity": None if self.equity is None else str(self.equity),
            "cash": None if self.cash is None else str(self.cash),
            "error": self.error,
        }


class BookRunSummary(BookModel):
    """What one book did in one run."""

    book_id: str
    name: str
    run_id: str
    invocation_id: str | None = None
    process_profile: str = "single_pass"
    experiment_phase_id: str | None = None
    evaluation_id: str | None = None
    invocation_trail: tuple[str, ...] = ()
    decision_status: str
    abstention_classification: str | None = None
    dissent_disposition_count: int = Field(default=0, ge=0)
    proposal_count: int = Field(default=0, ge=0)
    approved_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    filled_count: int = Field(default=0, ge=0)
    unfilled_outcomes: tuple[str, ...] = ()
    equity: Decimal
    cash: Decimal
    position_count: int = Field(default=0, ge=0)
    references: tuple[ReferencePointSummary, ...] = ()

    def summary(self) -> dict[str, object]:
        return {
            "book_id": self.book_id,
            "name": self.name,
            "run_id": self.run_id,
            "invocation_id": self.invocation_id,
            "process_profile": self.process_profile,
            "experiment_phase_id": self.experiment_phase_id,
            "evaluation_id": self.evaluation_id,
            "invocation_trail": list(self.invocation_trail),
            "decision_status": self.decision_status,
            "abstention_classification": self.abstention_classification,
            "dissent_disposition_count": self.dissent_disposition_count,
            "proposal_count": self.proposal_count,
            "approved_count": self.approved_count,
            "rejected_count": self.rejected_count,
            "filled_count": self.filled_count,
            "unfilled_outcomes": list(self.unfilled_outcomes),
            "equity": str(self.equity),
            "cash": str(self.cash),
            "position_count": self.position_count,
            "references": [item.summary() for item in self.references],
        }
