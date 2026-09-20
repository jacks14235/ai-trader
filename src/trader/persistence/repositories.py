"""Transactional persistence helpers for the trading audit trail."""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trader.agent.models import TradeProposal
from trader.broker.models import Account, Position
from trader.risk.models import RiskDecision

if TYPE_CHECKING:
    from trader.agent.reasoning import DailyDecision

from .models import (
    AgentDecisionRecord,
    AgentInvocation,
    AgentInvocationEvidence,
    BookEvaluation,
    BrokerOrderEvent,
    BrokerOrderRecord,
    DailyReport,
    Fill,
    KnowledgeChange,
    MarketEvent,
    MarketSnapshot,
    PerformanceSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
    ResearchItem,
    ResearchItemQuestion,
    ResearchItemSymbol,
    RiskDecisionRecord,
    Run,
    RunEvent,
    ScheduledRun,
    ScheduledRunEvent,
    TradeProposalRecord,
)


class PersistenceConflictError(RuntimeError):
    """Raised when an idempotency key is reused for different immutable content."""


@dataclass(frozen=True)
class RunAuditTrail:
    """Records required to reconstruct a run without relying on current knowledge."""

    run: Run
    scheduled_run: ScheduledRun | None
    scheduled_run_events: list[ScheduledRunEvent]
    market_event: MarketEvent | None
    events: list[RunEvent]
    portfolio_snapshots: list[PortfolioSnapshot]
    position_snapshots: list[PositionSnapshot]
    market_snapshots: list[MarketSnapshot]
    research_items: list[ResearchItem]
    research_item_symbols: list[ResearchItemSymbol]
    research_item_questions: list[ResearchItemQuestion]
    agent_invocations: list[AgentInvocation]
    agent_invocation_evidence: list[AgentInvocationEvidence]
    agent_decisions: list[AgentDecisionRecord]
    trade_proposals: list[TradeProposalRecord]
    risk_decisions: list[RiskDecisionRecord]
    broker_orders: list[BrokerOrderRecord]
    broker_order_events: list[BrokerOrderEvent]
    fills: list[Fill]
    knowledge_changes: list[KnowledgeChange]
    daily_reports: list[DailyReport]
    performance_snapshots: list[PerformanceSnapshot]


def claim_run(
    session: Session,
    run_key: str,
    scheduled_for: datetime,
    config_hash: str,
    *,
    mode: str = "paper",
    git_commit_sha: str | None = None,
    agent_model: str | None = None,
    prompt_version: str | None = None,
    raw_artifact_path: str | None = None,
) -> Run | None:
    """Atomically claim a scheduled run window, returning ``None`` for a duplicate."""
    if mode != "paper":
        raise ValueError("only paper-trading runs may be persisted")
    run = Run(
        run_key=run_key,
        scheduled_for=scheduled_for,
        config_hash=config_hash,
        mode=mode,
        git_commit_sha=git_commit_sha,
        agent_model=agent_model,
        prompt_version=prompt_version,
        raw_artifact_path=raw_artifact_path,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    return run


def event(
    session: Session,
    run_id: str,
    stage: str,
    detail: str | None = None,
    *,
    metadata: dict[str, object] | None = None,
) -> RunEvent:
    """Append a run event and commit it independently for crash visibility."""
    record = RunEvent(
        run_id=run_id,
        stage=stage,
        detail=detail,
        metadata_json=_canonical_json(metadata) if metadata is not None else None,
    )
    session.add(record)
    session.commit()
    return record


def snapshot(
    session: Session,
    account: Account,
    positions: list[Position],
    run_id: str | None = None,
    *,
    snapshot_type: str = "CURRENT",
) -> PortfolioSnapshot:
    """Persist an account and all of its positions in one transaction."""
    snap = PortfolioSnapshot(
        run_id=run_id,
        snapshot_type=snapshot_type,
        equity=str(account.equity),
        cash=str(account.cash),
        buying_power=str(account.buying_power),
        raw_json=account.model_dump_json(),
    )
    session.add(snap)
    session.flush()
    session.add_all(
        [
            PositionSnapshot(
                portfolio_snapshot_id=snap.id,
                symbol=position.symbol,
                qty=str(position.qty),
                market_value=str(position.market_value),
                current_price=str(position.current_price),
            )
            for position in positions
        ]
    )
    session.commit()
    return snap


def latest_snapshot(session: Session, *, run_id: str | None = None) -> PortfolioSnapshot | None:
    """Return the most recently captured portfolio snapshot."""
    statement = select(PortfolioSnapshot)
    if run_id is not None:
        statement = statement.where(PortfolioSnapshot.run_id == run_id)
    return session.scalar(statement.order_by(PortfolioSnapshot.captured_at.desc()).limit(1))


RESEARCH_SOURCE_TIERS = frozenset(
    {"BROKER", "PRIMARY", "WEB", "PAID", "SOCIAL", "LEGACY"}
)
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,31}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESEARCH_SYMBOLS = 50
_MAX_INVOCATION_EVIDENCE = 250
_MAX_RESEARCH_RESULTS = 500
_MAX_METADATA_JSON_CHARS = 100_000
_MAX_NORMALIZED_TEXT_CHARS = 2_000_000


