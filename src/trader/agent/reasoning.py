"""Deterministic context assembly and validated paper-proposal contracts."""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.config import AgentRoleConfig, ContextSource
from trader.agent.invocation import verify_context_sources
from trader.agent.models import SYMBOL_PATTERN, TradeProposal
from trader.agent.packets import LocalIdentifier, NamedPacket
from trader.broker.models import Account, BrokerOrder, Position
from trader.ledger.history import load_open_theses, load_recent_decisions
from trader.ledger.models import OpenThesis, RecentRunDecision, WaitingDecisionMemory
from trader.persistence.models import ResearchItem, ResearchItemSymbol
from trader.research.service import ResearchRunResult
from trader.universe.models import UniverseScan

TEMPLATE_TOKEN_MARKER = "__DAILY_UPDATE_"
MAX_DISSENT_DISPOSITIONS = 40

DAILY_CONTEXT_SOURCES: dict[ContextSource, tuple[str, ...]] = {
    "strategy_current": ("strategy",),
    "portfolio_policy": ("portfolio_policy",),
    "account_snapshot": ("account",),
    "positions": ("positions",),
    "open_orders": ("open_orders",),
    "candidate_overview": ("candidates",),
    "deep_research": ("evidence_catalog", "deep_evidence"),
    "recent_decisions": ("recent_decisions",),
    "open_theses": ("open_theses",),
}
"""The context fields that satisfy each source the daily assembler is able to supply.

A declared source absent from this mapping is a configuration error rather than an empty section,
which is what previously allowed ``recent_decisions`` to be required and never delivered.
"""


class ReasoningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateContext(ReasoningModel):
    symbol: str
    score: int
    signals: tuple[dict[str, object], ...]
    evidence_ids: tuple[str, ...]


class EvidenceCatalogItem(ReasoningModel):
    research_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbols: tuple[str, ...]
    source_tier: str
    source_type: str
    provider: str
    headline: str
    published_at: datetime
    retrieved_at: datetime
    summary: str


class DeepEvidenceItem(ReasoningModel):
    research_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str
    raw_artifact_path: str


class DailyAgentContext(ReasoningModel):
    schema_version: Literal[1] = 1
    mode: Literal["paper_proposal"] = "paper_proposal"
    run_id: str
    as_of: datetime
    strategy: str
    portfolio_policy: str
    account: Account
    positions: tuple[Position, ...]
    open_orders: tuple[BrokerOrder, ...]
    candidates: tuple[CandidateContext, ...]
    evidence_catalog: tuple[EvidenceCatalogItem, ...]
    deep_evidence: tuple[DeepEvidenceItem, ...]
    admitted_evidence_ids: tuple[str, ...]
    recent_decisions: tuple[RecentRunDecision, ...]
    open_theses: tuple[OpenThesis, ...]

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("agent context cutoff must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def decisions_precede_cutoff(self) -> "DailyAgentContext":
        for record in self.recent_decisions:
            if record.scheduled_for > self.as_of:
                raise ValueError("recent decisions cannot postdate the context cutoff")
        return self


class DailyUpdateSection(ReasoningModel):
    """An optional extra teaching block the daily trader may invent."""

    heading: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=8_000)

    @field_validator("heading", "body")
    @classmethod
    def briefing_safe_text(cls, value: str) -> str:
        return _briefing_text(value)


class DailyUpdateGlossaryEntry(ReasoningModel):
    term: str = Field(min_length=1, max_length=80)
    definition: str = Field(min_length=1, max_length=1_000)

    @field_validator("term", "definition")
    @classmethod
    def briefing_safe_text(cls, value: str) -> str:
        return _briefing_text(value)


