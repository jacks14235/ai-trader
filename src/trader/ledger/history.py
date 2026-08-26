"""Bounded, cutoff-respecting reads of the decision ledger for reasoning context."""

import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from trader.ledger.models import (
    DecisionOutcome,
    OpenThesis,
    ProposalAction,
    RecentRunDecision,
)
from trader.persistence.models import (
    BrokerOrderRecord,
    DailyReport,
    Fill,
    PerformanceSnapshot,
    RiskDecisionRecord,
    Run,
    SimulatedFill,
    Thesis,
    TradeProposalRecord,
)

DAILY_RUN_KEY_PREFIXES: tuple[str, ...] = ("daily:", "daily-test:")
"""Only the daily decision line is memory; event runs and canaries are not decisions."""

MAX_RECENT_RUNS = 5
MAX_PROPOSALS_PER_RUN = 10
MAX_OPEN_THESES = 25
MAX_RATIONALE_CHARS = 800
MAX_SUMMARY_CHARS = 1_200
MAX_INVALIDATION_CONDITIONS = 8
MAX_EQUITY_POINTS = 30


def load_recent_decisions(
    session: Session,
    *,
    exclude_run_id: str,
    as_of: datetime,
    max_runs: int = MAX_RECENT_RUNS,
    max_proposals_per_run: int = MAX_PROPOSALS_PER_RUN,
    book_id: str | None = None,
) -> tuple[RecentRunDecision, ...]:
    """Return the most recent completed daily decisions knowable at ``as_of``.

    Runs scheduled after the cutoff are excluded so a replayed run can never see a decision
    that had not yet been made.

    ``book_id`` selects the decision line: ``None`` is the live portfolio and a value is one
    simulated book. Without it a variant would recall the incumbent's trades as its own, which is
    exactly the comparison a book exists to avoid contaminating.
    """
    if max_runs <= 0 or max_proposals_per_run <= 0:
        raise ValueError("recent-decision limits must be positive")
    same_line = (
        TradeProposalRecord.book_id.is_(None)
        if book_id is None
        else TradeProposalRecord.book_id == book_id
    )
    runs = list(
        session.scalars(
            select(Run)
            .where(
                Run.id != exclude_run_id,
                Run.status == "COMPLETED",
                Run.scheduled_for <= as_of,
                or_(*(Run.run_key.startswith(prefix) for prefix in DAILY_RUN_KEY_PREFIXES)),
            )
            .order_by(Run.scheduled_for.desc(), Run.run_key.desc())
            .limit(max_runs)
        )
    )
    if not runs:
        return ()
    run_ids = [run.id for run in runs]

    proposals_by_run: dict[str, list[TradeProposalRecord]] = defaultdict(list)
    for proposal in session.scalars(
        select(TradeProposalRecord)
        .where(TradeProposalRecord.run_id.in_(run_ids), same_line)
        .order_by(
            TradeProposalRecord.run_id,
            TradeProposalRecord.created_at,
            TradeProposalRecord.id,
        )
    ):
        proposals_by_run[proposal.run_id].append(proposal)

    risk_by_proposal = {
        (record.run_id, record.proposal_id): record
        for record in session.scalars(
            select(RiskDecisionRecord)
            .join(
                TradeProposalRecord,
                TradeProposalRecord.id == RiskDecisionRecord.proposal_id,
            )
            .where(RiskDecisionRecord.run_id.in_(run_ids), same_line)
        )
    }
    orders_by_proposal: dict[tuple[str, str], BrokerOrderRecord] = {}
    simulated_by_proposal: dict[str, SimulatedFill] = {}
    if book_id is None:
        for order in session.scalars(
            select(BrokerOrderRecord)
            .where(BrokerOrderRecord.run_id.in_(run_ids))
            .order_by(BrokerOrderRecord.created_at)
        ):
            orders_by_proposal[(order.run_id, order.proposal_id)] = order
    else:
        # A book never reaches a broker, so its execution memory is its simulated settlement.
        simulated_by_proposal = {
            record.proposal_id: record
            for record in session.scalars(
                select(SimulatedFill).where(
                    SimulatedFill.book_id == book_id,
                    SimulatedFill.run_id.in_(run_ids),
                )
            )
        }
    filled_by_order = _filled_quantities(
        session,
        [order.id for order in orders_by_proposal.values()],
    )
    reports_by_run = {
        report.run_id: report
        for report in session.scalars(
            select(DailyReport).where(DailyReport.run_id.in_(run_ids), DailyReport.version == 1)
        )
    }

    records: list[RecentRunDecision] = []
    for run in runs:
        outcomes: list[DecisionOutcome] = []
        for proposal in proposals_by_run[run.id][:max_proposals_per_run]:
            risk = risk_by_proposal.get((run.id, proposal.id))
            order_record = orders_by_proposal.get((run.id, proposal.id))
            simulated = simulated_by_proposal.get(proposal.id)
            outcomes.append(
                DecisionOutcome(
                    proposal_id=proposal.id,
                    symbol=proposal.symbol,
                    action=_action(proposal.action),
                    target_notional_usd=_decimal(proposal.requested_notional),
                    target_position_pct=_decimal(proposal.target_position_pct),
                    confidence=proposal.confidence,
                    rationale=_truncate(proposal.rationale, MAX_RATIONALE_CHARS),
                    risk_approved=None if risk is None else risk.approved,
                    risk_rejection_codes=(
                        () if risk is None else _string_tuple(risk.reason_codes_json)
                    ),
                    order_status=_order_status(
                        order_record,
                        simulated,
                        risk,
                        simulated_line=book_id is not None,
                    ),
                    filled_qty=(
                        _decimal(simulated.qty)
                        if simulated is not None
                        else (
                            None
                            if order_record is None
                            else filled_by_order.get(order_record.id)
                        )
                    ),
                )
            )
        report = reports_by_run.get(run.id)
        records.append(
            RecentRunDecision(
                run_id=run.id,
                run_key=run.run_key,
                scheduled_for=run.scheduled_for,
                decision_summary=(
                    None
                    if report is None or report.summary is None
                    else _truncate(report.summary, MAX_SUMMARY_CHARS)
                ),
                proposals=tuple(outcomes),
            )
        )
    return tuple(records)


