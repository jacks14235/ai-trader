"""Aggregate one review period from records that already exist.

Nothing here calls a model, a broker, or a clock. Every number is derived from persisted
snapshots, proposals, risk decisions, and broker-reported fills, so a review can be recomputed
from the database alone and two reviewers of the same period see the same figures.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trader.ledger.models import (
    EquityPoint,
    RejectionTally,
    ThesisOutcome,
    WeeklyPerformance,
)
from trader.ledger.service import (
    THESIS_CLOSED_NO_POSITION,
    THESIS_CLOSED_ON_EXIT,
    THESIS_OPENED,
    snapshot_peak_equity,
)
from trader.ledger.strategy import strategy_versions_in_effect
from trader.persistence.models import (
    AgentDecisionRecord,
    BrokerOrderRecord,
    Fill,
    KnowledgeChange,
    PerformanceSnapshot,
    RiskDecisionRecord,
    Run,
    Thesis,
    TradeProposalRecord,
)

DAILY_RUN_KEY_PREFIXES: tuple[str, ...] = ("daily:", "daily-test:")
CLOSURE_CHANGE_TYPES: tuple[str, ...] = (THESIS_CLOSED_ON_EXIT, THESIS_CLOSED_NO_POSITION)
MAX_EQUITY_POINTS = 40
MAX_THESIS_OUTCOMES = 40
MAX_TITLE_CHARS = 500
_PERCENT = Decimal("100")
_QUANTUM = Decimal("0.000001")


def load_weekly_performance(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
    max_equity_points: int = MAX_EQUITY_POINTS,
    max_thesis_outcomes: int = MAX_THESIS_OUTCOMES,
) -> WeeklyPerformance:
    """Aggregate everything decided, held, and earned in ``[period_start, period_end)``."""
    if period_end <= period_start:
        raise ValueError("review period must end after it starts")
    if max_equity_points <= 0 or max_thesis_outcomes <= 0:
        raise ValueError("review period limits must be positive")
    start = _utc(period_start)
    end = _utc(period_end)

    runs = list(
        session.scalars(
            select(Run)
            .where(
                Run.scheduled_for >= start,
                Run.scheduled_for < end,
                or_(*(Run.run_key.startswith(prefix) for prefix in DAILY_RUN_KEY_PREFIXES)),
            )
            .order_by(Run.scheduled_for, Run.run_key)
        )
    )
    run_ids = [run.id for run in runs]

    snapshots = list(
        session.scalars(
            select(PerformanceSnapshot)
            .where(
                PerformanceSnapshot.period == "daily",
                PerformanceSnapshot.book_id.is_(None),
                PerformanceSnapshot.captured_at >= start,
                PerformanceSnapshot.captured_at < end,
            )
            .order_by(PerformanceSnapshot.captured_at, PerformanceSnapshot.id)
        )
    )
    baseline = session.scalar(
        select(PerformanceSnapshot)
        .where(
            PerformanceSnapshot.period == "daily",
            PerformanceSnapshot.book_id.is_(None),
            PerformanceSnapshot.captured_at < start,
        )
        .order_by(PerformanceSnapshot.captured_at.desc(), PerformanceSnapshot.id.desc())
        .limit(1)
    )
    equity_points = tuple(
        EquityPoint(
            captured_at=_utc(record.captured_at),
            equity=equity,
            drawdown_pct=_decimal(record.drawdown_pct),
        )
        for record in snapshots[-max_equity_points:]
        if (equity := _decimal(record.equity)) is not None
    )

    starting_equity = _decimal(baseline.equity) if baseline is not None else None
    if starting_equity is None and equity_points:
        starting_equity = equity_points[0].equity
    ending_equity = equity_points[-1].equity if equity_points else None
    pnl = (
        ending_equity - starting_equity
        if ending_equity is not None and starting_equity is not None
        else None
    )
    return_pct = (
        _quantize(pnl * _PERCENT / starting_equity)
        if pnl is not None and starting_equity is not None and starting_equity > 0
        else None
    )
    peaks = [snapshot_peak_equity(record) for record in snapshots]
    drawdowns = [point.drawdown_pct for point in equity_points if point.drawdown_pct is not None]

    # The strategist reviews the portfolio's own decisions. Simulated books share these runs, so
    # every count here is restricted to the live line or a variant's activity would be reported as
    # the portfolio's own.
    proposals = (
        list(
            session.scalars(
                select(TradeProposalRecord)
                .where(
                    TradeProposalRecord.run_id.in_(run_ids),
                    TradeProposalRecord.book_id.is_(None),
                )
                .order_by(TradeProposalRecord.created_at, TradeProposalRecord.id)
            )
        )
        if run_ids
        else []
    )
    decisions = (
        list(
            session.scalars(
                select(RiskDecisionRecord)
                .join(
                    TradeProposalRecord,
                    TradeProposalRecord.id == RiskDecisionRecord.proposal_id,
                )
                .where(
                    RiskDecisionRecord.run_id.in_(run_ids),
                    TradeProposalRecord.book_id.is_(None),
                )
            )
        )
        if run_ids
        else []
    )
    tally: dict[str, int] = defaultdict(int)
    for decision in decisions:
        if decision.approved:
            continue
        for code in _string_tuple(decision.reason_codes_json):
            tally[code] += 1

    orders = (
        list(
            session.scalars(
                select(BrokerOrderRecord).where(BrokerOrderRecord.run_id.in_(run_ids))
            )
        )
        if run_ids
        else []
    )
    filled_order_ids = _orders_with_fills(session, [order.id for order in orders])

    no_action_runs = (
        session.scalar(
            select(func.count())
            .select_from(AgentDecisionRecord)
            .join(Run, Run.id == AgentDecisionRecord.run_id)
            .where(
                AgentDecisionRecord.run_id.in_(run_ids),
                AgentDecisionRecord.book_id.is_(None),
                AgentDecisionRecord.status == "NO_ACTION",
                Run.status == "COMPLETED",
            )
        )
        or 0
        if run_ids
        else 0
    )

    changes = (
        list(
            session.scalars(
                select(KnowledgeChange).where(KnowledgeChange.run_id.in_(run_ids))
            )
        )
        if run_ids
        else []
    )

    return WeeklyPerformance(
        period_start=start,
        period_end=end,
        run_count=len(runs),
        equity_points=equity_points,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        pnl=pnl,
        return_pct=return_pct,
        peak_equity=max(peaks) if peaks else None,
        max_drawdown_pct=max(drawdowns) if drawdowns else None,
        proposal_count=len(proposals),
        approved_count=sum(1 for decision in decisions if decision.approved),
        rejected_count=sum(1 for decision in decisions if not decision.approved),
        rejection_codes=tuple(
            RejectionTally(code=code, count=count)
            for code, count in sorted(tally.items(), key=lambda item: (-item[1], item[0]))
        ),
        no_action_run_count=no_action_runs,
        submitted_order_count=len(orders),
        filled_order_count=len(filled_order_ids),
        theses_opened=sum(1 for change in changes if change.change_type == THESIS_OPENED),
        theses_closed=sum(1 for change in changes if change.change_type in CLOSURE_CHANGE_TYPES),
        thesis_outcomes=load_thesis_outcomes(
            session,
            period_start=start,
            period_end=end,
            max_outcomes=max_thesis_outcomes,
        ),
        strategy_versions=strategy_versions_in_effect(
            session,
            period_start=start,
            period_end=end,
        ),
    )


def load_thesis_outcomes(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
    max_outcomes: int = MAX_THESIS_OUTCOMES,
) -> tuple[ThesisOutcome, ...]:
    """Return what each thesis alive during the period cost and returned.

    A thesis qualifies if it existed before the period ended and either is still active or was
    touched during the period, which is what makes a review cover held positions as well as
    completed round trips.
    """
    if max_outcomes <= 0:
        raise ValueError("thesis-outcome limit must be positive")
    start = _utc(period_start)
    end = _utc(period_end)
    theses = list(
        session.scalars(
            select(Thesis)
            .where(
                Thesis.created_at < end,
                or_(Thesis.status == "active", Thesis.updated_at >= start),
            )
            .order_by(Thesis.created_at, Thesis.id)
            .limit(max_outcomes)
        )
    )
    if not theses:
        return ()
    thesis_ids = [thesis.id for thesis in theses]

    proposals_by_thesis: dict[str, list[str]] = defaultdict(list)
    for proposal_id, thesis_id in session.execute(
        select(TradeProposalRecord.id, TradeProposalRecord.thesis_id).where(
            TradeProposalRecord.thesis_id.in_(thesis_ids)
        )
    ):
        proposals_by_thesis[thesis_id].append(proposal_id)

    proposal_ids = [item for items in proposals_by_thesis.values() for item in items]
    orders_by_proposal: dict[str, list[str]] = defaultdict(list)
    if proposal_ids:
        for order_id, proposal_id in session.execute(
            select(BrokerOrderRecord.id, BrokerOrderRecord.proposal_id).where(
                BrokerOrderRecord.proposal_id.in_(proposal_ids)
            )
        ):
            orders_by_proposal[proposal_id].append(order_id)

    order_ids = [item for items in orders_by_proposal.values() for item in items]
    fills_by_order: dict[str, list[Fill]] = defaultdict(list)
    if order_ids:
        for fill in session.scalars(
            select(Fill)
            .where(Fill.broker_order_record_id.in_(order_ids))
            .order_by(Fill.transaction_time, Fill.id)
        ):
            fills_by_order[fill.broker_order_record_id].append(fill)

    closures = _closure_types(session, thesis_ids)
    outcomes: list[ThesisOutcome] = []
    for thesis in theses:
        fills = [
            fill
            for proposal_id in proposals_by_thesis[thesis.id]
            for order_id in orders_by_proposal[proposal_id]
            for fill in fills_by_order[order_id]
        ]
        opened_at = _utc(thesis.created_at)
        updated_at = _utc(thesis.updated_at)
        closed = thesis.status != "active"
        outcomes.append(
            ThesisOutcome(
                thesis_id=thesis.id,
                symbol=thesis.symbol,
                title=_truncate(thesis.title, MAX_TITLE_CHARS),
                status="closed" if closed else "active",
                closure=closures.get(thesis.id),
                confidence=thesis.confidence,
                opened_at=opened_at,
                updated_at=updated_at,
                closed_at=updated_at if closed else None,
                holding_days=max(0, (updated_at - opened_at).days) if closed else None,
                proposal_count=len(proposals_by_thesis[thesis.id]),
                fill_count=len(fills),
                **_fill_totals(fills),
            )
        )
    return tuple(outcomes)


def _fill_totals(fills: list[Fill]) -> dict[str, Decimal | None]:
    buy_qty = Decimal("0")
    buy_notional = Decimal("0")
    sell_qty = Decimal("0")
    sell_notional = Decimal("0")
    commission = Decimal("0")
    for fill in fills:
        quantity = _decimal(fill.qty)
        price = _decimal(fill.price)
        if quantity is None or price is None:
            continue
        commission += _decimal(fill.commission) or Decimal("0")
        if fill.side.strip().lower().startswith("s"):
            sell_qty += quantity
            sell_notional += quantity * price
        else:
            buy_qty += quantity
            buy_notional += quantity * price

    realized: Decimal | None = None
    if buy_qty > 0 and sell_qty > 0:
        matched = min(sell_qty, buy_qty)
        average_entry = buy_notional / buy_qty
        proceeds = sell_notional if sell_qty <= buy_qty else sell_notional * matched / sell_qty
        realized = _quantize(proceeds - average_entry * matched - commission)
    return {
        "buy_qty": buy_qty,
        "buy_notional": _quantize(buy_notional),
        "sell_qty": sell_qty,
        "sell_notional": _quantize(sell_notional),
        "commission": _quantize(commission),
        "open_qty": buy_qty - sell_qty,
        "realized_pnl": realized,
    }


def _closure_types(session: Session, thesis_ids: list[str]) -> dict[str, str]:
    closures: dict[str, str] = {}
    for change in session.scalars(
        select(KnowledgeChange)
        .where(
            KnowledgeChange.entity_id.in_(thesis_ids),
            KnowledgeChange.change_type.in_(CLOSURE_CHANGE_TYPES),
        )
        .order_by(KnowledgeChange.created_at, KnowledgeChange.id)
    ):
        closures[change.entity_id] = change.change_type
    return closures


def _orders_with_fills(session: Session, order_ids: list[str]) -> set[str]:
    if not order_ids:
        return set()
    return set(
        session.scalars(
            select(Fill.broker_order_record_id).where(
                Fill.broker_order_record_id.in_(order_ids)
            )
        )
    )


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


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 16)].rstrip() + "\n[TRUNCATED]"
