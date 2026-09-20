"""Deterministic ledger writers.

Every function here is driven by observable state: risk-approved proposals, broker-reported
positions, and persisted account snapshots. Free-text invalidation conditions are retained for a
later reviewer but are never interpreted by this module.
"""

import hashlib
import json
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.models import TradeProposal
from trader.broker.models import Account
from trader.ledger.models import LedgerWriteSummary, PerformanceMetrics
from trader.persistence.models import (
    DailyReport,
    KnowledgeChange,
    PerformanceSnapshot,
    ResearchItem,
    Thesis,
    ThesisEvidence,
    TradeProposalRecord,
)
from trader.persistence.repositories import PersistenceConflictError

THESIS_OPENED = "THESIS_OPENED"
THESIS_UPDATED = "THESIS_UPDATED"
THESIS_CLOSED_ON_EXIT = "THESIS_CLOSED_ON_EXIT"
THESIS_CLOSED_NO_POSITION = "THESIS_CLOSED_NO_POSITION"

DECISION_CHANGE_TYPES = frozenset({THESIS_OPENED, THESIS_UPDATED, THESIS_CLOSED_ON_EXIT})
THESIS_ENTITY_TYPE = "thesis"
MAX_THESIS_TITLE_CHARS = 200
MAX_THESIS_SUMMARY_CHARS = 4_000
MAX_REPORT_SUMMARY_CHARS = 4_000
_PERCENT = Decimal("100")
_QUANTUM = Decimal("0.000001")


def close_theses_without_positions(
    session: Session,
    *,
    run_id: str,
    held_symbols: frozenset[str],
    as_of: datetime,
) -> tuple[str, ...]:
    """Close active theses for symbols the portfolio no longer holds.

    A thesis is a reason to hold something, so it cannot outlive the position. The caller must
    already have established that no orders are open; otherwise a pending entry would be mistaken
    for an abandoned one.
    """
    if _has_change(session, run_id, frozenset({THESIS_CLOSED_NO_POSITION})):
        raise PersistenceConflictError(
            f"thesis position reconciliation already ran for run {run_id}"
        )
    normalized = frozenset(symbol.upper().strip() for symbol in held_symbols)
    closed: list[str] = []
    for thesis in _active_theses(session):
        if thesis.symbol.upper().strip() in normalized:
            continue
        before = _thesis_state_text(thesis)
        thesis.status = "closed"
        thesis.updated_at = as_of
        session.add(
            _change(
                run_id=run_id,
                thesis=thesis,
                change_type=THESIS_CLOSED_NO_POSITION,
                before_text=before,
                reason=(
                    f"Portfolio no longer holds {thesis.symbol} and no order is open, "
                    "so the thesis has no position to justify."
                ),
                as_of=as_of,
            )
        )
        closed.append(thesis.id)
    session.commit()
    return tuple(closed)