def load_equity_curve(
    session: Session,
    *,
    period: str = "daily",
    max_points: int = MAX_EQUITY_POINTS,
    book_id: str | None = None,
) -> tuple[tuple[datetime, Decimal], ...]:
    """Return recent equity points for one line's charts, oldest first.

    ``book_id`` selects the curve: ``None`` is the live paper account, a value is one book.
    """
    if max_points <= 0:
        raise ValueError("equity-curve limit must be positive")
    rows = list(
        session.scalars(
            select(PerformanceSnapshot)
            .where(
                PerformanceSnapshot.period == period,
                (
                    PerformanceSnapshot.book_id.is_(None)
                    if book_id is None
                    else PerformanceSnapshot.book_id == book_id
                ),
            )
            .order_by(
                PerformanceSnapshot.captured_at.desc(),
                PerformanceSnapshot.id.desc(),
            )
            .limit(max_points)
        )
    )
    points: list[tuple[datetime, Decimal]] = []
    for row in reversed(rows):
        equity = _decimal(row.equity)
        if equity is None:
            continue
        captured = row.captured_at
        if captured.tzinfo is None or captured.utcoffset() is None:
            captured = captured.replace(tzinfo=UTC)
        else:
            captured = captured.astimezone(UTC)
        points.append((captured, equity))
    return tuple(points)


def load_open_theses(
    session: Session,
    *,
    symbols: frozenset[str] | None = None,
    max_theses: int = MAX_OPEN_THESES,
) -> tuple[OpenThesis, ...]:
    """Return active theses, optionally restricted to a set of admitted symbols."""
    if max_theses <= 0:
        raise ValueError("open-thesis limit must be positive")
    statement = select(Thesis).where(Thesis.status == "active")
    if symbols is not None:
        if not symbols:
            return ()
        statement = statement.where(Thesis.symbol.in_(sorted(symbols)))
    records = session.scalars(
        statement.order_by(Thesis.symbol, Thesis.created_at, Thesis.id).limit(max_theses)
    )
    return tuple(
        OpenThesis(
            thesis_id=record.id,
            symbol=record.symbol,
            title=_truncate(record.title, 500),
            summary=_truncate(record.summary, MAX_SUMMARY_CHARS),
            confidence=record.confidence,
            invalidation_conditions=_string_tuple(record.invalidation_json)[
                :MAX_INVALIDATION_CONDITIONS
            ],
            opened_at=record.created_at,
            updated_at=record.updated_at,
        )
        for record in records
    )


def _order_status(
    order_record: BrokerOrderRecord | None,
    simulated: SimulatedFill | None,
    risk: RiskDecisionRecord | None,
    *,
    simulated_line: bool,
) -> str | None:
    """Describe what became of a proposal, distinguishing a real fill from a modeled one.

    On the live line an approved proposal with no order simply was not executed, which is not the
    same claim as a book's order failing to settle, so the two are never labeled alike.
    """
    if order_record is not None:
        return order_record.status
    if not simulated_line:
        return None
    if simulated is not None:
        return "SIMULATED_FILLED"
    return "SIMULATED_UNFILLED" if risk is not None and risk.approved else None


def _filled_quantities(session: Session, order_ids: list[str]) -> dict[str, Decimal]:
    if not order_ids:
        return {}
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for fill in session.scalars(select(Fill).where(Fill.broker_order_record_id.in_(order_ids))):
        quantity = _decimal(fill.qty)
        if quantity is not None:
            totals[fill.broker_order_record_id] += quantity
    return dict(totals)


def _action(value: str) -> ProposalAction:
    normalized = value.strip().upper()
    if normalized == "BUY":
        return "BUY"
    if normalized == "SELL":
        return "SELL"
    if normalized == "HOLD":
        return "HOLD"
    raise ValueError(f"persisted proposal action is not recognized: {value}")


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _string_tuple(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def _truncate(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 16)].rstrip() + "\n[TRUNCATED]"
