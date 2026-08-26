"""Context and output contract for the weekly strategist.

The strategist reviews a period that has already happened. It receives no broker, no candidate
slate, and no research: only the strategy version it is reviewing, the human-owned portfolio
policy, the audited decisions, and the derived performance record. Its output is a *proposal*
anchored to text that actually exists in the current document; applying it is a separate,
human-initiated act.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from trader.agent.config import AgentRoleConfig, ContextSource
from trader.agent.invocation import verify_context_sources
from trader.ledger.history import load_recent_decisions
from trader.ledger.models import RecentRunDecision, StrategyVersion, WeeklyPerformance
from trader.ledger.performance import load_weekly_performance

WEEKLY_CONTEXT_SOURCES: dict[ContextSource, tuple[str, ...]] = {
    "strategy_current": ("strategy", "strategy_version"),
    "portfolio_policy": ("portfolio_policy",),
    "recent_decisions": ("recent_decisions",),
    "weekly_performance": ("performance",),
}

MAX_REVIEW_RUNS = 10
MAX_CONTROL_SAFE_CHARS = 20_000


class WeeklyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeeklyAgentContext(WeeklyModel):
    schema_version: Literal[1] = 1
    mode: Literal["paper_proposal"] = "paper_proposal"
    run_id: str
    as_of: datetime
    period_start: datetime
    period_end: datetime
    strategy: str = Field(min_length=1)
    strategy_version: StrategyVersion
    portfolio_policy: str = Field(min_length=1)
    recent_decisions: tuple[RecentRunDecision, ...]
    performance: WeeklyPerformance

    @field_validator("as_of", "period_start", "period_end")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("weekly context timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def coherent_period(self) -> "WeeklyAgentContext":
        if self.period_end > self.as_of:
            raise ValueError("a review period cannot extend past the review cutoff")
        if self.performance.period_start != self.period_start:
            raise ValueError("performance record does not cover the declared period start")
        if self.performance.period_end != self.period_end:
            raise ValueError("performance record does not cover the declared period end")
        for record in self.recent_decisions:
            if record.scheduled_for > self.as_of:
                raise ValueError("recent decisions cannot postdate the review cutoff")
        return self


class ProposedStrategyChange(WeeklyModel):
    """One anchored, revertable edit to the strategy document.

    ``current_text`` must appear exactly once in the supplied document. That is what makes the
    proposal reviewable as a diff and applicable without guessing where it belongs.
    """

    section_heading: str = Field(min_length=1, max_length=200)
    current_text: str = Field(min_length=1, max_length=10_000)
    replacement_text: str = Field(min_length=1, max_length=10_000)
    hypothesis: str = Field(min_length=1, max_length=5_000)
    disconfirming_evidence: str = Field(min_length=1, max_length=5_000)
    expected_effect: str = Field(min_length=1, max_length=5_000)
    failure_modes: tuple[str, ...] = Field(default=(), max_length=8)
    evaluation_plan: str = Field(min_length=1, max_length=5_000)
    revert_criteria: str = Field(min_length=1, max_length=5_000)

    @field_validator(
        "section_heading",
        "current_text",
        "replacement_text",
        "hypothesis",
        "disconfirming_evidence",
        "expected_effect",
        "evaluation_plan",
        "revert_criteria",
    )
    @classmethod
    def safe_text(cls, value: str) -> str:
        return _safe_text(value)

    @field_validator("failure_modes")
    @classmethod
    def safe_failure_modes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_safe_text(value) for value in values)

    @model_validator(mode="after")
    def actually_changes_something(self) -> "ProposedStrategyChange":
        if self.current_text.strip() == self.replacement_text.strip():
            raise ValueError("a proposed change must differ from the text it replaces")
        return self


class StrategyRecommendation(WeeklyModel):
    schema_version: Literal[1] = 1
    status: Literal["NO_CHANGE", "PROPOSE_CHANGE"]
    diagnosis: str = Field(min_length=1, max_length=10_000)
    process_assessment: str = Field(min_length=1, max_length=10_000)
    cited_run_ids: tuple[str, ...] = Field(default=(), max_length=20)
    cited_proposal_ids: tuple[str, ...] = Field(default=(), max_length=40)
    cited_thesis_ids: tuple[str, ...] = Field(default=(), max_length=40)
    no_change_reason: str | None = Field(default=None, max_length=5_000)
    # One atomic edit per review, so a change can be approved, measured, and reverted on its own.
    proposed_changes: tuple[ProposedStrategyChange, ...] = Field(default=(), max_length=1)

    @field_validator("diagnosis", "process_assessment")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return _safe_text(value)

    @model_validator(mode="after")
    def coherent_status(self) -> "StrategyRecommendation":
        if self.status == "NO_CHANGE":
            if self.proposed_changes:
                raise ValueError("NO_CHANGE cannot carry a proposed change")
            if not (self.no_change_reason or "").strip():
                raise ValueError("NO_CHANGE requires a reason")
        else:
            if not self.proposed_changes:
                raise ValueError("PROPOSE_CHANGE requires a proposed change")
            if (self.no_change_reason or "").strip():
                raise ValueError("PROPOSE_CHANGE cannot carry a no_change_reason")
        return self


def assemble_weekly_context(
    session: Session,
    *,
    run_id: str,
    as_of: datetime,
    period_start: datetime,
    period_end: datetime,
    strategy: str,
    strategy_version: StrategyVersion,
    portfolio_policy: str,
    role: AgentRoleConfig,
) -> WeeklyAgentContext:
    """Build the review context from persisted records only, bounded by the role's limits."""
    verify_weekly_context_sources(role)
    context = WeeklyAgentContext(
        run_id=run_id,
        as_of=as_of,
        period_start=period_start,
        period_end=period_end,
        strategy=strategy.strip(),
        strategy_version=strategy_version,
        portfolio_policy=portfolio_policy.strip(),
        # Cut off at the period end, not the review time: a run that happened after the reviewed
        # week would let the strategist grade a decision using an outcome it could not have known.
        recent_decisions=load_recent_decisions(
            session,
            exclude_run_id=run_id,
            as_of=period_end,
            max_runs=MAX_REVIEW_RUNS,
        ),
        performance=load_weekly_performance(
            session,
            period_start=period_start,
            period_end=period_end,
        ),
    )
    size = len(context.model_dump_json())
    if size > role.max_context_chars:
        raise ValueError(
            f"assembled weekly context has {size} chars; role limit is {role.max_context_chars}"
        )
    return context