class DailyUpdateChart(ReasoningModel):
    """A small chart drawn only from numbers the model places in structured output."""

    title: str = Field(min_length=1, max_length=120)
    kind: Literal["bar", "line"]
    caption: str = Field(min_length=1, max_length=2_000)
    labels: tuple[str, ...] = Field(min_length=1, max_length=20)
    values: tuple[float, ...] = Field(min_length=1, max_length=20)

    @field_validator("title", "caption")
    @classmethod
    def briefing_safe_text(cls, value: str) -> str:
        return _briefing_text(value)

    @field_validator("labels")
    @classmethod
    def briefing_safe_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        labels = tuple(_briefing_text(label) for label in values)
        if any(not label for label in labels):
            raise ValueError("chart labels must be nonempty")
        return labels

    @field_validator("values")
    @classmethod
    def finite_chart_values(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        if any(not isfinite(value) for value in values):
            raise ValueError("chart values must be finite")
        return values

    @model_validator(mode="after")
    def matching_series(self) -> "DailyUpdateChart":
        if len(self.labels) != len(self.values):
            raise ValueError("chart labels and values must have the same length")
        return self


class DailyUpdate(ReasoningModel):
    """Beginner-facing briefing the daily trader writes alongside its trade decision."""

    headline: str = Field(min_length=1, max_length=200)
    lesson_title: str = Field(min_length=1, max_length=120)
    lesson: str = Field(min_length=1, max_length=4_000)
    overview: str = Field(min_length=1, max_length=8_000)
    next_day_plan: str = Field(min_length=1, max_length=4_000)
    sections: tuple[DailyUpdateSection, ...] = Field(default=(), max_length=8)
    glossary: tuple[DailyUpdateGlossaryEntry, ...] = Field(default=(), max_length=12)
    charts: tuple[DailyUpdateChart, ...] = Field(default=(), max_length=4)

    @field_validator("headline", "lesson_title", "lesson", "overview", "next_day_plan")
    @classmethod
    def briefing_safe_text(cls, value: str) -> str:
        return _briefing_text(value)


class WaitTrigger(ReasoningModel):
    """One falsifiable condition that can make waiting worth reconsidering."""

    trigger_id: LocalIdentifier
    kind: Literal["PRICE", "EVIDENCE", "EVENT"]
    description: str = Field(min_length=1, max_length=2_000)
    symbol: str | None = None
    comparison: Literal["AT_OR_BELOW", "AT_OR_ABOVE"] | None = None
    target_price: Decimal | None = None
    evidence_needed: str | None = Field(default=None, min_length=1, max_length=2_000)
    event: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("symbol")
    @classmethod
    def normalized_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("PRICE trigger symbol must be a valid uppercase equity symbol")
        return normalized

    @field_validator("description", "evidence_needed", "event")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        return None if value is None else _briefing_text(value)

    @model_validator(mode="after")
    def fields_match_kind(self) -> "WaitTrigger":
        if self.kind == "PRICE":
            if self.symbol is None or self.comparison is None or self.target_price is None:
                raise ValueError("PRICE trigger requires symbol, comparison, and target_price")
            if not self.target_price.is_finite() or self.target_price <= 0:
                raise ValueError("PRICE trigger target_price must be positive and finite")
            if self.evidence_needed is not None or self.event is not None:
                raise ValueError("PRICE trigger cannot include evidence_needed or event")
        elif self.kind == "EVIDENCE":
            if self.evidence_needed is None:
                raise ValueError("EVIDENCE trigger requires evidence_needed")
            if any(
                value is not None
                for value in (self.symbol, self.comparison, self.target_price, self.event)
            ):
                raise ValueError("EVIDENCE trigger contains fields for another trigger kind")
        else:
            if self.event is None:
                raise ValueError("EVENT trigger requires event")
            if any(
                value is not None
                for value in (
                    self.symbol,
                    self.comparison,
                    self.target_price,
                    self.evidence_needed,
                )
            ):
                raise ValueError("EVENT trigger contains fields for another trigger kind")
        return self


class AbstentionRecord(ReasoningModel):
    """A deliberate, testable explanation for reaching no-action."""

    classification: Literal["DELIBERATE_WAIT", "DATA_UNAVAILABLE"]
    insufficient_evidence: str = Field(min_length=1, max_length=5_000)
    unavailable_data: tuple[str, ...] = Field(default=(), max_length=20)
    triggers: tuple[WaitTrigger, ...] = Field(min_length=1, max_length=10)
    reconsider_at: datetime | None = None
    reconsider_on: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("insufficient_evidence", "reconsider_on")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        return None if value is None else _briefing_text(value)

    @field_validator("unavailable_data")
    @classmethod
    def clean_unavailable_data(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_briefing_text(value) for value in values)
        if any(len(value) > 1_000 for value in cleaned):
            raise ValueError("unavailable_data entries cannot exceed 1000 characters")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("unavailable_data entries must be unique")
        return cleaned

    @field_validator("reconsider_at")
    @classmethod
    def aware_reconsideration(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("reconsider_at must be timezone-aware")
        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def complete_abstention(self) -> "AbstentionRecord":
        if (self.reconsider_at is None) == (self.reconsider_on is None):
            raise ValueError("abstention requires exactly one of reconsider_at or reconsider_on")
        if self.classification == "DATA_UNAVAILABLE" and not self.unavailable_data:
            raise ValueError("DATA_UNAVAILABLE requires unavailable_data")
        if self.classification == "DELIBERATE_WAIT" and self.unavailable_data:
            raise ValueError("DELIBERATE_WAIT cannot claim unavailable_data")
        trigger_ids = [trigger.trigger_id for trigger in self.triggers]
        if len(trigger_ids) != len(set(trigger_ids)):
            raise ValueError("abstention trigger IDs must be unique")
        return self


class DissentDisposition(ReasoningModel):
    """The manager's explicit treatment of one consumed contradiction or dissent claim."""

    packet_step: LocalIdentifier
    claim_id: LocalIdentifier
    resolution: Literal["ACCEPTED", "REJECTED", "DEFERRED"]
    rationale: str = Field(min_length=1, max_length=3_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    defer_until: WaitTrigger | None = None

    @field_validator("rationale")
    @classmethod
    def clean_rationale(cls, value: str) -> str:
        return _briefing_text(value)

    @field_validator("evidence_ids")
    @classmethod
    def exact_unique_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("dissent disposition evidence IDs must be unique")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("dissent disposition evidence IDs must be lowercase SHA-256 values")
        return values

    @model_validator(mode="after")
    def coherent_resolution(self) -> "DissentDisposition":
        if self.resolution == "DEFERRED" and self.defer_until is None:
            raise ValueError("DEFERRED dissent requires a named trigger")
        if self.resolution != "DEFERRED" and self.defer_until is not None:
            raise ValueError("only DEFERRED dissent may include defer_until")
        return self


class WaitReconsideration(ReasoningModel):
    """The exact machine-observed change used to reopen a prior abstention."""

    prior_decision_id: str = Field(min_length=1, max_length=128)
    trigger_ids: tuple[LocalIdentifier, ...] = Field(default=(), max_length=10)
    new_evidence_ids: tuple[str, ...] = Field(default=(), max_length=40)
    rationale: str = Field(min_length=1, max_length=3_000)

    @field_validator("trigger_ids", "new_evidence_ids")
    @classmethod
    def unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("wait reconsideration IDs must be unique")
        return values

    @field_validator("new_evidence_ids")
    @classmethod
    def exact_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("wait reconsideration evidence IDs must be lowercase SHA-256 values")
        return values

    @field_validator("rationale")
    @classmethod
    def clean_rationale(cls, value: str) -> str:
        return _briefing_text(value)


class DailyDecision(ReasoningModel):
    schema_version: Literal[3] = 3
    status: Literal["NO_ACTION", "PROPOSE_TRADES"]
    market_assessment: str = Field(min_length=1, max_length=10_000)
    strongest_counterargument: str = Field(min_length=1, max_length=5_000)
    abstention: AbstentionRecord | None = None
    dissent_dispositions: tuple[DissentDisposition, ...] = Field(
        default=(), max_length=MAX_DISSENT_DISPOSITIONS
    )
    wait_reconsiderations: tuple[WaitReconsideration, ...] = Field(default=(), max_length=1)
    proposals: tuple[TradeProposal, ...] = Field(default=(), max_length=10)
    watchlist: tuple[str, ...] = Field(default=(), max_length=20)
    daily_update: DailyUpdate

    @field_validator("watchlist")
    @classmethod
    def normalized_watchlist(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.upper().strip() for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("watchlist symbols must be unique")
        if any(not SYMBOL_PATTERN.fullmatch(value) for value in normalized):
            raise ValueError("watchlist contains an invalid symbol")
        return normalized

    @model_validator(mode="after")
    def coherent_status(self) -> "DailyDecision":
        if self.status == "NO_ACTION":
            if self.proposals or self.abstention is None:
                raise ValueError(
                    "NO_ACTION requires structured abstention and cannot contain proposals"
                )
        elif not self.proposals or self.abstention is not None:
            raise ValueError("PROPOSE_TRADES requires proposals and no abstention")
        if any(proposal.action == "HOLD" for proposal in self.proposals):
            raise ValueError("trade proposals may only BUY or SELL")
        if self.status == "NO_ACTION" and self.wait_reconsiderations:
            raise ValueError("NO_ACTION cannot claim that a prior wait was reopened")
        dispositions = [(item.packet_step, item.claim_id) for item in self.dissent_dispositions]
        if len(dispositions) != len(set(dispositions)):
            raise ValueError("each packet claim may have only one dissent disposition")
        return self

    @property
    def no_action_reason(self) -> str | None:
        """Compatibility projection for human-readable reports."""
        return None if self.abstention is None else self.abstention.insufficient_evidence


def assemble_daily_context(
    session: Session,
    *,
    run_id: str,
    as_of: datetime,
    strategy: str,
    portfolio_policy: str,
    account: Account,
    positions: tuple[Position, ...],
    open_orders: tuple[BrokerOrder, ...],
    scan: UniverseScan,
    research: ResearchRunResult,
    role: AgentRoleConfig,
    book_id: str | None = None,
) -> DailyAgentContext:
    """Build one bounded context from registered sources and exact persisted evidence.

    ``book_id`` scopes the memory to one decision line. A simulated book recalls only its own
    prior proposals and carries no theses, because the thesis ledger belongs to the live
    portfolio; lending a variant the incumbent's memory would make the two indistinguishable.
    """
    verify_daily_context_sources(role)
    admitted_ids = tuple(research.persisted_research_ids)
    if not admitted_ids:
        raise ValueError("daily reasoning requires persisted research evidence")
    items = list(
        session.scalars(
            select(ResearchItem)
            .where(ResearchItem.run_id == run_id, ResearchItem.id.in_(admitted_ids))
            .order_by(ResearchItem.id)
        )
    )
    by_id = {item.id: item for item in items}
    missing = [research_id for research_id in admitted_ids if research_id not in by_id]
    if missing:
        raise LookupError("persisted research records are missing: " + ", ".join(missing))
    symbol_links = list(
        session.scalars(
            select(ResearchItemSymbol)
            .where(ResearchItemSymbol.research_id.in_(admitted_ids))
            .order_by(ResearchItemSymbol.research_id, ResearchItemSymbol.symbol)
        )
    )
    symbols_by_id: dict[str, list[str]] = defaultdict(list)
    ids_by_symbol: dict[str, list[str]] = defaultdict(list)
    for link in symbol_links:
        symbols_by_id[link.research_id].append(link.symbol)
        ids_by_symbol[link.symbol].append(link.research_id)

    catalog = tuple(
        EvidenceCatalogItem(
            research_id=item.id,
            symbols=tuple(symbols_by_id[item.id]),
            source_tier=item.source_tier,
            source_type=item.source_type,
            provider=item.provider or "legacy",
            headline=_truncate(item.headline, 400),
            published_at=_utc(item.published_at),
            retrieved_at=_utc(item.retrieved_at),
            summary=_truncate(item.normalized_summary, 500),
        )
        for item in items
    )
    deep_ids = {
        research_id
        for symbol in research.plan.deep_symbols
        for research_id in ids_by_symbol.get(symbol, ())
    }
    deep_skeleton = tuple(
        DeepEvidenceItem(
            research_id=catalog_item.research_id,
            excerpt="",
            raw_artifact_path=by_id[catalog_item.research_id].raw_artifact_path or "",
        )
        for catalog_item in catalog
        if catalog_item.research_id in deep_ids
    )
    candidates = tuple(
        CandidateContext(
            symbol=candidate.symbol,
            score=candidate.score,
            signals=tuple(signal.model_dump(mode="json") for signal in candidate.signals),
            evidence_ids=tuple(sorted(set(ids_by_symbol.get(candidate.symbol, ())))),
        )
        for candidate in scan.candidates
    )
    recent_decisions = load_recent_decisions(
        session,
        exclude_run_id=run_id,
        as_of=as_of,
        book_id=book_id,
    )
    open_theses = (
        ()
        if book_id is not None
        else load_open_theses(
            session,
            symbols=frozenset(
                {candidate.symbol for candidate in scan.candidates}
                | {position.symbol for position in positions}
            ),
        )
    )

    def build(deep: tuple[DeepEvidenceItem, ...]) -> DailyAgentContext:
        return DailyAgentContext(
            run_id=run_id,
            as_of=as_of,
            strategy=strategy.strip(),
            portfolio_policy=portfolio_policy.strip(),
            account=account,
            positions=positions,
            open_orders=open_orders,
            candidates=candidates,
            evidence_catalog=catalog,
            deep_evidence=deep,
            admitted_evidence_ids=tuple(item.research_id for item in catalog),
            recent_decisions=recent_decisions,
            open_theses=open_theses,
        )

    skeleton_context = build(deep_skeleton)
    skeleton_chars = len(skeleton_context.model_dump_json())
    if skeleton_chars > role.max_context_chars:
        raise ValueError(
            f"daily evidence catalog has {skeleton_chars} chars before excerpts; "
            f"role limit is {role.max_context_chars}"
        )
    detail_budget = role.max_document_chars
    if deep_skeleton:
        remaining = role.max_context_chars - skeleton_chars - 1_000
        detail_budget = min(role.max_document_chars, max(100, remaining // len(deep_skeleton)))

    def excerpts(limit: int) -> tuple[DeepEvidenceItem, ...]:
        return tuple(
            item.model_copy(
                update={
                    "excerpt": _truncate(by_id[item.research_id].normalized_text, limit)
                }
            )
            for item in deep_skeleton
        )

    context = build(excerpts(detail_budget))
    serialized = context.model_dump_json()
    while len(serialized) > role.max_context_chars and detail_budget > 100:
        excess_per_item = (len(serialized) - role.max_context_chars) // len(deep_skeleton)
        detail_budget = max(100, detail_budget - excess_per_item - 25)
        context = build(excerpts(detail_budget))
        serialized = context.model_dump_json()
    if len(serialized) > role.max_context_chars:
        raise ValueError(
            f"assembled daily context has {len(serialized)} chars; "
            f"role limit is {role.max_context_chars}"
        )
    return context


def verify_daily_context_sources(role: AgentRoleConfig) -> None:
    """Fail closed when a role declares a context source the daily assembler cannot supply."""
    verify_context_sources(
        role,
        supported=DAILY_CONTEXT_SOURCES,
        context_model=DailyAgentContext,
        label="daily context",
    )


def validate_daily_decision(
    decision: DailyDecision,
    context: DailyAgentContext,
) -> DailyDecision:
    """Reject unsupported symbols, uncited ideas, invented evidence IDs, and unknown theses."""
    candidate_symbols = {candidate.symbol for candidate in context.candidates}
    position_symbols = {position.symbol for position in context.positions}
    admitted_symbols = candidate_symbols | position_symbols
    admitted_evidence = set(context.admitted_evidence_ids)
    admitted_theses = {thesis.thesis_id: thesis.symbol for thesis in context.open_theses}
    for symbol in decision.watchlist:
        if symbol not in admitted_symbols:
            raise ValueError(f"watchlist symbol was not admitted to this run: {symbol}")
    for proposal in decision.proposals:
        if proposal.symbol not in admitted_symbols:
            raise ValueError(f"proposal symbol was not admitted to this run: {proposal.symbol}")
        if not proposal.evidence_ids:
            raise ValueError(f"proposal {proposal.proposal_id} has no evidence")
        unknown = set(proposal.evidence_ids).difference(admitted_evidence)
        if unknown:
            raise ValueError("proposal cites evidence outside the invocation manifest")
        if proposal.thesis_id is not None:
            thesis_symbol = admitted_theses.get(str(proposal.thesis_id))
            if thesis_symbol is None:
                raise ValueError("proposal cites a thesis outside the supplied context")
            if thesis_symbol != proposal.symbol:
                raise ValueError("proposal cites a thesis belonging to another symbol")
    packets = cast(tuple[NamedPacket, ...], getattr(context, "research_packets", ()))
    required_dissent = {
        (envelope.step, claim.claim_id)
        for envelope in packets
        for symbol in envelope.packet.symbols
        for claim in (*symbol.contradictions, *symbol.dissent)
    }
    supplied_dissent = {
        (item.packet_step, item.claim_id) for item in decision.dissent_dispositions
    }
    missing_dissent = sorted(required_dissent - supplied_dissent)
    unknown_dissent = sorted(supplied_dissent - required_dissent)
    if missing_dissent:
        raise ValueError(f"decision did not address consumed dissent claims: {missing_dissent}")
    if unknown_dissent:
        raise ValueError(f"decision addressed unknown dissent claims: {unknown_dissent}")
    for disposition in decision.dissent_dispositions:
        unknown = set(disposition.evidence_ids).difference(admitted_evidence)
        if unknown:
            raise ValueError("dissent disposition cites evidence outside the invocation manifest")
        if disposition.defer_until is not None:
            _validate_wait_trigger(disposition.defer_until, admitted_symbols)
    waiting = cast(
        tuple[WaitingDecisionMemory, ...], getattr(context, "waiting_decisions", ())
    )
    _validate_wait_reconsideration(decision, waiting)
    if decision.abstention is not None:
        if (
            decision.abstention.reconsider_at is not None
            and decision.abstention.reconsider_at <= context.as_of
        ):
            raise ValueError("abstention reconsider_at must be later than the context cutoff")
        for trigger in decision.abstention.triggers:
            _validate_wait_trigger(trigger, admitted_symbols)
    return decision


def _validate_wait_reconsideration(
    decision: DailyDecision,
    waiting: tuple[WaitingDecisionMemory, ...],
) -> None:
    if len(waiting) > 1:
        raise ValueError("book context may contain only the latest active wait")
    if not waiting:
        if decision.wait_reconsiderations:
            raise ValueError("decision reconsidered a wait that is not active")
        return
    prior = waiting[0]
    proposal_symbols = {proposal.symbol for proposal in decision.proposals}
    relevant = bool(proposal_symbols) and (
        not prior.scope_symbols or bool(proposal_symbols.intersection(prior.scope_symbols))
    )
    if not relevant:
        if decision.wait_reconsiderations:
            raise ValueError("decision reconsidered a wait unrelated to its proposals")
        return
    if not prior.reopenable:
        raise ValueError("proposal reopens a prior wait without a changed condition")
    if len(decision.wait_reconsiderations) != 1:
        raise ValueError("proposal reopening a prior wait requires one reconsideration record")
    record = decision.wait_reconsiderations[0]
    if record.prior_decision_id != prior.decision_id:
        raise ValueError("wait reconsideration cites an unknown prior decision")
    satisfied = {
        item.trigger_id for item in prior.trigger_assessments if item.status == "SATISFIED"
    }
    if not set(record.trigger_ids).issubset(satisfied):
        raise ValueError("wait reconsideration cites a trigger that is not satisfied")
    if not set(record.new_evidence_ids).issubset(set(prior.new_evidence_ids)):
        raise ValueError("wait reconsideration cites evidence that is not new")
    if not prior.review_due and not record.trigger_ids and not record.new_evidence_ids:
        raise ValueError("wait reconsideration does not identify what changed")


def _validate_wait_trigger(trigger: WaitTrigger, admitted_symbols: set[str]) -> None:
    if trigger.symbol is not None and trigger.symbol not in admitted_symbols:
        raise ValueError(f"wait trigger symbol was not admitted to this run: {trigger.symbol}")


def _truncate(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 16)].rstrip() + "\n[TRUNCATED]"


def _briefing_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("daily update text cannot be blank")
    if TEMPLATE_TOKEN_MARKER in normalized:
        raise ValueError("daily update text cannot contain template token markers")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in normalized):
        raise ValueError("daily update text cannot contain control characters")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
