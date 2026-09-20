import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.models import TradeProposal
from trader.broker.models import Account
from trader.ledger.history import load_equity_curve, load_open_theses, load_recent_decisions
from trader.ledger.models import LedgerWriteSummary
from trader.ledger.service import (
    THESIS_CLOSED_NO_POSITION,
    THESIS_CLOSED_ON_EXIT,
    THESIS_OPENED,
    THESIS_UPDATED,
    close_theses_without_positions,
    record_decision_ledger,
    record_performance_snapshot,
    record_run_report,
    snapshot_peak_equity,
)
from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    BrokerOrderRecord,
    KnowledgeChange,
    PerformanceSnapshot,
    Run,
    Thesis,
    ThesisEvidence,
    TradeProposalRecord,
)
from trader.persistence.repositories import (
    PersistenceConflictError,
    persist_research_item,
    persist_risk_decision,
    persist_trade_proposal,
    record_fill,
)
from trader.risk.models import NormalizedOrder, RiskDecision

AS_OF = datetime(2026, 8, 22, 19, 15, tzinfo=UTC)
UNKNOWN_EVIDENCE_ID = "f" * 64


def test_approved_buy_opens_a_thesis_and_links_only_run_scoped_evidence(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    evidence_id = _evidence(session, run_id=run.id, marker="spy-context", symbol="SPY")
    proposal = _buy("SPY", evidence_ids=[evidence_id, UNKNOWN_EVIDENCE_ID])

    summary = _apply(session, run=run, proposals=(proposal,))

    thesis = _only(session, Thesis)
    assert summary.opened_thesis_ids == (thesis.id,)
    assert summary.updated_thesis_ids == ()
    assert summary.closed_thesis_ids == ()
    assert summary.linked_evidence_count == 1
    assert summary.unlinked_evidence_count == 1
    assert thesis.symbol == "SPY"
    assert thesis.status == "active"
    assert thesis.confidence == pytest.approx(0.6)
    assert json.loads(thesis.invalidation_json) == ["Support fails"]
    assert thesis.title.startswith("SPY — ")

    link = _only(session, ThesisEvidence)
    assert link.thesis_id == thesis.id
    assert link.research_id == evidence_id
    assert link.relationship == "supporting"

    change = _only(session, KnowledgeChange)
    assert change.change_type == THESIS_OPENED
    assert change.entity_id == thesis.id
    assert change.before_text == ""
    assert "status=active" in change.after_text
    assert json.loads(change.evidence_ids_json) == sorted(proposal.evidence_ids)

    record = session.get(TradeProposalRecord, str(proposal.proposal_id))
    assert record is not None
    assert record.thesis_id == thesis.id


def test_risk_rejected_proposals_leave_no_trace_in_the_ledger(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    evidence_id = _evidence(session, run_id=run.id, marker="spy-context", symbol="SPY")
    proposal = _buy("SPY", evidence_ids=[evidence_id])
    persist_trade_proposal(session, run.id, proposal)

    summary = record_decision_ledger(
        session,
        run_id=run.id,
        as_of=AS_OF,
        proposals=(proposal,),
        approved_proposal_ids=frozenset(),
    )

    assert summary == LedgerWriteSummary()
    assert list(session.scalars(select(Thesis))) == []
    assert list(session.scalars(select(KnowledgeChange))) == []
    record = session.get(TradeProposalRecord, str(proposal.proposal_id))
    assert record is not None
    assert record.thesis_id is None


def test_partial_sell_restates_the_thesis_and_full_exit_closes_it(tmp_path: Path) -> None:
    session = _session(tmp_path)
    entry = _run(session, key="daily:2026-08-20", scheduled_for=AS_OF - timedelta(days=2))
    trim = _run(session, key="daily:2026-08-21", scheduled_for=AS_OF - timedelta(days=1))
    exit_run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)

    opened = _apply(session, run=entry, proposals=(_buy("SPY", evidence_ids=[]),))
    assert len(opened.opened_thesis_ids) == 1
    thesis_id = opened.opened_thesis_ids[0]

    trimmed = _apply(
        session,
        run=trim,
        proposals=(_sell("SPY", target_position_pct=Decimal("2"), confidence=0.4),),
    )
    assert trimmed.updated_thesis_ids == (thesis_id,)
    assert trimmed.closed_thesis_ids == ()
    trimmed_thesis = session.get(Thesis, thesis_id)
    assert trimmed_thesis is not None
    assert trimmed_thesis.status == "active"
    assert trimmed_thesis.confidence == pytest.approx(0.4)
    assert trimmed_thesis.summary.startswith("Rationale: SPY no longer earns its weight.")

    closed = _apply(
        session,
        run=exit_run,
        proposals=(_sell("SPY", target_position_pct=Decimal("0")),),
    )
    assert closed.closed_thesis_ids == (thesis_id,)
    exited_thesis = session.get(Thesis, thesis_id)
    assert exited_thesis is not None
    assert exited_thesis.status == "closed"
    assert _change_types(session) == [THESIS_OPENED, THESIS_UPDATED, THESIS_CLOSED_ON_EXIT]


def test_selling_a_symbol_with_no_active_thesis_is_ignored(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    proposal = _sell("QQQ", target_position_pct=Decimal("0"))

    summary = _apply(session, run=run, proposals=(proposal,))

    assert summary == LedgerWriteSummary()
    assert list(session.scalars(select(Thesis))) == []
    record = session.get(TradeProposalRecord, str(proposal.proposal_id))
    assert record is not None
    assert record.thesis_id is None


def test_recording_the_decision_ledger_twice_for_one_run_is_refused(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    proposal = _buy("SPY", evidence_ids=[])
    _apply(session, run=run, proposals=(proposal,))

    with pytest.raises(PersistenceConflictError, match="decision ledger already recorded"):
        record_decision_ledger(
            session,
            run_id=run.id,
            as_of=AS_OF,
            proposals=(proposal,),
            approved_proposal_ids=frozenset({str(proposal.proposal_id)}),
        )
    assert len(list(session.scalars(select(Thesis)))) == 1


def test_a_thesis_cannot_outlive_its_position(tmp_path: Path) -> None:
    session = _session(tmp_path)
    entry = _run(session, key="daily:2026-08-21", scheduled_for=AS_OF - timedelta(days=1))
    later = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    opened = _apply(session, run=entry, proposals=(_buy("SPY", evidence_ids=[]),))

    closed = close_theses_without_positions(
        session,
        run_id=later.id,
        held_symbols=frozenset({"QQQ"}),
        as_of=AS_OF,
    )

    assert closed == opened.opened_thesis_ids
    thesis = _only(session, Thesis)
    assert thesis.status == "closed"
    assert _change_types(session) == [THESIS_OPENED, THESIS_CLOSED_NO_POSITION]

    with pytest.raises(PersistenceConflictError, match="already ran"):
        close_theses_without_positions(
            session,
            run_id=later.id,
            held_symbols=frozenset({"QQQ"}),
            as_of=AS_OF,
        )


def test_position_reconciliation_keeps_theses_for_held_symbols(tmp_path: Path) -> None:
    session = _session(tmp_path)
    entry = _run(session, key="daily:2026-08-21", scheduled_for=AS_OF - timedelta(days=1))
    later = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    _apply(session, run=entry, proposals=(_buy("SPY", evidence_ids=[]),))

    closed = close_theses_without_positions(
        session,
        run_id=later.id,
        held_symbols=frozenset({" spy "}),
        as_of=AS_OF,
    )

    assert closed == ()
    assert _only(session, Thesis).status == "active"
    assert _change_types(session) == [THESIS_OPENED]


def test_performance_snapshots_carry_the_peak_forward(tmp_path: Path) -> None:
    session = _session(tmp_path)

    first = _snapshot(session, day=20, equity="2000", cash="2000")
    assert first.pnl is None
    assert first.return_pct is None
    assert first.drawdown_pct == "0"
    assert snapshot_peak_equity(first) == Decimal("2000")

    drawdown = _snapshot(session, day=21, equity="1800", cash="500")
    assert drawdown.pnl == "-200"
    assert drawdown.return_pct == "-10.000000"
    assert drawdown.drawdown_pct == "10.000000"
    assert snapshot_peak_equity(drawdown) == Decimal("2000")

    recovery = _snapshot(session, day=22, equity="2100", cash="600")
    assert recovery.pnl == "300"
    assert recovery.return_pct == "16.666667"
    assert recovery.drawdown_pct == "0"
    assert snapshot_peak_equity(recovery) == Decimal("2100")

    curve = load_equity_curve(session)
    assert [point[1] for point in curve] == [
        Decimal("2000"),
        Decimal("1800"),
        Decimal("2100"),
    ]


def test_performance_snapshot_is_idempotent_and_detects_divergent_equity(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    account = Account(equity="2000", cash="2000", buying_power="2000")
    first = record_performance_snapshot(session, run_id=run.id, account=account, as_of=AS_OF)
    repeated = record_performance_snapshot(session, run_id=run.id, account=account, as_of=AS_OF)

    assert repeated.id == first.id
    assert len(list(session.scalars(select(PerformanceSnapshot)))) == 1

    with pytest.raises(PersistenceConflictError, match="idempotency conflict"):
        record_performance_snapshot(
            session,
            run_id=run.id,
            account=Account(equity="2500", cash="2500", buying_power="2500"),
            as_of=AS_OF,
        )


def test_backfilled_performance_snapshot_does_not_use_future_curve_points(tmp_path: Path) -> None:
    session = _session(tmp_path)
    future = _snapshot(session, day=22, equity="3000", cash="3000")
    past = _snapshot(session, day=21, equity="2000", cash="2000")

    assert future.equity == "3000"
    assert past.pnl is None
    assert past.return_pct is None
    assert snapshot_peak_equity(past) == Decimal("2000")
    assert past.drawdown_pct == "0"


def test_daily_report_rows_are_idempotent_and_detect_divergent_content(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    content = "# Report\nNO_ACTION\n"
    summary = "NO_ACTION: evidence is insufficient."
    report = record_run_report(
        session,
        run_id=run.id,
        report_path="daily_report.md",
        content=content,
        summary=summary,
    )
    repeated = record_run_report(
        session,
        run_id=run.id,
        report_path="daily_report.md",
        content=content,
        summary=summary,
    )

    assert report.content_hash == hashlib.sha256(content.encode()).hexdigest()
    assert report.summary == summary
    assert repeated.id == report.id

    with pytest.raises(PersistenceConflictError, match="idempotency conflict"):
        record_run_report(
            session,
            run_id=run.id,
            report_path="daily_report.md",
            content="# Report\nPROPOSE_TRADES\n",
            summary="Rewritten after the fact.",
        )


def test_recent_decisions_stop_at_the_cutoff_and_carry_the_risk_outcome(tmp_path: Path) -> None:
    session = _session(tmp_path)
    knowable = _run(session, key="daily:2026-08-20", scheduled_for=AS_OF - timedelta(days=2))
    future = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    current = _run(session, key="daily:2026-08-23", scheduled_for=AS_OF + timedelta(days=1))
    rejected = _buy("SPY", evidence_ids=[])
    authorized = _buy("QQQ", evidence_ids=[])
    persist_trade_proposal(session, knowable.id, rejected)
    persist_trade_proposal(session, knowable.id, authorized)
    persist_risk_decision(
        session,
        knowable.id,
        RiskDecision(
            proposal_id=str(rejected.proposal_id),
            approved=False,
            rejection_codes=["MAX_SINGLE_POSITION_PCT"],
            human_explanation="The position would exceed the single-name cap.",
        ),
    )
    persist_risk_decision(
        session,
        knowable.id,
        RiskDecision(
            proposal_id=str(authorized.proposal_id),
            approved=True,
            normalized_order=NormalizedOrder(
                symbol="QQQ",
                side="buy",
                qty=Decimal("2"),
                limit_price=Decimal("50"),
                notional=Decimal("100"),
            ),
            human_explanation="Within every limit.",
        ),
    )
    _fill(session, run=knowable, proposal_id=str(authorized.proposal_id), qty=Decimal("2"))
    record_run_report(
        session,
        run_id=knowable.id,
        report_path="daily_report.md",
        content="# Report\n",
        summary="PROPOSE_TRADES: one entry authorized.",
    )
    persist_trade_proposal(session, future.id, _buy("IWM", evidence_ids=[]))

    decisions = load_recent_decisions(
        session,
        exclude_run_id=current.id,
        as_of=AS_OF - timedelta(days=1),
    )

    assert [record.run_key for record in decisions] == ["daily:2026-08-20"]
    assert decisions[0].decision_summary == "PROPOSE_TRADES: one entry authorized."
    outcomes = {outcome.symbol: outcome for outcome in decisions[0].proposals}
    assert outcomes["SPY"].risk_approved is False
    assert outcomes["SPY"].risk_rejection_codes == ("MAX_SINGLE_POSITION_PCT",)
    assert outcomes["SPY"].order_status is None
    assert outcomes["SPY"].filled_qty is None
    assert outcomes["SPY"].target_notional_usd == Decimal("100")
    assert outcomes["QQQ"].risk_approved is True
    assert outcomes["QQQ"].order_status == "filled"
    assert outcomes["QQQ"].filled_qty == Decimal("2")


def test_recent_decisions_ignore_non_daily_and_unfinished_runs(tmp_path: Path) -> None:
    session = _session(tmp_path)
    current = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)
    event = _run(session, key="event:2026-08-20:GDP", scheduled_for=AS_OF - timedelta(days=2))
    crashed = _run(
        session,
        key="daily:2026-08-21",
        scheduled_for=AS_OF - timedelta(days=1),
        status="FAILED",
    )
    rerun = _run(session, key="daily-test:2026-08-19", scheduled_for=AS_OF - timedelta(days=3))
    persist_trade_proposal(session, event.id, _buy("SPY", evidence_ids=[]))
    persist_trade_proposal(session, crashed.id, _buy("QQQ", evidence_ids=[]))

    decisions = load_recent_decisions(session, exclude_run_id=current.id, as_of=AS_OF)

    assert [record.run_id for record in decisions] == [rerun.id]
    assert decisions[0].proposals == ()
    assert decisions[0].decision_summary is None


def test_history_limits_must_be_positive(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = _run(session, key="daily:2026-08-22", scheduled_for=AS_OF)

    with pytest.raises(ValueError, match="must be positive"):
        load_recent_decisions(session, exclude_run_id=run.id, as_of=AS_OF, max_runs=0)
    with pytest.raises(ValueError, match="must be positive"):
        load_open_theses(session, max_theses=0)
    with pytest.raises(ValueError, match="must be positive"):
        load_equity_curve(session, max_points=0)


def test_open_theses_are_restricted_to_admitted_symbols(tmp_path: Path) -> None:
    session = _session(tmp_path)
    entry = _run(session, key="daily:2026-08-20", scheduled_for=AS_OF - timedelta(days=2))
    exited = _run(session, key="daily:2026-08-21", scheduled_for=AS_OF - timedelta(days=1))
    _apply(
        session,
        run=entry,
        proposals=(_buy("SPY", evidence_ids=[]), _buy("QQQ", evidence_ids=[])),
    )
    _apply(session, run=exited, proposals=(_sell("QQQ", target_position_pct=Decimal("0")),))

    assert [thesis.symbol for thesis in load_open_theses(session)] == ["SPY"]
    assert [thesis.symbol for thesis in load_open_theses(session, symbols=frozenset({"SPY"}))] == [
        "SPY"
    ]
    assert load_open_theses(session, symbols=frozenset({"IWM"})) == ()
    assert load_open_theses(session, symbols=frozenset()) == ()

    thesis = load_open_theses(session)[0]
    assert thesis.invalidation_conditions == ("Support fails",)
    assert thesis.confidence == pytest.approx(0.6)
    assert thesis.opened_at.tzinfo is not None


def _session(tmp_path: Path) -> Session:
    return create_session_factory(f"sqlite:///{tmp_path}/ledger.sqlite")()


def _run(
    session: Session,
    *,
    key: str,
    scheduled_for: datetime,
    status: str = "COMPLETED",
) -> Run:
    run = Run(run_key=key, scheduled_for=scheduled_for, config_hash="config", status=status)
    session.add(run)
    session.commit()
    return run


def _evidence(session: Session, *, run_id: str, marker: str, symbol: str) -> str:
    content_hash = hashlib.sha256(marker.encode()).hexdigest()
    research_id = hashlib.sha256(f"{run_id}\x00{content_hash}".encode()).hexdigest()
    persist_research_item(
        session,
        research_id=research_id,
        run_id=run_id,
        symbols=(symbol,),
        source_tier="BROKER",
        source_type="MARKET_DATA",
        source_name="alpaca-market-context",
        provider="alpaca",
        provider_item_id=f"{symbol}:{marker}",
        research_question_id="a" * 64,
        research_question="What changed?",
        raw_artifact_path=f"research/alpaca/{marker}.json",
        normalized_summary=f"{symbol} market context summary",
        normalized_text=f"{symbol} detailed market context",
        published_at=AS_OF - timedelta(minutes=5),
        retrieved_at=AS_OF,
        content_hash=content_hash,
        headline=f"{symbol} market context",
    )
    return research_id


def _buy(symbol: str, *, evidence_ids: list[str], confidence: float = 0.6) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action="BUY",
        target_notional_usd=Decimal("100"),
        confidence=confidence,
        time_horizon="weeks",
        rationale=f"{symbol} is the cleanest expression of the current regime.",
        catalysts=["Trend continuation"],
        key_risks=["Reversal"],
        invalidation_conditions=["Support fails"],
        evidence_ids=evidence_ids,
        max_acceptable_price=Decimal("650"),
    )


def _sell(
    symbol: str,
    *,
    target_position_pct: Decimal,
    confidence: float = 0.6,
) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action="SELL",
        target_position_pct=target_position_pct,
        confidence=confidence,
        time_horizon="weeks",
        rationale=f"{symbol} no longer earns its weight.",
        invalidation_conditions=["Trend reasserts"],
        evidence_ids=[],
        min_acceptable_price=Decimal("100"),
    )


def _apply(
    session: Session,
    *,
    run: Run,
    proposals: tuple[TradeProposal, ...],
) -> LedgerWriteSummary:
    """Persist proposals and record the ledger as if risk had approved every one of them."""
    for proposal in proposals:
        persist_trade_proposal(session, run.id, proposal)
    return record_decision_ledger(
        session,
        run_id=run.id,
        as_of=run.scheduled_for,
        proposals=proposals,
        approved_proposal_ids=frozenset(str(proposal.proposal_id) for proposal in proposals),
    )


def _snapshot(session: Session, *, day: int, equity: str, cash: str) -> PerformanceSnapshot:
    as_of = datetime(2026, 8, day, 19, 15, tzinfo=UTC)
    run = _run(session, key=f"daily:2026-08-{day}", scheduled_for=as_of)
    return record_performance_snapshot(
        session,
        run_id=run.id,
        account=Account(equity=equity, cash=cash, buying_power=cash),
        as_of=as_of,
    )


def _fill(session: Session, *, run: Run, proposal_id: str, qty: Decimal) -> None:
    order = BrokerOrderRecord(
        run_id=run.id,
        proposal_id=proposal_id,
        client_order_id=f"{run.id}:{proposal_id}",
        symbol="QQQ",
        side="buy",
        qty=str(qty),
        limit_price="50",
        status="filled",
    )
    session.add(order)
    session.commit()
    record_fill(
        session,
        broker_order_record_id=order.id,
        broker_activity_id=f"{order.id}:1",
        qty=qty,
        price=Decimal("50"),
        side="buy",
        transaction_time=run.scheduled_for,
    )


def _only[RecordT](session: Session, model: type[RecordT]) -> RecordT:
    records = list(session.scalars(select(model)))
    assert len(records) == 1
    return records[0]


def _change_types(session: Session) -> list[str]:
    return [
        change.change_type
        for change in session.scalars(
            select(KnowledgeChange).order_by(KnowledgeChange.created_at, KnowledgeChange.id)
        )
    ]