def record_decision_ledger(
    session: Session,
    *,
    run_id: str,
    as_of: datetime,
    proposals: tuple[TradeProposal, ...],
    approved_proposal_ids: frozenset[str],
) -> LedgerWriteSummary:
    """Open, update, or close theses from the proposals deterministic risk actually approved."""
    if _has_change(session, run_id, DECISION_CHANGE_TYPES):
        raise PersistenceConflictError(f"decision ledger already recorded for run {run_id}")
    admitted_research = _research_ids_for_run(
        session,
        run_id=run_id,
        cited={research_id for proposal in proposals for research_id in proposal.evidence_ids},
    )
    active = {thesis.symbol.upper(): thesis for thesis in _active_theses(session)}
    opened: list[str] = []
    updated: list[str] = []
    closed: list[str] = []
    linked = 0
    unlinked = 0

    ordered = sorted(proposals, key=lambda item: (item.symbol, str(item.proposal_id)))
    for proposal in ordered:
        if str(proposal.proposal_id) not in approved_proposal_ids:
            continue
        linkable = tuple(
            research_id
            for research_id in proposal.evidence_ids
            if research_id in admitted_research
        )
        unlinked += len(proposal.evidence_ids) - len(linkable)
        existing = active.get(proposal.symbol.upper())

        if proposal.action == "BUY":
            if existing is None:
                thesis = _open_thesis(session, run_id=run_id, proposal=proposal, as_of=as_of)
                active[proposal.symbol.upper()] = thesis
                opened.append(thesis.id)
            else:
                thesis = existing
                _update_thesis(
                    session,
                    run_id=run_id,
                    thesis=thesis,
                    proposal=proposal,
                    as_of=as_of,
                )
                updated.append(thesis.id)
        elif proposal.action == "SELL":
            if existing is None:
                continue
            thesis = existing
            if _is_full_exit(proposal):
                _close_thesis(
                    session,
                    run_id=run_id,
                    thesis=thesis,
                    proposal=proposal,
                    as_of=as_of,
                )
                active.pop(proposal.symbol.upper(), None)
                closed.append(thesis.id)
            else:
                _update_thesis(
                    session,
                    run_id=run_id,
                    thesis=thesis,
                    proposal=proposal,
                    as_of=as_of,
                )
                updated.append(thesis.id)
        else:
            continue

        linked += _link_evidence(
            session,
            thesis_id=thesis.id,
            research_ids=linkable,
            explanation=proposal.rationale,
            as_of=as_of,
        )
        _attach_proposal(session, proposal_id=str(proposal.proposal_id), thesis_id=thesis.id)

    session.commit()
    return LedgerWriteSummary(
        opened_thesis_ids=tuple(opened),
        updated_thesis_ids=tuple(dict.fromkeys(updated)),
        closed_thesis_ids=tuple(closed),
        linked_evidence_count=linked,
        unlinked_evidence_count=unlinked,
    )


def record_performance_snapshot(
    session: Session,
    *,
    run_id: str,
    account: Account,
    as_of: datetime,
    period: str = "daily",
    book_id: str | None = None,
) -> PerformanceSnapshot:
    """Persist one run's equity-curve metrics, carrying the running peak forward.

    ``book_id`` selects the equity curve being extended: ``None`` is the live paper account, and a
    value is one simulated book. Each curve carries its own peak, so a variant's drawdown is
    measured against the variant's own history rather than the portfolio's.
    """
    if not account.equity.is_finite() or not account.cash.is_finite():
        raise ValueError("account equity and cash must be finite to record performance")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("performance snapshot cutoff must be timezone-aware")
    same_curve = (
        PerformanceSnapshot.book_id.is_(None)
        if book_id is None
        else PerformanceSnapshot.book_id == book_id
    )
    existing = session.scalar(
        select(PerformanceSnapshot).where(
            PerformanceSnapshot.run_id == run_id,
            PerformanceSnapshot.period == period,
            same_curve,
        )
    )
    if existing is not None:
        if Decimal(existing.equity) != account.equity:
            raise PersistenceConflictError(
                f"performance snapshot idempotency conflict for {run_id}/{period}"
            )
        return existing

    prior = session.scalar(
        select(PerformanceSnapshot)
        .where(
            PerformanceSnapshot.run_id != run_id,
            PerformanceSnapshot.period == period,
            PerformanceSnapshot.captured_at < as_of,
            same_curve,
        )
        .order_by(PerformanceSnapshot.captured_at.desc(), PerformanceSnapshot.id.desc())
        .limit(1)
    )
    metrics = _metrics(account, prior)
    record = PerformanceSnapshot(
        run_id=run_id,
        book_id=book_id,
        period=period,
        captured_at=as_of,
        equity=str(metrics.equity),
        cash=str(metrics.cash),
        pnl=None if metrics.pnl is None else str(metrics.pnl),
        return_pct=None if metrics.return_pct is None else str(metrics.return_pct),
        drawdown_pct=str(metrics.drawdown_pct),
        raw_json=json.dumps(metrics.raw_payload(), sort_keys=True),
    )
    session.add(record)
    session.commit()
    return record


