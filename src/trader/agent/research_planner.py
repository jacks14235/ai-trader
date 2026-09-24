"""Audited model-directed second-round research planning."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.codex_cli import StructuredReasoningProvider
from trader.agent.config import AgentConfig, AgentRoleConfig, ContextSource
from trader.agent.invocation import (
    WorkflowStep,
    canonical_json,
    invoke_role,
    resolve_role,
    verify_context_sources,
)
from trader.persistence.models import ResearchItem, ResearchItemSymbol
from trader.research.config import ResearchConfig, ResearchFollowUpConfig
from trader.research.models import ResearchRequest
from trader.research.selection import DeepSelectionAssessment
from trader.research.service import ResearchRunResult
from trader.universe.models import UniverseScan

FOLLOW_UP_WORKFLOW = WorkflowStep(role="research_planner")
FOLLOW_UP_CONTEXT_SOURCES: dict[ContextSource, tuple[str, ...]] = {
    "candidate_overview": ("candidates", "deep_selection"),
    "deep_research": ("evidence", "admitted_evidence_ids"),
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FollowUpCandidate(_Model):
    symbol: str
    score: int
    follow_up_eligible: bool
    initially_deep: bool
    available_question_types: tuple[
        Literal["COMPANY_NEWS", "SEC_FILINGS", "SEC_FILING_HISTORY"], ...
    ]


class FollowUpEvidence(_Model):
    research_id: str
    symbols: tuple[str, ...]
    source_tier: str
    source_type: str
    provider: str
    headline: str
    summary: str
    excerpt: str = ""


class FollowUpResearchContext(_Model):
    run_id: str
    as_of: str
    candidates: tuple[FollowUpCandidate, ...]
    deep_selection: tuple[DeepSelectionAssessment, ...]
    evidence: tuple[FollowUpEvidence, ...]
    admitted_evidence_ids: tuple[str, ...]
    allowed_question_types: tuple[
        Literal["COMPANY_NEWS", "SEC_FILINGS", "SEC_FILING_HISTORY"], ...
    ] = ("COMPANY_NEWS", "SEC_FILINGS", "SEC_FILING_HISTORY")
    max_requests: int
    max_new_deep_symbols: int


class FollowUpRequestProposal(_Model):
    symbol: str
    question_type: Literal["COMPANY_NEWS", "SEC_FILINGS", "SEC_FILING_HISTORY"]
    gap: str = Field(min_length=1, max_length=300)
    decision_relevance: str = Field(min_length=1, max_length=500)

    @field_validator("symbol")
    @classmethod
    def normalized_symbol(cls, value: str) -> str:
        return value.upper().strip()

    @field_validator("gap", "decision_relevance")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("follow-up request text cannot be blank")
        return normalized


class FollowUpResearchDecision(_Model):
    requests: tuple[FollowUpRequestProposal, ...] = Field(max_length=8)
    no_follow_up_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def coherent_decision(self) -> FollowUpResearchDecision:
        if self.requests and self.no_follow_up_reason is not None:
            raise ValueError("a nonempty follow-up plan cannot include a no-follow-up reason")
        if not self.requests and not (self.no_follow_up_reason or "").strip():
            raise ValueError("an empty follow-up plan requires a reason")
        identities = [(item.symbol, item.question_type) for item in self.requests]
        if len(identities) != len(set(identities)):
            raise ValueError("follow-up requests must be unique by symbol and question type")
        return self


class FollowUpPlanningResult(_Model):
    invocation_id: str
    decision: FollowUpResearchDecision
    questions: tuple[ResearchRequest, ...]


class AgentFollowUpResearchPlanner:
    """Let a read-only role choose questions; deterministic code owns every boundary."""

    def __init__(
        self,
        session: Session,
        config: AgentConfig,
        research_config: ResearchConfig,
        *,
        prompt: str,
        provider: StructuredReasoningProvider,
        sec_symbols: frozenset[str],
    ) -> None:
        self.role, _profile = resolve_role(config, "research_planner")
        verify_context_sources(
            self.role,
            supported=FOLLOW_UP_CONTEXT_SOURCES,
            context_model=FollowUpResearchContext,
            label="research planner context",
        )
        self.session = session
        self.config = config
        self.research_config = research_config
        self.follow_up = research_config.follow_up
        self.prompt = prompt.strip()
        self.provider = provider
        self.sec_symbols = sec_symbols

    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        scan: UniverseScan,
        research: ResearchRunResult,
    ) -> FollowUpPlanningResult:
        context = _assemble_context(
            self.session,
            run_id=run_id,
            scan=scan,
            research=research,
            role=self.role,
            follow_up=self.follow_up,
            sec_symbols=self.sec_symbols,
        )
        eligible = {
            candidate.symbol: candidate for candidate in context.candidates
            if candidate.follow_up_eligible
        }

        def validate(decision: FollowUpResearchDecision) -> FollowUpResearchDecision:
            if len(decision.requests) > self.follow_up.max_questions:
                raise ValueError("follow-up plan exceeds the configured question cap")
            new_symbols = {
                item.symbol
                for item in decision.requests
                if item.symbol not in research.plan.deep_symbols
            }
            if len(new_symbols) > self.follow_up.max_new_deep_symbols:
                raise ValueError("follow-up plan exceeds the new deep-symbol cap")
            for item in decision.requests:
                candidate = eligible.get(item.symbol)
                if candidate is None:
                    raise ValueError(
                        f"follow-up request names an ineligible symbol: {item.symbol}"
                    )
                if item.question_type not in candidate.available_question_types:
                    raise ValueError(
                        f"follow-up question is unavailable for {item.symbol}: "
                        f"{item.question_type}"
                    )
            return decision

        result = invoke_role(
            self.session,
            self.config,
            run_id=run_id,
            run_directory=run_directory,
            workflow=FOLLOW_UP_WORKFLOW,
            prompt=self.prompt,
            context=context,
            output_model=FollowUpResearchDecision,
            admitted_evidence_ids=context.admitted_evidence_ids,
            provider=self.provider,
            validate=validate,
        )
        questions = tuple(
            _request_from_proposal(
                proposal,
                as_of=research.plan.as_of,
                priority=max(1, 80 - index),
                config=self.research_config,
            )
            for index, proposal in enumerate(result.output.requests)
        )
        return FollowUpPlanningResult(
            invocation_id=result.invocation_id,
            decision=result.output,
            questions=questions,
        )


def _assemble_context(
    session: Session,
    *,
    run_id: str,
    scan: UniverseScan,
    research: ResearchRunResult,
    role: AgentRoleConfig,
    follow_up: ResearchFollowUpConfig,
    sec_symbols: frozenset[str],
) -> FollowUpResearchContext:
    admitted = research.persisted_research_ids
    items = list(
        session.scalars(
            select(ResearchItem)
            .where(ResearchItem.run_id == run_id, ResearchItem.id.in_(admitted))
            .order_by(ResearchItem.id)
        )
    )
    if len(items) != len(admitted):
        raise LookupError("follow-up planner could not load every admitted research item")
    links = list(
        session.scalars(
            select(ResearchItemSymbol)
            .where(ResearchItemSymbol.research_id.in_(admitted))
            .order_by(ResearchItemSymbol.research_id, ResearchItemSymbol.symbol)
        )
    )
    symbols_by_id: dict[str, list[str]] = defaultdict(list)
    for link in links:
        symbols_by_id[link.research_id].append(link.symbol)
    deep = set(research.plan.deep_symbols)
    assessment_by_symbol = {item.symbol: item for item in research.deep_selection}
    candidates = tuple(
        FollowUpCandidate(
            symbol=candidate.symbol,
            score=candidate.score,
            follow_up_eligible=(
                candidate.symbol in deep
                or assessment_by_symbol.get(candidate.symbol) is not None
                and assessment_by_symbol[candidate.symbol].reason_codes
                == ("POLICY_SCREEN_PASSED",)
            ),
            initially_deep=candidate.symbol in deep,
            available_question_types=_available_question_types(
                symbol=candidate.symbol,
                initially_deep=candidate.symbol in deep,
                sec_symbols=sec_symbols,
            ),
        )
        for candidate in scan.candidates
    )

    def evidence(excerpts: set[str]) -> tuple[FollowUpEvidence, ...]:
        return tuple(
            FollowUpEvidence(
                research_id=item.id,
                symbols=tuple(symbols_by_id[item.id]),
                source_tier=item.source_tier,
                source_type=item.source_type,
                provider=item.provider or "legacy",
                headline=item.headline[:400],
                summary=item.normalized_summary[:500],
                excerpt=(
                    item.normalized_text[: role.max_document_chars]
                    if item.id in excerpts
                    else ""
                ),
            )
            for item in items
        )

    def build(excerpts: set[str]) -> FollowUpResearchContext:
        return FollowUpResearchContext(
            run_id=run_id,
            as_of=research.plan.as_of.isoformat(),
            candidates=candidates,
            deep_selection=research.deep_selection,
            evidence=evidence(excerpts),
            admitted_evidence_ids=tuple(item.id for item in items),
            max_requests=follow_up.max_questions,
            max_new_deep_symbols=follow_up.max_new_deep_symbols,
        )

    chosen: set[str] = set()
    deep_item_ids = [
        item.id
        for item in items
        if deep.intersection(symbols_by_id[item.id])
    ]
    for research_id in deep_item_ids:
        trial = build(chosen | {research_id})
        if len(canonical_json(trial)) > role.max_context_chars:
            break
        chosen.add(research_id)
    context = build(chosen)
    if len(canonical_json(context)) > role.max_context_chars:
        raise ValueError("research planner context skeleton exceeds its configured limit")
    return context


def _request_from_proposal(
    proposal: FollowUpRequestProposal,
    *,
    as_of: object,
    priority: int,
    config: ResearchConfig,
) -> ResearchRequest:
    from datetime import datetime

    if not isinstance(as_of, datetime):
        raise TypeError("research cutoff must be a datetime")
    if proposal.question_type == "COMPANY_NEWS":
        window_start = as_of - timedelta(days=config.follow_up.company_news_days)
        window_end = as_of
    elif proposal.question_type == "SEC_FILINGS":
        window_start = as_of - timedelta(days=config.freshness.sec_filings_days)
        window_end = as_of
    else:
        window_start = as_of - timedelta(
            days=config.follow_up.sec_filing_history_days
        )
        window_end = as_of - timedelta(days=config.freshness.sec_filings_days)
    query = (
        f"Resolve this decision-relevant gap for {proposal.symbol}: {proposal.gap}"
    )
    return ResearchRequest.create(
        symbol=proposal.symbol,
        question_type=proposal.question_type,
        query=query[:500],
        window_start=window_start,
        window_end=window_end,
        priority=priority,
    )


def _available_question_types(
    *,
    symbol: str,
    initially_deep: bool,
    sec_symbols: frozenset[str],
) -> tuple[Literal["COMPANY_NEWS", "SEC_FILINGS", "SEC_FILING_HISTORY"], ...]:
    if symbol not in sec_symbols:
        return ("COMPANY_NEWS",)
    if initially_deep:
        return ("COMPANY_NEWS", "SEC_FILING_HISTORY")
    return ("COMPANY_NEWS", "SEC_FILINGS")
