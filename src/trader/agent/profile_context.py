"""Shared immutable evidence and role-specific projections for simulated research teams.

Collection and portfolio state remain outside the profile executor. This module loads research
once and budgets each projection after consumed packets have been reserved, using the same
canonical serialization as the invocation boundary. The incumbent daily context is unchanged.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.config import AgentRoleConfig, ContextSource
from trader.agent.invocation import canonical_json, verify_context_sources
from trader.agent.packets import NamedPacket
from trader.agent.reasoning import (
    DAILY_CONTEXT_SOURCES,
    CandidateContext,
    DailyAgentContext,
    DeepEvidenceItem,
    EvidenceCatalogItem,
    ReasoningModel,
)
from trader.broker.models import Account, Position
from trader.ledger.models import RecentRunDecision, WaitingDecisionMemory
from trader.persistence.models import ResearchItem, ResearchItemSymbol
from trader.research.service import ResearchRunResult
from trader.universe.models import UniverseScan

RESEARCH_CONTEXT_SOURCES: dict[ContextSource, tuple[str, ...]] = {
    "candidate_overview": ("candidates",),
    "deep_research": ("evidence_catalog", "deep_evidence", "admitted_evidence_ids"),
    "research_packets": ("research_packets",),
}
BOOK_CONTEXT_SOURCES: dict[ContextSource, tuple[str, ...]] = {
    **DAILY_CONTEXT_SOURCES,
    "recent_decisions": ("recent_decisions", "waiting_decisions"),
    "research_packets": ("research_packets",),
}


class ResearchDocument(ReasoningModel):
    research_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str
    raw_artifact_path: str
    recorded_published_at: datetime
    effective_at: datetime


class ResearchBundle(ReasoningModel):
    run_id: str
    as_of: datetime
    candidates: tuple[CandidateContext, ...]
    evidence_catalog: tuple[EvidenceCatalogItem, ...]
    documents: tuple[ResearchDocument, ...]
    deep_ids: tuple[str, ...]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical_json(self).encode()).hexdigest()

    @property
    def admitted_evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.research_id for item in self.evidence_catalog)


class ResearchAgentContext(ReasoningModel):
    schema_version: Literal[1] = 1
    run_id: str
    as_of: datetime
    candidates: tuple[CandidateContext, ...]
    evidence_catalog: tuple[EvidenceCatalogItem, ...]
    deep_evidence: tuple[DeepEvidenceItem, ...]
    admitted_evidence_ids: tuple[str, ...]
    research_packets: tuple[NamedPacket, ...] = ()

    @field_validator("as_of")
    @classmethod
    def aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("research context cutoff must be timezone-aware")
        return value.astimezone(UTC)


class BookAgentContext(DailyAgentContext):
    waiting_decisions: tuple[WaitingDecisionMemory, ...] = ()
    research_packets: tuple[NamedPacket, ...] = ()


def load_research_bundle(
    session: Session,
    *,
    run_id: str,
    as_of: datetime,
    scan: UniverseScan,
    research: ResearchRunResult,
) -> ResearchBundle:
    """Copy admitted persisted research out of the ORM once for every book and profile step."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("research cutoff must be timezone-aware")
    requested_cutoff = _utc(as_of)
    if scan.as_of != research.plan.as_of or scan.as_of > requested_cutoff:
        raise ValueError(
            "research and scan must share a timestamp no later than the profile cutoff"
        )
    scan_symbols = tuple(candidate.symbol for candidate in scan.candidates)
    plan_symbols = research.plan.candidate_symbols
    if len(plan_symbols) != len(set(plan_symbols)):
        raise ValueError("research plan candidates must be unique")
    if len(plan_symbols) != len(scan_symbols) or set(plan_symbols) != set(scan_symbols):
        raise ValueError("research plan candidates must exactly match the supplied scan")
    admitted = research.persisted_research_ids
    if not admitted or len(admitted) != len(set(admitted)):
        raise ValueError("profile research requires nonempty unique admitted evidence")
    items = tuple(
        session.scalars(
            select(ResearchItem)
            .where(ResearchItem.run_id == run_id, ResearchItem.id.in_(admitted))
            .order_by(ResearchItem.id)
        )
    )
    if {item.id for item in items} != set(admitted):
        raise LookupError("persisted research records are missing from this run")
    links = session.scalars(
        select(ResearchItemSymbol)
        .where(ResearchItemSymbol.research_id.in_(admitted))
        .order_by(ResearchItemSymbol.research_id, ResearchItemSymbol.symbol)
    )
    symbols: dict[str, list[str]] = defaultdict(list)
    by_symbol: dict[str, list[str]] = defaultdict(list)
    for link in links:
        symbols[link.research_id].append(link.symbol)
        by_symbol[link.symbol].append(link.research_id)
    # Collection occurs after the scan is pinned. The causal decision cutoff is therefore the
    # latest retrieval in this same run, rather than the earlier scan timestamp.
    cutoff = max((requested_cutoff, *(_utc(item.retrieved_at) for item in items)))
    effective_times = {item.id: _effective_time(item, cutoff) for item in items}
    return ResearchBundle(
        run_id=run_id,
        as_of=cutoff,
        candidates=tuple(
            CandidateContext(
                symbol=candidate.symbol,
                score=candidate.score,
                signals=tuple(signal.model_dump(mode="json") for signal in candidate.signals),
                evidence_ids=tuple(sorted(set(by_symbol[candidate.symbol]))),
            )
            for candidate in scan.candidates
        ),
        evidence_catalog=tuple(
            EvidenceCatalogItem(
                research_id=item.id,
                symbols=tuple(symbols[item.id]),
                source_tier=item.source_tier,
                source_type=item.source_type,
                provider=item.provider or "legacy",
                headline=_truncate(item.headline, 400),
                published_at=effective_times[item.id],
                retrieved_at=_utc(item.retrieved_at),
                summary=_truncate(item.normalized_summary, 500),
            )
            for item in items
        ),
        documents=tuple(
            ResearchDocument(
                research_id=item.id,
                content_hash=item.content_hash,
                text=item.normalized_text,
                raw_artifact_path=item.raw_artifact_path or "",
                recorded_published_at=_utc(item.published_at),
                effective_at=effective_times[item.id],
            )
            for item in items
        ),
        deep_ids=tuple(
            sorted(
                {
                    research_id
                    for symbol in research.plan.deep_symbols
                    for research_id in by_symbol[symbol]
                }
            )
        ),
    )