def persist_research_item(
    session: Session,
    *,
    research_id: str,
    run_id: str,
    symbols: Sequence[str],
    source_tier: str,
    source_type: str,
    source_name: str,
    provider: str,
    provider_item_id: str,
    research_question_id: str,
    research_question: str,
    raw_artifact_path: str,
    normalized_summary: str,
    normalized_text: str,
    published_at: datetime,
    retrieved_at: datetime,
    content_hash: str,
    headline: str,
    url: str | None = None,
    author: str | None = None,
    metadata: dict[str, object] | None = None,
    cost_usd: Decimal | None = None,
) -> ResearchItem:
    """Persist one immutable run-scoped source with strict natural-key idempotency.

    Callers should derive ``research_id`` deterministically from the run ID and content hash
    (for example with UUID5). The question is deliberately excluded: one item may answer several
    questions in the same run. Identical content is intentionally allowed in separate runs so each
    historical run remains independently reconstructable.
    """
    research_id = _required_text(research_id, "research_id", max_length=128)
    run_id = _required_text(run_id, "run_id", max_length=128)
    normalized_symbols = _validate_symbols(symbols)
    normalized_tier = source_tier.strip().upper()
    if normalized_tier not in RESEARCH_SOURCE_TIERS:
        raise ValueError(f"source_tier must be one of {sorted(RESEARCH_SOURCE_TIERS)}")
    source_type = _required_text(source_type, "source_type", max_length=100)
    source_name = _required_text(source_name, "source_name", max_length=200)
    provider = _required_text(provider, "provider", max_length=100)
    provider_item_id = _required_text(provider_item_id, "provider_item_id", max_length=500)
    research_question_id = _required_text(
        research_question_id,
        "research_question_id",
        max_length=500,
    )
    research_question = _required_text(
        research_question,
        "research_question",
        max_length=2_000,
    )
    raw_artifact_path = _required_text(
        raw_artifact_path,
        "raw_artifact_path",
        max_length=4_000,
    )
    normalized_summary = _required_text(
        normalized_summary,
        "normalized_summary",
        max_length=20_000,
    )
    normalized_text = _required_text(
        normalized_text,
        "normalized_text",
        max_length=_MAX_NORMALIZED_TEXT_CHARS,
    )
    headline = _required_text(headline, "headline", max_length=2_000)
    url = _optional_bounded_text(url, "url", max_length=2_000)
    author = _optional_bounded_text(author, "author", max_length=500)
    normalized_hash = content_hash.strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized_hash) is None:
        raise ValueError("content_hash must be a lowercase hexadecimal SHA-256 digest")
    published_at = _aware_utc(published_at, "published_at")
    retrieved_at = _aware_utc(retrieved_at, "retrieved_at")
    if published_at > retrieved_at:
        raise ValueError("published_at cannot be later than retrieved_at")
    metadata_json = _canonical_json(metadata) if metadata is not None else None
    if metadata_json is not None and len(metadata_json) > _MAX_METADATA_JSON_CHARS:
        raise ValueError("metadata exceeds the hard size limit")
    cost_value = _cost_string(cost_usd)

    existing = session.get(ResearchItem, research_id)
    if existing is not None:
        _verify_research_item(
            session,
            existing,
            research_id=research_id,
            run_id=run_id,
            source_tier=normalized_tier,
            source_type=source_type,
            source_name=source_name,
            provider=provider,
            provider_item_id=provider_item_id,
            raw_artifact_path=raw_artifact_path,
            normalized_summary=normalized_summary,
            normalized_text=normalized_text,
            published_at=published_at,
            retrieved_at=retrieved_at,
            content_hash=normalized_hash,
            headline=headline,
            url=url,
            author=author,
            metadata_json=metadata_json,
            cost_usd=cost_value,
        )
        _associate_research_context(
            session,
            research_id=existing.id,
            symbols=normalized_symbols,
            research_question_id=research_question_id,
            research_question=research_question,
        )
        return existing

    natural_key_match = _research_natural_key_match(
        session,
        run_id=run_id,
        provider=provider,
        provider_item_id=provider_item_id,
        content_hash=normalized_hash,
    )
    if natural_key_match is not None:
        _verify_research_item(
            session,
            natural_key_match,
            research_id=natural_key_match.id,
            run_id=run_id,
            source_tier=normalized_tier,
            source_type=source_type,
            source_name=source_name,
            provider=provider,
            provider_item_id=provider_item_id,
            raw_artifact_path=raw_artifact_path,
            normalized_summary=normalized_summary,
            normalized_text=normalized_text,
            published_at=published_at,
            retrieved_at=retrieved_at,
            content_hash=normalized_hash,
            headline=headline,
            url=url,
            author=author,
            metadata_json=metadata_json,
            cost_usd=cost_value,
        )
        _associate_research_context(
            session,
            research_id=natural_key_match.id,
            symbols=normalized_symbols,
            research_question_id=research_question_id,
            research_question=research_question,
        )
        return natural_key_match

    record = ResearchItem(
        id=research_id,
        run_id=run_id,
        source_tier=normalized_tier,
        source_type=source_type,
        source_name=source_name,
        provider=provider,
        provider_item_id=provider_item_id,
        url=url,
        author=author,
        published_at=published_at,
        retrieved_at=retrieved_at,
        headline=headline,
        normalized_summary=normalized_summary,
        normalized_text=normalized_text,
        content_hash=normalized_hash,
        raw_artifact_path=raw_artifact_path,
        metadata_json=metadata_json,
        cost_usd=cost_value,
    )
    session.add(record)
    try:
        session.flush()
        session.add_all(
            [
                ResearchItemSymbol(research_id=research_id, symbol=symbol)
                for symbol in normalized_symbols
            ]
        )
        session.add(
            ResearchItemQuestion(
                research_id=research_id,
                question_id=research_question_id,
                question_text=research_question,
            )
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = session.get(ResearchItem, research_id)
        if concurrent is None:
            natural_key_match = _research_natural_key_match(
                session,
                run_id=run_id,
                provider=provider,
                provider_item_id=provider_item_id,
                content_hash=normalized_hash,
            )
            if natural_key_match is not None:
                _verify_research_item(
                    session,
                    natural_key_match,
                    research_id=natural_key_match.id,
                    run_id=run_id,
                    source_tier=normalized_tier,
                    source_type=source_type,
                    source_name=source_name,
                    provider=provider,
                    provider_item_id=provider_item_id,
                    raw_artifact_path=raw_artifact_path,
                    normalized_summary=normalized_summary,
                    normalized_text=normalized_text,
                    published_at=published_at,
                    retrieved_at=retrieved_at,
                    content_hash=normalized_hash,
                    headline=headline,
                    url=url,
                    author=author,
                    metadata_json=metadata_json,
                    cost_usd=cost_value,
                )
                _associate_research_context(
                    session,
                    research_id=natural_key_match.id,
                    symbols=normalized_symbols,
                    research_question_id=research_question_id,
                    research_question=research_question,
                )
                return natural_key_match
            raise
        _verify_research_item(
            session,
            concurrent,
            research_id=research_id,
            run_id=run_id,
            source_tier=normalized_tier,
            source_type=source_type,
            source_name=source_name,
            provider=provider,
            provider_item_id=provider_item_id,
            raw_artifact_path=raw_artifact_path,
            normalized_summary=normalized_summary,
            normalized_text=normalized_text,
            published_at=published_at,
            retrieved_at=retrieved_at,
            content_hash=normalized_hash,
            headline=headline,
            url=url,
            author=author,
            metadata_json=metadata_json,
            cost_usd=cost_value,
        )
        _associate_research_context(
            session,
            research_id=concurrent.id,
            symbols=normalized_symbols,
            research_question_id=research_question_id,
            research_question=research_question,
        )
        return concurrent
    return record


def associate_agent_invocation_evidence(
    session: Session,
    *,
    agent_invocation_id: str,
    research_ids: Sequence[str],
) -> list[AgentInvocationEvidence]:
    """Immutably bind an invocation to the exact ordered evidence IDs it received."""
    invocation = session.get(AgentInvocation, agent_invocation_id)
    if invocation is None:
        raise LookupError(f"agent invocation not found: {agent_invocation_id}")
    normalized_ids = tuple(_required_text(item, "research_id") for item in research_ids)
    if len(normalized_ids) > _MAX_INVOCATION_EVIDENCE:
        raise ValueError(
            f"research_ids cannot contain more than {_MAX_INVOCATION_EVIDENCE} items"
        )
    if len(set(normalized_ids)) != len(normalized_ids):
        raise ValueError("research_ids must be unique")
    manifest_hash = hashlib.sha256(_canonical_json(normalized_ids).encode()).hexdigest()

    existing = _invocation_evidence(session, agent_invocation_id)
    if invocation.evidence_manifest_hash is not None:
        _verify_same(
            invocation.evidence_manifest_hash == manifest_hash
            and tuple(link.research_id for link in existing) == normalized_ids,
            "agent invocation evidence",
            agent_invocation_id,
        )
        return existing
    if existing:
        raise PersistenceConflictError(
            f"agent invocation evidence is inconsistent for {agent_invocation_id}"
        )

    if normalized_ids:
        items = list(
            session.scalars(select(ResearchItem).where(ResearchItem.id.in_(normalized_ids)))
        )
        items_by_id = {item.id: item for item in items}
        missing = [item_id for item_id in normalized_ids if item_id not in items_by_id]
        if missing:
            raise LookupError(f"research items not found: {', '.join(missing)}")
        wrong_run = [
            item_id
            for item_id in normalized_ids
            if items_by_id[item_id].run_id != invocation.run_id
        ]
        if wrong_run:
            raise ValueError(
                "invocation evidence must belong to the invocation run: " + ", ".join(wrong_run)
            )

    links = [
        AgentInvocationEvidence(
            agent_invocation_id=agent_invocation_id,
            research_id=research_id,
            ordinal=ordinal,
        )
        for ordinal, research_id in enumerate(normalized_ids)
    ]
    invocation.evidence_manifest_hash = manifest_hash
    session.add_all(links)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent_invocation = session.get(AgentInvocation, agent_invocation_id)
        if concurrent_invocation is None:
            raise
        concurrent = _invocation_evidence(session, agent_invocation_id)
        _verify_same(
            concurrent_invocation.evidence_manifest_hash == manifest_hash
            and tuple(link.research_id for link in concurrent) == normalized_ids,
            "agent invocation evidence",
            agent_invocation_id,
        )
        return concurrent
    return links


def persist_agent_decision(
    session: Session,
    run_id: str,
    invocation_id: str,
    decision: "DailyDecision",
    *,
    book_id: str | None = None,
    book_evaluation_id: str | None = None,
    commit: bool = True,
) -> AgentDecisionRecord:
    """Persist one validated terminal decision for the live line or a simulated book."""
    invocation = session.get(AgentInvocation, invocation_id)
    if invocation is None:
        raise LookupError(f"agent invocation not found: {invocation_id}")
    if invocation.run_id != run_id or invocation.role != "daily_trader":
        raise ValueError("terminal decision invocation must be this run's daily_trader")
    if book_id is None:
        if book_evaluation_id is not None or invocation.step:
            raise ValueError("live terminal decision must use the unnamed daily invocation")
    else:
        expected_prefix = f"book_{book_id.replace('-', '')}_"
        if not invocation.step.startswith(expected_prefix):
            raise ValueError("book terminal decision does not match its invocation namespace")
        if book_evaluation_id is None:
            raise ValueError("book terminal decision requires its evaluation ID")
        evaluation = session.get(BookEvaluation, book_evaluation_id)
        if (
            evaluation is None
            or evaluation.run_id != run_id
            or evaluation.book_id != book_id
        ):
            raise ValueError("book terminal decision does not match its evaluation")

    raw_json = decision.model_dump_json()
    abstention_json = (
        None if decision.abstention is None else decision.abstention.model_dump_json()
    )
    dissent_json = json.dumps(
        [item.model_dump(mode="json") for item in decision.dissent_dispositions],
        sort_keys=True,
        separators=(",", ":"),
    )
    existing = session.scalar(
        select(AgentDecisionRecord).where(
            AgentDecisionRecord.agent_invocation_id == invocation_id
        )
    )
    if existing is not None:
        _verify_same(
            existing.run_id == run_id
            and existing.book_id == book_id
            and existing.book_evaluation_id == book_evaluation_id
            and existing.schema_version == decision.schema_version
            and existing.status == decision.status
            and _optional_json_equal(existing.abstention_json, abstention_json)
            and _json_equal(existing.dissent_dispositions_json, dissent_json)
            and _json_equal(existing.raw_json, raw_json),
            "agent decision",
            invocation_id,
        )
        return existing

    record = AgentDecisionRecord(
        run_id=run_id,
        agent_invocation_id=invocation_id,
        book_id=book_id,
        book_evaluation_id=book_evaluation_id,
        schema_version=decision.schema_version,
        status=decision.status,
        abstention_classification=(
            None if decision.abstention is None else decision.abstention.classification
        ),
        abstention_json=abstention_json,
        dissent_dispositions_json=dissent_json,
        raw_json=raw_json,
    )
    session.add(record)
    if not commit:
        session.flush()
        return record
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        line = (
            AgentDecisionRecord.book_id.is_(None)
            if book_id is None
            else AgentDecisionRecord.book_id == book_id
        )
        concurrent = session.scalar(
            select(AgentDecisionRecord).where(AgentDecisionRecord.run_id == run_id, line)
        )
        if concurrent is None:
            raise
        _verify_same(
            concurrent.agent_invocation_id == invocation_id
            and concurrent.book_evaluation_id == book_evaluation_id
            and concurrent.schema_version == decision.schema_version
            and concurrent.status == decision.status
            and _optional_json_equal(concurrent.abstention_json, abstention_json)
            and _json_equal(concurrent.dissent_dispositions_json, dissent_json)
            and _json_equal(concurrent.raw_json, raw_json),
            "agent decision",
            invocation_id,
        )
        return concurrent
    return record


def load_completed_agent_decisions(
    session: Session,
    *,
    run_ids: Sequence[str],
    book_id: str | None = None,
) -> tuple[AgentDecisionRecord, ...]:
    """Return decisions whose enclosing live run or exact book evaluation completed."""
    if not run_ids:
        return ()
    statement = select(AgentDecisionRecord).where(AgentDecisionRecord.run_id.in_(run_ids))
    if book_id is None:
        statement = statement.join(Run, Run.id == AgentDecisionRecord.run_id).where(
            AgentDecisionRecord.book_id.is_(None),
            Run.status == "COMPLETED",
        )
    else:
        statement = statement.join(
            BookEvaluation,
            BookEvaluation.id == AgentDecisionRecord.book_evaluation_id,
        ).where(
            AgentDecisionRecord.book_id == book_id,
            BookEvaluation.book_id == book_id,
            BookEvaluation.status == "COMPLETED",
            BookEvaluation.terminal_invocation_id
            == AgentDecisionRecord.agent_invocation_id,
        )
    return tuple(session.scalars(statement.order_by(AgentDecisionRecord.created_at)))


def persist_trade_proposal(
    session: Session,
    run_id: str,
    proposal: TradeProposal,
    *,
    agent_invocation_id: str | None = None,
    book_id: str | None = None,
    commit: bool = True,
) -> TradeProposalRecord:
    """Persist a proposal once; identical retries return the original row.

    ``book_id`` records which decision line the proposal belongs to: ``None`` is the live
    portfolio, a value is a simulated book. ``commit=False`` lets an invocation own the
    transaction so a rejected batch cannot leave a partially persisted decision.
    """
    proposal_id = str(proposal.proposal_id)
    raw_json = proposal.model_dump_json()
    existing = session.get(TradeProposalRecord, proposal_id)
    if existing is not None:
        _verify_same(
            existing.run_id == run_id
            and existing.agent_invocation_id == agent_invocation_id
            and existing.book_id == book_id
            and _json_equal(existing.raw_json, raw_json),
            "trade proposal",
            proposal_id,
        )
        return existing

    record = TradeProposalRecord(
        id=proposal_id,
        run_id=run_id,
        agent_invocation_id=agent_invocation_id,
        book_id=book_id,
        symbol=proposal.symbol,
        action=proposal.action,
        requested_notional=_optional_decimal(proposal.target_notional_usd),
        target_position_pct=_optional_decimal(proposal.target_position_pct),
        confidence=proposal.confidence,
        rationale=proposal.rationale,
        thesis_id=str(proposal.thesis_id) if proposal.thesis_id is not None else None,
        raw_json=raw_json,
    )
    session.add(record)
    if not commit:
        session.flush()
        return record
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = session.get(TradeProposalRecord, proposal_id)
        if concurrent is None:
            raise
        _verify_same(
            concurrent.run_id == run_id
            and concurrent.agent_invocation_id == agent_invocation_id
            and concurrent.book_id == book_id
            and _json_equal(concurrent.raw_json, raw_json),
            "trade proposal",
            proposal_id,
        )
        return concurrent
    return record


def persist_risk_decision(
    session: Session,
    run_id: str,
    decision: RiskDecision,
    *,
    policy_hash: str | None = None,
) -> RiskDecisionRecord:
    """Persist the immutable decision for one run/proposal pair."""
    proposal_id = str(decision.proposal_id)
    reason_codes_json = _canonical_json(decision.rejection_codes)
    normalized_order_json = (
        decision.normalized_order.model_dump_json()
        if decision.normalized_order is not None
        else None
    )
    existing = session.scalar(
        select(RiskDecisionRecord).where(
            RiskDecisionRecord.run_id == run_id,
            RiskDecisionRecord.proposal_id == proposal_id,
        )
    )
    if existing is not None:
        _verify_same(
            existing.approved == decision.approved
            and _json_equal(existing.reason_codes_json, reason_codes_json)
            and _optional_json_equal(existing.normalized_order_json, normalized_order_json)
            and existing.explanation == decision.human_explanation
            and existing.policy_hash == policy_hash,
            "risk decision",
            f"{run_id}/{proposal_id}",
        )
        return existing

    record = RiskDecisionRecord(
        run_id=run_id,
        proposal_id=proposal_id,
        approved=decision.approved,
        reason_codes_json=reason_codes_json,
        normalized_order_json=normalized_order_json,
        explanation=decision.human_explanation,
        policy_hash=policy_hash,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = session.scalar(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.run_id == run_id,
                RiskDecisionRecord.proposal_id == proposal_id,
            )
        )
        if concurrent is None:
            raise
        _verify_same(
            concurrent.approved == decision.approved
            and _json_equal(concurrent.reason_codes_json, reason_codes_json)
            and _optional_json_equal(concurrent.normalized_order_json, normalized_order_json)
            and concurrent.explanation == decision.human_explanation
            and concurrent.policy_hash == policy_hash,
            "risk decision",
            f"{run_id}/{proposal_id}",
        )
        return concurrent
    return record


def record_broker_order_event(
    session: Session,
    *,
    broker_order_record_id: str,
    event_key: str,
    event_type: str,
    status: str,
    occurred_at: datetime | None = None,
    broker_event_id: str | None = None,
    raw_event_path: str | None = None,
    raw_json: str | None = None,
) -> BrokerOrderEvent:
    """Record a broker transition once using a caller-stable idempotency key."""
    existing = session.scalar(
        select(BrokerOrderEvent).where(BrokerOrderEvent.event_key == event_key)
    )
    if existing is not None:
        _verify_same(
            existing.broker_order_record_id == broker_order_record_id
            and existing.event_type == event_type
            and existing.status == status
            and existing.broker_event_id == broker_event_id,
            "broker order event",
            event_key,
        )
        return existing

    values: dict[str, object] = {
        "broker_order_record_id": broker_order_record_id,
        "event_key": event_key,
        "broker_event_id": broker_event_id,
        "event_type": event_type,
        "status": status,
        "raw_event_path": raw_event_path,
        "raw_json": raw_json,
    }
    if occurred_at is not None:
        values["occurred_at"] = occurred_at
    record = BrokerOrderEvent(**values)
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = session.scalar(
            select(BrokerOrderEvent).where(BrokerOrderEvent.event_key == event_key)
        )
        if concurrent is None:
            raise
        _verify_same(
            concurrent.broker_order_record_id == broker_order_record_id
            and concurrent.event_type == event_type
            and concurrent.status == status
            and concurrent.broker_event_id == broker_event_id,
            "broker order event",
            event_key,
        )
        return concurrent
    return record


def record_fill(
    session: Session,
    *,
    broker_order_record_id: str,
    broker_activity_id: str,
    qty: Decimal,
    price: Decimal,
    side: str,
    transaction_time: datetime,
    commission: Decimal | None = None,
    raw_activity_path: str | None = None,
    raw_json: str | None = None,
) -> Fill:
    """Record a fill once using the broker activity identifier."""
    existing = session.scalar(select(Fill).where(Fill.broker_activity_id == broker_activity_id))
    if existing is not None:
        _verify_same(
            existing.broker_order_record_id == broker_order_record_id
            and Decimal(existing.qty) == qty
            and Decimal(existing.price) == price
            and existing.side == side
            and existing.commission == (str(commission) if commission is not None else None),
            "fill",
            broker_activity_id,
        )
        return existing

    record = Fill(
        broker_order_record_id=broker_order_record_id,
        broker_activity_id=broker_activity_id,
        qty=str(qty),
        price=str(price),
        side=side,
        transaction_time=transaction_time,
        commission=str(commission) if commission is not None else None,
        raw_activity_path=raw_activity_path,
        raw_json=raw_json,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = session.scalar(
            select(Fill).where(Fill.broker_activity_id == broker_activity_id)
        )
        if concurrent is None:
            raise
        _verify_same(
            concurrent.broker_order_record_id == broker_order_record_id
            and Decimal(concurrent.qty) == qty
            and Decimal(concurrent.price) == price
            and concurrent.side == side
            and concurrent.commission == (str(commission) if commission is not None else None),
            "fill",
            broker_activity_id,
        )
        return concurrent
    return record


def get_run_audit_trail(session: Session, run_id: str) -> RunAuditTrail:
    """Load the complete immutable record set for a historical run."""
    run = session.get(Run, run_id)
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    orders = _list_by_run(session, BrokerOrderRecord, run_id)
    order_ids = [order.id for order in orders]
    portfolio_snapshots = _list_by_run(session, PortfolioSnapshot, run_id)
    portfolio_snapshot_ids = [item.id for item in portfolio_snapshots]
    research_items = _list_by_run(session, ResearchItem, run_id)
    research_ids = [item.id for item in research_items]
    agent_invocations = _list_by_run(session, AgentInvocation, run_id)
    agent_invocation_ids = [item.id for item in agent_invocations]
    order_events = _list_by_order_ids(session, BrokerOrderEvent, order_ids)
    fills = _list_by_order_ids(session, Fill, order_ids)
    scheduled_run = session.scalar(select(ScheduledRun).where(ScheduledRun.run_id == run_id))
    scheduled_run_events = (
        list(
            session.scalars(
                select(ScheduledRunEvent).where(
                    ScheduledRunEvent.scheduled_run_id == scheduled_run.id
                )
            )
        )
        if scheduled_run is not None
        else []
    )
    market_event = (
        session.get(MarketEvent, scheduled_run.market_event_id)
        if scheduled_run is not None
        else None
    )
    return RunAuditTrail(
        run=run,
        scheduled_run=scheduled_run,
        scheduled_run_events=scheduled_run_events,
        market_event=market_event,
        events=_list_by_run(session, RunEvent, run_id),
        portfolio_snapshots=portfolio_snapshots,
        position_snapshots=_list_by_snapshot_ids(session, portfolio_snapshot_ids),
        market_snapshots=_list_by_run(session, MarketSnapshot, run_id),
        research_items=research_items,
        research_item_symbols=_list_research_symbols(session, research_ids),
        research_item_questions=_list_research_questions(session, research_ids),
        agent_invocations=agent_invocations,
        agent_invocation_evidence=_list_invocation_evidence(
            session,
            agent_invocation_ids,
        ),
        agent_decisions=_list_by_run(session, AgentDecisionRecord, run_id),
        trade_proposals=_list_by_run(session, TradeProposalRecord, run_id),
        risk_decisions=_list_by_run(session, RiskDecisionRecord, run_id),
        broker_orders=orders,
        broker_order_events=order_events,
        fills=fills,
        knowledge_changes=_list_by_run(session, KnowledgeChange, run_id),
        daily_reports=_list_by_run(session, DailyReport, run_id),
        performance_snapshots=_list_by_run(session, PerformanceSnapshot, run_id),
    )


def available_research_as_of(
    session: Session,
    as_of: datetime,
    *,
    run_id: str | None = None,
    symbol: str | None = None,
    source_tiers: Sequence[str] | None = None,
    provider: str | None = None,
    research_question_id: str | None = None,
    limit: int = 100,
) -> list[ResearchItem]:
    """Return a bounded evidence set that was knowable at a historical cutoff."""
    as_of = _aware_utc(as_of, "as_of")
    if isinstance(limit, bool) or not 1 <= limit <= _MAX_RESEARCH_RESULTS:
        raise ValueError(f"limit must be between 1 and {_MAX_RESEARCH_RESULTS}")
    statement = select(ResearchItem).where(
        ResearchItem.published_at <= as_of,
        ResearchItem.retrieved_at <= as_of,
    )
    if run_id is not None:
        statement = statement.where(ResearchItem.run_id == run_id)
    if symbol is not None:
        normalized_symbol = _validate_symbols([symbol])[0]
        statement = statement.join(
            ResearchItemSymbol,
            ResearchItemSymbol.research_id == ResearchItem.id,
        ).where(ResearchItemSymbol.symbol == normalized_symbol)
    if source_tiers is not None:
        if isinstance(source_tiers, str):
            raise TypeError("source_tiers must be a sequence of individual tiers")
        normalized_tiers = tuple(tier.strip().upper() for tier in source_tiers)
        if not normalized_tiers:
            raise ValueError("source_tiers cannot be empty")
        invalid_tiers = sorted(set(normalized_tiers) - RESEARCH_SOURCE_TIERS)
        if invalid_tiers:
            raise ValueError(f"invalid source_tiers: {', '.join(invalid_tiers)}")
        statement = statement.where(ResearchItem.source_tier.in_(normalized_tiers))
    if provider is not None:
        statement = statement.where(ResearchItem.provider == _required_text(provider, "provider"))
    if research_question_id is not None:
        statement = statement.join(
            ResearchItemQuestion,
            ResearchItemQuestion.research_id == ResearchItem.id,
        ).where(
            ResearchItemQuestion.question_id
            == _required_text(research_question_id, "research_question_id")
        )
    statement = statement.order_by(
        ResearchItem.retrieved_at.desc(),
        ResearchItem.published_at.desc(),
        ResearchItem.id,
    ).limit(limit)
    return list(session.scalars(statement))


def decision_history(
    session: Session,
    *,
    symbol: str | None = None,
    as_of: datetime | None = None,
    limit: int = 100,
) -> list[RiskDecisionRecord]:
    """Retrieve risk decisions through their immutable proposal records."""
    statement = select(RiskDecisionRecord).join(
        TradeProposalRecord,
        TradeProposalRecord.id == RiskDecisionRecord.proposal_id,
    )
    if symbol is not None:
        statement = statement.where(TradeProposalRecord.symbol == symbol.upper())
    if as_of is not None:
        statement = statement.where(RiskDecisionRecord.created_at <= as_of)
    statement = statement.order_by(RiskDecisionRecord.created_at.desc()).limit(limit)
    return list(session.scalars(statement))


def _list_by_run[ModelT](session: Session, model: type[ModelT], run_id: str) -> list[ModelT]:
    return list(session.scalars(select(model).where(model.run_id == run_id)))  # type: ignore[attr-defined]


def _list_by_order_ids[ModelT](
    session: Session,
    model: type[ModelT],
    order_ids: list[str],
) -> list[ModelT]:
    if not order_ids:
        return []
    return list(
        session.scalars(
            select(model).where(model.broker_order_record_id.in_(order_ids))  # type: ignore[attr-defined]
        )
    )


def _list_by_snapshot_ids(session: Session, snapshot_ids: list[str]) -> list[PositionSnapshot]:
    if not snapshot_ids:
        return []
    return list(
        session.scalars(
            select(PositionSnapshot).where(
                PositionSnapshot.portfolio_snapshot_id.in_(snapshot_ids)
            )
        )
    )


def _list_research_symbols(
    session: Session,
    research_ids: list[str],
) -> list[ResearchItemSymbol]:
    if not research_ids:
        return []
    return list(
        session.scalars(
            select(ResearchItemSymbol)
            .where(ResearchItemSymbol.research_id.in_(research_ids))
            .order_by(ResearchItemSymbol.research_id, ResearchItemSymbol.symbol)
        )
    )


def _list_research_questions(
    session: Session,
    research_ids: list[str],
) -> list[ResearchItemQuestion]:
    if not research_ids:
        return []
    return list(
        session.scalars(
            select(ResearchItemQuestion)
            .where(ResearchItemQuestion.research_id.in_(research_ids))
            .order_by(ResearchItemQuestion.research_id, ResearchItemQuestion.question_id)
        )
    )


def _list_invocation_evidence(
    session: Session,
    invocation_ids: list[str],
) -> list[AgentInvocationEvidence]:
    if not invocation_ids:
        return []
    return list(
        session.scalars(
            select(AgentInvocationEvidence)
            .where(AgentInvocationEvidence.agent_invocation_id.in_(invocation_ids))
            .order_by(
                AgentInvocationEvidence.agent_invocation_id,
                AgentInvocationEvidence.ordinal,
            )
        )
    )


def _invocation_evidence(
    session: Session,
    invocation_id: str,
) -> list[AgentInvocationEvidence]:
    return list(
        session.scalars(
            select(AgentInvocationEvidence)
            .where(AgentInvocationEvidence.agent_invocation_id == invocation_id)
            .order_by(AgentInvocationEvidence.ordinal)
        )
    )


def _research_natural_key_match(
    session: Session,
    *,
    run_id: str,
    provider: str,
    provider_item_id: str,
    content_hash: str,
) -> ResearchItem | None:
    del provider, provider_item_id
    by_content = session.scalar(
        select(ResearchItem).where(
            ResearchItem.run_id == run_id,
            ResearchItem.content_hash == content_hash,
        )
    )
    return by_content


def _verify_research_item(
    session: Session,
    item: ResearchItem,
    *,
    research_id: str,
    run_id: str,
    source_tier: str,
    source_type: str,
    source_name: str,
    provider: str,
    provider_item_id: str,
    raw_artifact_path: str,
    normalized_summary: str,
    normalized_text: str,
    published_at: datetime,
    retrieved_at: datetime,
    content_hash: str,
    headline: str,
    url: str | None,
    author: str | None,
    metadata_json: str | None,
    cost_usd: str | None,
) -> None:
    del session
    _verify_same(
        item.run_id == run_id
        and item.source_tier == source_tier
        and item.source_type == source_type
        and item.source_name == source_name
        and item.provider == provider
        and item.provider_item_id == provider_item_id
        and item.raw_artifact_path == raw_artifact_path
        and item.normalized_summary == normalized_summary
        and item.normalized_text == normalized_text
        and _same_timestamp(item.published_at, published_at)
        and _same_timestamp(item.retrieved_at, retrieved_at)
        and item.content_hash == content_hash
        and item.headline == headline
        and item.url == url
        and item.author == author
        and _optional_json_equal(item.metadata_json, metadata_json)
        and item.cost_usd == cost_usd,
        "research item",
        research_id,
    )


def _associate_research_context(
    session: Session,
    *,
    research_id: str,
    symbols: tuple[str, ...],
    research_question_id: str,
    research_question: str,
) -> None:
    existing_symbols = set(
        session.scalars(
            select(ResearchItemSymbol.symbol).where(
                ResearchItemSymbol.research_id == research_id
            )
        )
    )
    question = session.get(ResearchItemQuestion, (research_id, research_question_id))
    if question is not None and question.question_text != research_question:
        raise PersistenceConflictError(
            "research question idempotency conflict for "
            f"{research_id}/{research_question_id}"
        )
    session.add_all(
        [
            ResearchItemSymbol(research_id=research_id, symbol=symbol)
            for symbol in symbols
            if symbol not in existing_symbols
        ]
    )
    if question is None:
        session.add(
            ResearchItemQuestion(
                research_id=research_id,
                question_id=research_question_id,
                question_text=research_question,
            )
        )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        stored_symbols = set(
            session.scalars(
                select(ResearchItemSymbol.symbol).where(
                    ResearchItemSymbol.research_id == research_id
                )
            )
        )
        concurrent_question = session.get(
            ResearchItemQuestion,
            (research_id, research_question_id),
        )
        _verify_same(
            set(symbols).issubset(stored_symbols)
            and concurrent_question is not None
            and concurrent_question.question_text == research_question,
            "research context",
            research_id,
        )


def _validate_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    if isinstance(symbols, str):
        raise TypeError("symbols must be a sequence of individual symbols")
    normalized = tuple(symbol.strip().upper() for symbol in symbols)
    if not normalized:
        raise ValueError("symbols cannot be empty")
    if len(normalized) > _MAX_RESEARCH_SYMBOLS:
        raise ValueError(f"symbols cannot contain more than {_MAX_RESEARCH_SYMBOLS} items")
    if len(set(normalized)) != len(normalized):
        raise ValueError("symbols must be unique")
    invalid = [symbol for symbol in normalized if _SYMBOL_PATTERN.fullmatch(symbol) is None]
    if invalid:
        raise ValueError(f"invalid symbols: {', '.join(invalid)}")
    return tuple(sorted(normalized))


def _required_text(value: str, field: str, *, max_length: int | None = None) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} cannot be empty")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{field} cannot exceed {max_length} characters")
    return normalized


def _optional_bounded_text(
    value: str | None,
    field: str,
    *,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, max_length=max_length)


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _same_timestamp(left: datetime, right: datetime) -> bool:
    normalized_left = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
    normalized_right = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    return normalized_left == normalized_right


def _cost_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError("cost_usd must be a finite non-negative decimal")
    return format(value.normalize(), "f")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_equal(left: str, right: str) -> bool:
    return bool(json.loads(left) == json.loads(right))


def _optional_json_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    return _json_equal(left, right)


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _verify_same(condition: bool, record_type: str, key: str) -> None:
    if not condition:
        raise PersistenceConflictError(f"{record_type} idempotency conflict for {key}")