def record_run_report(
    session: Session,
    *,
    run_id: str,
    report_path: str,
    content: str,
    summary: str,
    version: int = 1,
) -> DailyReport:
    """Make any run's report queryable by recording its path, hash, and decision summary.

    The table is still named ``daily_reports`` for schema compatibility, but the row is generic:
    weekly reviews record theirs here too, so one query answers "what did this run conclude?".
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    existing = session.scalar(
        select(DailyReport).where(
            DailyReport.run_id == run_id,
            DailyReport.version == version,
        )
    )
    if existing is not None:
        if existing.content_hash != content_hash:
            raise PersistenceConflictError(
                f"daily report idempotency conflict for {run_id}/{version}"
            )
        return existing
    record = DailyReport(
        run_id=run_id,
        version=version,
        report_path=report_path,
        content_hash=content_hash,
        summary=_truncate(summary, MAX_REPORT_SUMMARY_CHARS),
    )
    session.add(record)
    session.commit()
    return record


def snapshot_peak_equity(record: PerformanceSnapshot) -> Decimal:
    """Return the running equity peak carried alongside a performance snapshot."""
    return _prior_peak(record, _parse_decimal(record.equity))


def _open_thesis(
    session: Session,
    *,
    run_id: str,
    proposal: TradeProposal,
    as_of: datetime,
) -> Thesis:
    thesis = Thesis(
        symbol=proposal.symbol,
        title=_thesis_title(proposal),
        status="active",
        summary=_thesis_summary(proposal),
        confidence=proposal.confidence,
        invalidation_json=json.dumps(list(proposal.invalidation_conditions)),
        created_at=as_of,
        updated_at=as_of,
    )
    session.add(thesis)
    session.flush()
    session.add(
        _change(
            run_id=run_id,
            thesis=thesis,
            change_type=THESIS_OPENED,
            before_text="",
            reason=f"Risk approved a {proposal.action} for {proposal.symbol}.",
            as_of=as_of,
            evidence_ids=proposal.evidence_ids,
        )
    )
    return thesis


def _update_thesis(
    session: Session,
    *,
    run_id: str,
    thesis: Thesis,
    proposal: TradeProposal,
    as_of: datetime,
) -> None:
    before = _thesis_state_text(thesis)
    thesis.summary = _thesis_summary(proposal)
    thesis.confidence = proposal.confidence
    thesis.invalidation_json = json.dumps(list(proposal.invalidation_conditions))
    thesis.updated_at = as_of
    session.add(
        _change(
            run_id=run_id,
            thesis=thesis,
            change_type=THESIS_UPDATED,
            before_text=before,
            reason=(
                f"Risk approved a further {proposal.action} for {proposal.symbol}; "
                "the thesis was restated from the new proposal."
            ),
            as_of=as_of,
            evidence_ids=proposal.evidence_ids,
        )
    )


def _close_thesis(
    session: Session,
    *,
    run_id: str,
    thesis: Thesis,
    proposal: TradeProposal,
    as_of: datetime,
) -> None:
    before = _thesis_state_text(thesis)
    thesis.status = "closed"
    thesis.updated_at = as_of
    session.add(
        _change(
            run_id=run_id,
            thesis=thesis,
            change_type=THESIS_CLOSED_ON_EXIT,
            before_text=before,
            reason=f"Risk approved a full exit of {proposal.symbol}.",
            as_of=as_of,
            evidence_ids=proposal.evidence_ids,
        )
    )


def _link_evidence(
    session: Session,
    *,
    thesis_id: str,
    research_ids: tuple[str, ...],
    explanation: str,
    as_of: datetime,
) -> int:
    if not research_ids:
        return 0
    already = set(
        session.scalars(
            select(ThesisEvidence.research_id).where(ThesisEvidence.thesis_id == thesis_id)
        )
    )
    new_ids = [research_id for research_id in research_ids if research_id not in already]
    session.add_all(
        [
            ThesisEvidence(
                thesis_id=thesis_id,
                research_id=research_id,
                relationship="supporting",
                agent_explanation=_truncate(explanation, MAX_THESIS_SUMMARY_CHARS),
                created_at=as_of,
            )
            for research_id in new_ids
        ]
    )
    return len(new_ids)


def _attach_proposal(session: Session, *, proposal_id: str, thesis_id: str) -> None:
    record = session.get(TradeProposalRecord, proposal_id)
    if record is not None and record.thesis_id is None:
        record.thesis_id = thesis_id


def _change(
    *,
    run_id: str,
    thesis: Thesis,
    change_type: str,
    before_text: str,
    reason: str,
    as_of: datetime,
    evidence_ids: list[str] | None = None,
) -> KnowledgeChange:
    return KnowledgeChange(
        run_id=run_id,
        entity_type=THESIS_ENTITY_TYPE,
        entity_id=thesis.id,
        change_type=change_type,
        before_text=before_text,
        after_text=_thesis_state_text(thesis),
        reason=reason,
        evidence_ids_json=json.dumps(sorted(evidence_ids or [])),
        created_at=as_of,
    )


def _active_theses(session: Session) -> list[Thesis]:
    return list(
        session.scalars(
            select(Thesis).where(Thesis.status == "active").order_by(Thesis.symbol, Thesis.id)
        )
    )


def _research_ids_for_run(
    session: Session,
    *,
    run_id: str,
    cited: set[str],
) -> frozenset[str]:
    """Return only cited evidence that exists in this run, so a link can never dangle."""
    if not cited:
        return frozenset()
    return frozenset(
        session.scalars(
            select(ResearchItem.id).where(
                ResearchItem.run_id == run_id,
                ResearchItem.id.in_(sorted(cited)),
            )
        )
    )


def _has_change(session: Session, run_id: str, change_types: frozenset[str]) -> bool:
    return (
        session.scalar(
            select(KnowledgeChange.id)
            .where(
                KnowledgeChange.run_id == run_id,
                KnowledgeChange.change_type.in_(sorted(change_types)),
            )
            .limit(1)
        )
        is not None
    )


def _is_full_exit(proposal: TradeProposal) -> bool:
    return (
        proposal.target_position_pct is not None
        and proposal.target_position_pct == Decimal("0")
    )


def _metrics(account: Account, prior: PerformanceSnapshot | None) -> PerformanceMetrics:
    equity = account.equity
    if prior is None:
        return PerformanceMetrics(
            equity=equity,
            cash=account.cash,
            peak_equity=equity,
            drawdown_pct=Decimal("0"),
        )
    prior_equity = _parse_decimal(prior.equity)
    peak = max(_prior_peak(prior, prior_equity), equity)
    pnl = equity - prior_equity if prior_equity is not None else None
    return_pct = (
        _quantize(pnl * _PERCENT / prior_equity)
        if pnl is not None and prior_equity is not None and prior_equity > 0
        else None
    )
    drawdown = (
        _quantize((peak - equity) * _PERCENT / peak)
        if peak > 0 and equity < peak
        else Decimal("0")
    )
    return PerformanceMetrics(
        equity=equity,
        cash=account.cash,
        peak_equity=peak,
        drawdown_pct=drawdown,
        pnl=pnl,
        return_pct=return_pct,
    )


def _prior_peak(prior: PerformanceSnapshot, prior_equity: Decimal | None) -> Decimal:
    fallback = prior_equity if prior_equity is not None else Decimal("0")
    if prior.raw_json is None:
        return fallback
    try:
        payload = json.loads(prior.raw_json)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    recorded = _parse_decimal(payload.get("peak_equity"))
    return recorded if recorded is not None else fallback


def _parse_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _thesis_title(proposal: TradeProposal) -> str:
    lines = proposal.rationale.strip().splitlines()
    headline = lines[0].strip() if lines else ""
    if not headline:
        headline = f"{proposal.time_horizon} thesis"
    return _truncate(f"{proposal.symbol} — {headline}", MAX_THESIS_TITLE_CHARS)


def _thesis_summary(proposal: TradeProposal) -> str:
    lines = [
        f"Rationale: {proposal.rationale.strip()}",
        f"Horizon: {proposal.time_horizon}",
        f"Confidence at entry: {proposal.confidence}",
    ]
    if proposal.catalysts:
        lines.append("Catalysts: " + "; ".join(item.strip() for item in proposal.catalysts))
    if proposal.key_risks:
        lines.append("Key risks: " + "; ".join(item.strip() for item in proposal.key_risks))
    return _truncate("\n".join(lines), MAX_THESIS_SUMMARY_CHARS)


def _thesis_state_text(thesis: Thesis) -> str:
    return "\n".join(
        [
            f"status={thesis.status}",
            f"confidence={thesis.confidence}",
            f"invalidation={thesis.invalidation_json}",
            f"summary={thesis.summary}",
        ]
    )


def _truncate(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 16)].rstrip() + "\n[TRUNCATED]"