def verify_weekly_context_sources(role: AgentRoleConfig) -> None:
    """Fail closed when the strategist declares a source the weekly assembler cannot supply."""
    verify_context_sources(
        role,
        supported=WEEKLY_CONTEXT_SOURCES,
        context_model=WeeklyAgentContext,
        label="weekly context",
    )


def validate_strategy_recommendation(
    recommendation: StrategyRecommendation,
    context: WeeklyAgentContext,
) -> StrategyRecommendation:
    """Reject invented citations, unanchored patches, and reviews of an empty record."""
    if not context.performance.has_sample() and recommendation.status == "PROPOSE_CHANGE":
        raise ValueError("a strategy change cannot be proposed for a period with no decisions")

    admitted_runs = {record.run_id for record in context.recent_decisions}
    admitted_proposals = {
        outcome.proposal_id
        for record in context.recent_decisions
        for outcome in record.proposals
    }
    admitted_theses = {outcome.thesis_id for outcome in context.performance.thesis_outcomes}
    for run_id in recommendation.cited_run_ids:
        if run_id not in admitted_runs:
            raise ValueError(f"review cites a run outside the supplied context: {run_id}")
    for proposal_id in recommendation.cited_proposal_ids:
        if proposal_id not in admitted_proposals:
            raise ValueError(f"review cites a proposal outside the supplied context: {proposal_id}")
    for thesis_id in recommendation.cited_thesis_ids:
        if thesis_id not in admitted_theses:
            raise ValueError(f"review cites a thesis outside the supplied context: {thesis_id}")

    for change in recommendation.proposed_changes:
        occurrences = context.strategy.count(change.current_text)
        if occurrences == 0:
            raise ValueError(
                "proposed change is not anchored to text in the current strategy document"
            )
        if occurrences > 1:
            raise ValueError(
                "proposed change anchors to text that appears more than once; it is ambiguous"
            )
        if change.current_text in context.portfolio_policy:
            raise ValueError("the strategist may not propose edits to the portfolio policy")
    return recommendation


def apply_anchored_change(document: str, *, current_text: str, replacement_text: str) -> str:
    """Return the document with one anchored change applied, refusing anything ambiguous.

    This is called only by a human-initiated approval, never by a run. The document may have moved
    on since the proposal was written, so the anchor is re-checked here rather than trusted.
    """
    if not current_text.strip() or not replacement_text.strip():
        raise ValueError("an applied change needs both anchor and replacement text")
    occurrences = document.count(current_text)
    if occurrences == 0:
        raise ValueError("the strategy document no longer contains the approved anchor text")
    if occurrences > 1:
        raise ValueError("the approved anchor text is no longer unique in the strategy document")
    return document.replace(current_text, replacement_text, 1)


def _safe_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("strategy review text cannot be blank")
    if len(normalized) > MAX_CONTROL_SAFE_CHARS:
        raise ValueError("strategy review text exceeds the permitted length")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in normalized):
        raise ValueError("strategy review text cannot contain control characters")
    return normalized
