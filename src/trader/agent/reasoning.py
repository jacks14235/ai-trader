"""Deterministic context assembly and validated paper-proposal contracts."""

from collections import defaultdict
from datetime import UTC, datetime
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.config import AgentRoleConfig, ContextSource
from trader.agent.invocation import verify_context_sources
from trader.agent.models import SYMBOL_PATTERN, TradeProposal
from trader.broker.models import Account, BrokerOrder, Position
from trader.ledger.history import load_open_theses, load_recent_decisions
from trader.ledger.models import OpenThesis, RecentRunDecision
from trader.persistence.models import ResearchItem, ResearchItemSymbol
from trader.research.service import ResearchRunResult
from trader.universe.models import UniverseScan

TEMPLATE_TOKEN_MARKER = "__DAILY_UPDATE_"

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


class DailyDecision(ReasoningModel):
    schema_version: Literal[1] = 1
    status: Literal["NO_ACTION", "PROPOSE_TRADES"]
    market_assessment: str = Field(min_length=1, max_length=10_000)
    strongest_counterargument: str = Field(min_length=1, max_length=5_000)
    no_action_reason: str | None = Field(default=None, min_length=1, max_length=5_000)
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
            if self.proposals or self.no_action_reason is None:
                raise ValueError("NO_ACTION requires a reason and cannot contain proposals")
        elif not self.proposals or self.no_action_reason is not None:
            raise ValueError("PROPOSE_TRADES requires proposals and no no_action_reason")
        if any(proposal.action == "HOLD" for proposal in self.proposals):
            raise ValueError("trade proposals may only BUY or SELL")
        return self


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
    return decision


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