def project_research_context(
    bundle: ResearchBundle, role: AgentRoleConfig, packets: tuple[NamedPacket, ...]
) -> ResearchAgentContext:
    verify_context_sources(
        role,
        supported=RESEARCH_CONTEXT_SOURCES,
        context_model=ResearchAgentContext,
        label="research context",
    )
    _verify_packets_source(role, packets)
    sources = set(role.context_sources)

    def build(deep: tuple[DeepEvidenceItem, ...]) -> ResearchAgentContext:
        return ResearchAgentContext(
            run_id=bundle.run_id,
            as_of=bundle.as_of,
            candidates=bundle.candidates if "candidate_overview" in sources else (),
            evidence_catalog=bundle.evidence_catalog if "deep_research" in sources else (),
            admitted_evidence_ids=(
                bundle.admitted_evidence_ids if "deep_research" in sources else ()
            ),
            deep_evidence=deep,
            research_packets=packets,
        )

    return _budget_projection(bundle, role, build, include_deep="deep_research" in sources)


def project_book_context(
    bundle: ResearchBundle,
    role: AgentRoleConfig,
    packets: tuple[NamedPacket, ...],
    *,
    strategy: str,
    portfolio_policy: str,
    account: Account,
    positions: tuple[Position, ...],
    recent_decisions: tuple[RecentRunDecision, ...],
    waiting_decisions: tuple[WaitingDecisionMemory, ...] = (),
) -> BookAgentContext:
    verify_context_sources(
        role, supported=BOOK_CONTEXT_SOURCES, context_model=BookAgentContext, label="book context"
    )
    _verify_packets_source(role, packets)

    def build(deep: tuple[DeepEvidenceItem, ...]) -> BookAgentContext:
        return BookAgentContext(
            run_id=bundle.run_id,
            as_of=bundle.as_of,
            strategy=strategy,
            portfolio_policy=portfolio_policy,
            account=account,
            positions=positions,
            open_orders=(),
            candidates=bundle.candidates,
            evidence_catalog=bundle.evidence_catalog,
            deep_evidence=deep,
            admitted_evidence_ids=bundle.admitted_evidence_ids,
            recent_decisions=recent_decisions,
            open_theses=(),
            waiting_decisions=waiting_decisions,
            research_packets=packets,
        )

    return _budget_projection(bundle, role, build)


def _budget_projection[Context: ReasoningModel](
    bundle: ResearchBundle,
    role: AgentRoleConfig,
    build: Callable[[tuple[DeepEvidenceItem, ...]], Context],
    *,
    include_deep: bool = True,
) -> Context:
    documents = tuple(
        item for item in bundle.documents if include_deep and item.research_id in bundle.deep_ids
    )

    def context_at(limit: int) -> Context:
        return build(
            tuple(
                DeepEvidenceItem(
                    research_id=item.research_id,
                    raw_artifact_path=item.raw_artifact_path,
                    excerpt=_truncate(item.text, limit) if limit else "",
                )
                for item in documents
            )
        )

    skeleton = context_at(0)
    if len(canonical_json(skeleton)) > role.max_context_chars:
        raise ValueError("profile context including consumed packets exceeds role limit")
    # Keep packets whole. Only raw excerpts are shortened, identically and deterministically.
    low, high = 0, role.max_document_chars
    result = skeleton
    while low <= high:
        limit = (low + high) // 2
        candidate = context_at(limit)
        if len(canonical_json(candidate)) <= role.max_context_chars:
            result, low = candidate, limit + 1
        else:
            high = limit - 1
    return result


def _verify_packets_source(role: AgentRoleConfig, packets: tuple[NamedPacket, ...]) -> None:
    if packets and "research_packets" not in role.context_sources:
        raise ValueError("consumed packets require the research_packets context source")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "[TRUNCATED]"
    return value[: max(0, limit - len(marker))] + marker[:limit]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _effective_time(item: ResearchItem, as_of: datetime) -> datetime:
    """Alpaca market documents are stamped at retrieval; their observations have own times.

    Retain the provider's original timestamp in the bundle. The projected catalog uses the
    latest underlying observation when supplied. A genuine future observation/publication is
    rejected; retrieval after the scan does not by itself mean hindsight.
    """
    published = _utc(item.published_at)
    if item.provider == "alpaca" and item.source_type == "MARKET_DATA" and item.metadata_json:
        metadata = json.loads(item.metadata_json)
        values = metadata.get("data_timestamps") if isinstance(metadata, dict) else None
        if not isinstance(values, list) or not values:
            raise ValueError("market evidence requires observation timestamps")
        observations: list[datetime] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("market observation timestamp must be an ISO string")
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError("market observation timestamp must be timezone-aware")
            observations.append(moment.astimezone(UTC))
        published = max(observations)
    if published > as_of:
        raise ValueError("research observation/publication postdates the profile cutoff")
    return published
