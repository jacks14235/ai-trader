"""Typed ledger records shared by the writers and by reasoning context assembly."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.agent.models import SYMBOL_PATTERN

ThesisStatus = Literal["active", "closed"]
"""Automatic transitions only ever open or close a thesis; humans own richer states."""

ProposalAction = Literal["BUY", "SELL", "HOLD"]


class LedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*")
    @classmethod
    def aware_timestamps(cls, value: object) -> object:
        """Treat persisted naive timestamps as UTC so records stay comparable."""
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            return value.replace(tzinfo=UTC)
        return value


class OpenThesis(LedgerModel):
    """One currently held thesis, supplied to a reasoning role as prior belief."""

    thesis_id: str = Field(min_length=1, max_length=128)
    symbol: str
    title: str = Field(min_length=1, max_length=500)
    summary: str
    confidence: float = Field(ge=0, le=1)
    invalidation_conditions: tuple[str, ...] = ()
    opened_at: datetime
    updated_at: datetime

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("thesis symbol must be a valid uppercase equity symbol")
        return normalized


class DecisionOutcome(LedgerModel):
    """A prior proposal together with what deterministic authorization did to it."""

    proposal_id: str = Field(min_length=1, max_length=128)
    symbol: str
    action: ProposalAction
    target_notional_usd: Decimal | None = None
    target_position_pct: Decimal | None = None
    confidence: float = Field(ge=0, le=1)
    rationale: str
    risk_approved: bool | None = None
    risk_rejection_codes: tuple[str, ...] = ()
    order_status: str | None = None
    filled_qty: Decimal | None = None


class RecentRunDecision(LedgerModel):
    """The decision a single prior run reached, including an explicit no-action outcome."""

    run_id: str = Field(min_length=1, max_length=128)
    run_key: str = Field(min_length=1, max_length=200)
    scheduled_for: datetime
    decision_summary: str | None = None
    proposals: tuple[DecisionOutcome, ...] = ()


class PriorWaitTrigger(LedgerModel):
    """A trigger copied from the latest completed abstention for one book."""

    trigger_id: str = Field(min_length=1, max_length=64)
    kind: Literal["PRICE", "EVIDENCE", "EVENT"]
    description: str
    symbol: str | None = None
    comparison: Literal["AT_OR_BELOW", "AT_OR_ABOVE"] | None = None
    target_price: Decimal | None = None
    evidence_needed: str | None = None
    event: str | None = None


class PriorWaitDecision(LedgerModel):
    """Latest completed no-action decision before deterministic trigger assessment."""

    decision_id: str
    invocation_id: str
    run_id: str
    scheduled_for: datetime
    classification: Literal["DELIBERATE_WAIT", "DATA_UNAVAILABLE"]
    insufficient_evidence: str
    unavailable_data: tuple[str, ...] = ()
    triggers: tuple[PriorWaitTrigger, ...]
    reconsider_at: datetime | None = None
    reconsider_on: str | None = None
    scope_symbols: tuple[str, ...] = ()
    prior_evidence_content_hashes: tuple[str, ...] = ()


class WaitTriggerAssessment(LedgerModel):
    """Machine-checkable status of one prior wait trigger at the current cutoff."""

    trigger_id: str
    kind: Literal["PRICE", "EVIDENCE", "EVENT"]
    description: str
    status: Literal["SATISFIED", "UNSATISFIED", "UNRESOLVED"]
    reason: str
    symbol: str | None = None
    current_price: Decimal | None = None
    comparison: Literal["AT_OR_BELOW", "AT_OR_ABOVE"] | None = None
    target_price: Decimal | None = None


class WaitingDecisionMemory(LedgerModel):
    """A prior abstention plus the exact changes that may justify reopening it."""

    decision_id: str
    invocation_id: str
    run_id: str
    scheduled_for: datetime
    classification: Literal["DELIBERATE_WAIT", "DATA_UNAVAILABLE"]
    insufficient_evidence: str
    unavailable_data: tuple[str, ...] = ()
    reconsider_at: datetime | None = None
    reconsider_on: str | None = None
    scope_symbols: tuple[str, ...] = ()
    review_due: bool
    new_evidence_ids: tuple[str, ...] = ()
    trigger_assessments: tuple[WaitTriggerAssessment, ...]
    reopenable: bool


class PerformanceMetrics(LedgerModel):
    """Equity-curve metrics for one run, computed from persisted snapshots only."""

    equity: Decimal
    cash: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal
    pnl: Decimal | None = None
    return_pct: Decimal | None = None

    def raw_payload(self) -> dict[str, str]:
        return {
            "cash": str(self.cash),
            "drawdown_pct": str(self.drawdown_pct),
            "equity": str(self.equity),
            "peak_equity": str(self.peak_equity),
            "pnl": "" if self.pnl is None else str(self.pnl),
            "return_pct": "" if self.return_pct is None else str(self.return_pct),
        }


class StrategyVersion(LedgerModel):
    """One recorded version of the strategy document, so a review names what it reviewed."""

    strategy_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["active", "superseded"]
    created_at: datetime
    markdown_path: str | None = None


class ThesisOutcome(LedgerModel):
    """What a thesis actually cost and returned, derived only from broker-reported fills.

    ``realized_pnl`` uses average entry cost against the quantity actually sold, so a partially
    exited thesis reports the realized part only and leaves the rest in ``open_qty``. It is
    ``None`` when nothing was ever bought, because there is then no cost basis to measure against.
    """

    thesis_id: str = Field(min_length=1, max_length=128)
    symbol: str
    title: str = Field(min_length=1, max_length=500)
    status: ThesisStatus
    closure: str | None = None
    confidence: float = Field(ge=0, le=1)
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    holding_days: int | None = Field(default=None, ge=0)
    proposal_count: int = Field(default=0, ge=0)
    fill_count: int = Field(default=0, ge=0)
    buy_qty: Decimal = Decimal("0")
    buy_notional: Decimal = Decimal("0")
    sell_qty: Decimal = Decimal("0")
    sell_notional: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    open_qty: Decimal = Decimal("0")
    realized_pnl: Decimal | None = None

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("thesis symbol must be a valid uppercase equity symbol")
        return normalized


class EquityPoint(LedgerModel):
    captured_at: datetime
    equity: Decimal
    drawdown_pct: Decimal | None = None


class RejectionTally(LedgerModel):
    code: str = Field(min_length=1, max_length=100)
    count: int = Field(ge=1)


class WeeklyPerformance(LedgerModel):
    """One review period aggregated from persisted snapshots, decisions, and fills.

    Every field is derived; nothing here is a model's opinion. An empty period is represented
    honestly with zero counts rather than omitted, so a reviewer can see it had no sample.
    """

    period_start: datetime
    period_end: datetime
    run_count: int = Field(default=0, ge=0)
    equity_points: tuple[EquityPoint, ...] = ()
    starting_equity: Decimal | None = None
    ending_equity: Decimal | None = None
    pnl: Decimal | None = None
    return_pct: Decimal | None = None
    peak_equity: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    proposal_count: int = Field(default=0, ge=0)
    approved_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    rejection_codes: tuple[RejectionTally, ...] = ()
    no_action_run_count: int = Field(default=0, ge=0)
    submitted_order_count: int = Field(default=0, ge=0)
    filled_order_count: int = Field(default=0, ge=0)
    theses_opened: int = Field(default=0, ge=0)
    theses_closed: int = Field(default=0, ge=0)
    thesis_outcomes: tuple[ThesisOutcome, ...] = ()
    strategy_versions: tuple[StrategyVersion, ...] = ()

    @model_validator(mode="after")
    def ordered_period(self) -> "WeeklyPerformance":
        if self.period_end <= self.period_start:
            raise ValueError("review period must end after it starts")
        return self

    def has_sample(self) -> bool:
        """Whether the period contains anything a reviewer could reason about."""
        return bool(self.run_count or self.proposal_count or self.thesis_outcomes)


class LedgerWriteSummary(LedgerModel):
    """What one run's ledger stage changed, for the run report and audit trail."""

    opened_thesis_ids: tuple[str, ...] = ()
    updated_thesis_ids: tuple[str, ...] = ()
    closed_thesis_ids: tuple[str, ...] = ()
    linked_evidence_count: int = Field(default=0, ge=0)
    unlinked_evidence_count: int = Field(default=0, ge=0)

    def summary(self) -> dict[str, object]:
        return {
            "opened_theses": list(self.opened_thesis_ids),
            "updated_theses": list(self.updated_thesis_ids),
            "closed_theses": list(self.closed_thesis_ids),
            "linked_evidence_count": self.linked_evidence_count,
            "unlinked_evidence_count": self.unlinked_evidence_count,
        }
