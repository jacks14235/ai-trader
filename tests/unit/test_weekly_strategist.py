import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.codex_cli import InvocationResponse
from trader.agent.config import AgentConfig, load_agent_config
from trader.agent.models import TradeProposal
from trader.agent.weekly import (
    ProposedStrategyChange,
    StrategyRecommendation,
    WeeklyAgentContext,
    apply_anchored_change,
    assemble_weekly_context,
    validate_strategy_recommendation,
    verify_weekly_context_sources,
)
from trader.agent.weekly_runner import review_period, weekly_run, weekly_run_key
from trader.broker.models import Account
from trader.ledger.knowledge import (
    STRATEGY_CHANGE_APPROVED,
    STRATEGY_CHANGE_PROPOSED,
    STRATEGY_CHANGE_REJECTED,
    STRATEGY_REVIEW_NO_CHANGE,
    get_strategy_proposal,
    pending_strategy_proposals,
    resolve_strategy_proposal,
)
from trader.ledger.performance import load_thesis_outcomes, load_weekly_performance
from trader.ledger.service import record_decision_ledger, record_performance_snapshot
from trader.ledger.strategy import (
    attribute_run_to_strategy,
    current_strategy_version,
    record_strategy_version,
    strategy_content_hash,
)
from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    BrokerOrderRecord,
    KnowledgeChange,
    Run,
    Strategy,
    Thesis,
)
from trader.persistence.repositories import (
    PersistenceConflictError,
    persist_trade_proposal,
    record_fill,
)

PROJECT_ROOT = Path(__file__).parents[2]
# Saturday 2026-08-22, after the trading week ended.
REVIEW_AT = datetime(2026, 8, 22, 21, 15, tzinfo=UTC)
STRATEGY = """# Strategy

## Position sizing
Keep any single new position under ten percent of equity.

## Exits
Exit when the recorded invalidation condition fires.
"""
POLICY = "# Portfolio policy\n\nHuman-owned limits live here.\n"


class ReviewProvider:
    provider_name = "test"

    def __init__(self, recommendation: StrategyRecommendation) -> None:
        self.recommendation = recommendation
        self.prompts: list[str] = []

    def invoke(self, **kwargs: object) -> InvocationResponse:
        self.prompts.append(str(kwargs["prompt"]))
        return InvocationResponse(self.recommendation.model_dump_json(), "stdout", "")


def test_review_period_covers_the_trading_week_that_just_ended() -> None:
    start, end = review_period(REVIEW_AT)

    assert end == datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    assert start == datetime(2026, 8, 15, 4, 0, tzinfo=UTC)
    # Monday through Friday of the finished week fall inside the period.
    assert start < datetime(2026, 8, 17, 19, 15, tzinfo=UTC) < end
    assert start < datetime(2026, 8, 21, 19, 15, tzinfo=UTC) < end
    # A Sunday review still covers the same finished week rather than sliding forward.
    assert review_period(datetime(2026, 8, 23, 16, 0, tzinfo=UTC)) == (start, end)
    assert weekly_run_key(end) == "weekly:2026-08-22:America/New_York"

    with pytest.raises(ValueError, match="timezone-aware"):
        review_period(datetime(2026, 8, 22, 12, 0))


def test_strategy_versions_supersede_and_attribute_the_runs_that_used_them(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    first = record_strategy_version(session, document=STRATEGY, as_of=REVIEW_AT)
    repeated = record_strategy_version(session, document=STRATEGY + "\n", as_of=REVIEW_AT)

    assert repeated.strategy_id == first.strategy_id
    assert first.content_hash == strategy_content_hash(STRATEGY)
    assert first.status == "active"

    revised = record_strategy_version(
        session,
        document=STRATEGY + "\n## Review cadence\nReview weekly.\n",
        as_of=REVIEW_AT + timedelta(days=7),
    )
    assert revised.strategy_id != first.strategy_id
    assert revised.status == "active"
    assert current_strategy_version(session) is not None
    version = current_strategy_version(session)
    assert version is not None and version.strategy_id == revised.strategy_id
    assert session.get(Strategy, first.strategy_id).status == "superseded"  # type: ignore[union-attr]

    run = _run(session, key="daily:2026-08-18", scheduled_for=REVIEW_AT - timedelta(days=4))
    attribute_run_to_strategy(session, run_id=run.id, strategy_id=first.strategy_id)
    attribute_run_to_strategy(session, run_id=run.id, strategy_id=first.strategy_id)
    with pytest.raises(PersistenceConflictError, match="already attributed"):
        attribute_run_to_strategy(session, run_id=run.id, strategy_id=revised.strategy_id)


def test_thesis_outcome_is_realized_from_fills_not_from_proposals(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start, end = review_period(REVIEW_AT)
    entry_run = _run(session, key="daily:2026-08-17", scheduled_for=start + timedelta(days=2))
    proposal = _buy("SPY")
    persist_trade_proposal(session, entry_run.id, proposal)
    record_decision_ledger(
        session,
        run_id=entry_run.id,
        as_of=entry_run.scheduled_for,
        proposals=(proposal,),
        approved_proposal_ids=frozenset({str(proposal.proposal_id)}),
    )
    _fill(
        session,
        run=entry_run,
        proposal_id=str(proposal.proposal_id),
        side="buy",
        qty=Decimal("10"),
        price=Decimal("100"),
    )

    outcomes = load_thesis_outcomes(session, period_start=start, period_end=end)
    assert len(outcomes) == 1
    held = outcomes[0]
    assert held.status == "active"
    assert held.buy_qty == Decimal("10")
    assert held.buy_notional == Decimal("1000")
    assert held.open_qty == Decimal("10")
    assert held.realized_pnl is None
    assert held.fill_count == 1

    exit_run = _run(session, key="daily:2026-08-20", scheduled_for=start + timedelta(days=5))
    exit_proposal = _sell("SPY", target_position_pct=Decimal("0"))
    persist_trade_proposal(session, exit_run.id, exit_proposal)
    record_decision_ledger(
        session,
        run_id=exit_run.id,
        as_of=exit_run.scheduled_for,
        proposals=(exit_proposal,),
        approved_proposal_ids=frozenset({str(exit_proposal.proposal_id)}),
    )
    _fill(
        session,
        run=exit_run,
        proposal_id=str(exit_proposal.proposal_id),
        side="sell",
        qty=Decimal("10"),
        price=Decimal("112"),
    )

    closed = load_thesis_outcomes(session, period_start=start, period_end=end)[0]
    assert closed.status == "closed"
    assert closed.closure == "THESIS_CLOSED_ON_EXIT"
    assert closed.sell_notional == Decimal("1120")
    assert closed.realized_pnl == Decimal("120")
    assert closed.open_qty == Decimal("0")
    assert closed.holding_days == 3


def test_weekly_performance_aggregates_only_the_requested_period(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start, end = review_period(REVIEW_AT)
    before = _run(session, key="daily:2026-08-14", scheduled_for=start - timedelta(days=1))
    record_performance_snapshot(
        session,
        run_id=before.id,
        account=_account("10000"),
        as_of=before.scheduled_for,
    )
    inside = _run(session, key="daily:2026-08-18", scheduled_for=start + timedelta(days=3))
    record_performance_snapshot(
        session,
        run_id=inside.id,
        account=_account("10400"),
        as_of=inside.scheduled_for,
    )
    quiet = _run(session, key="daily:2026-08-19", scheduled_for=start + timedelta(days=4))
    after = _run(session, key="daily:2026-08-24", scheduled_for=end + timedelta(days=2))
    record_performance_snapshot(
        session,
        run_id=after.id,
        account=_account("99999"),
        as_of=after.scheduled_for,
    )
    proposal = _buy("SPY")
    persist_trade_proposal(session, inside.id, proposal)
    _risk(session, run=inside, proposal_id=str(proposal.proposal_id), approved=False)

    performance = load_weekly_performance(session, period_start=start, period_end=end)

    assert performance.run_count == 2
    assert [point.equity for point in performance.equity_points] == [Decimal("10400")]
    assert performance.starting_equity == Decimal("10000")
    assert performance.ending_equity == Decimal("10400")
    assert performance.pnl == Decimal("400")
    assert performance.return_pct == Decimal("4")
    assert performance.proposal_count == 1
    assert performance.approved_count == 0
    assert performance.rejected_count == 1
    assert [item.code for item in performance.rejection_codes] == ["POSITION_LIMIT"]
    assert performance.no_action_run_count == 1
    assert quiet.id != inside.id
    assert performance.has_sample() is True

    empty = load_weekly_performance(
        session,
        period_start=start - timedelta(days=30),
        period_end=start - timedelta(days=23),
    )
    assert empty.has_sample() is False
    assert empty.run_count == 0

    with pytest.raises(ValueError, match="must end after it starts"):
        load_weekly_performance(session, period_start=end, period_end=start)


def test_the_review_cannot_see_runs_that_happened_after_the_period(tmp_path: Path) -> None:
    session, config, _context = _context_with_history(tmp_path)
    start, end = review_period(REVIEW_AT)
    later = _run(session, key="daily:2026-08-22", scheduled_for=end + timedelta(hours=10))
    proposal = _buy("QQQ")
    persist_trade_proposal(session, later.id, proposal)
    version = record_strategy_version(session, document=STRATEGY, as_of=REVIEW_AT)
    review = _run(session, key="weekly:later", scheduled_for=REVIEW_AT)

    context = assemble_weekly_context(
        session,
        run_id=review.id,
        as_of=REVIEW_AT,
        period_start=start,
        period_end=end,
        strategy=STRATEGY,
        strategy_version=version,
        portfolio_policy=POLICY,
        role=config.roles["weekly_strategist"],
    )

    assert later.scheduled_for > end
    assert later.id not in {record.run_id for record in context.recent_decisions}
    with pytest.raises(ValueError, match="cites a run outside"):
        validate_strategy_recommendation(_no_change(cited_run_ids=(later.id,)), context)


def test_declaring_a_weekly_context_source_the_assembler_cannot_supply_fails_closed() -> None:
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    role = config.roles["weekly_strategist"]
    verify_weekly_context_sources(role)

    undelivered = role.model_copy(
        update={"context_sources": (*role.context_sources, "deep_research")}
    )
    with pytest.raises(ValueError, match="deep_research"):
        verify_weekly_context_sources(undelivered)


def test_a_review_cannot_cite_records_it_was_not_shown(tmp_path: Path) -> None:
    session, config, context = _context_with_history(tmp_path)

    assert validate_strategy_recommendation(_no_change(), context) is not None
    with pytest.raises(ValueError, match="cites a run outside"):
        validate_strategy_recommendation(_no_change(cited_run_ids=("invented",)), context)
    with pytest.raises(ValueError, match="cites a proposal outside"):
        validate_strategy_recommendation(_no_change(cited_proposal_ids=("invented",)), context)
    with pytest.raises(ValueError, match="cites a thesis outside"):
        validate_strategy_recommendation(_no_change(cited_thesis_ids=("invented",)), context)

    known_run = context.recent_decisions[0].run_id
    cited = _no_change(cited_run_ids=(known_run,))
    assert validate_strategy_recommendation(cited, context) is cited
    assert config.roles["weekly_strategist"].permissions.can_mutate_knowledge is True


def test_a_proposed_change_must_anchor_uniquely_to_the_current_document(
    tmp_path: Path,
) -> None:
    session, _config, context = _context_with_history(tmp_path)
    assert session is not None

    anchored = _propose("Keep any single new position under ten percent of equity.")
    assert validate_strategy_recommendation(anchored, context) is anchored

    with pytest.raises(ValueError, match="not anchored"):
        validate_strategy_recommendation(_propose("Text that is not in the document."), context)

    repeated = context.model_copy(
        update={"strategy": context.strategy + "\nKeep any single new position under ten "
                "percent of equity.\n"}
    )
    with pytest.raises(ValueError, match="appears more than once"):
        validate_strategy_recommendation(anchored, repeated)

    with pytest.raises(ValueError, match="may not propose edits to the portfolio policy"):
        validate_strategy_recommendation(
            _propose("Human-owned limits live here."),
            context.model_copy(update={"strategy": context.strategy + "Human-owned limits "
                                       "live here."}),
        )


def test_a_change_cannot_be_proposed_for_a_period_with_no_decisions(tmp_path: Path) -> None:
    session = _session(tmp_path)
    version = record_strategy_version(session, document=STRATEGY, as_of=REVIEW_AT)
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    start, end = review_period(REVIEW_AT)
    run = _run(session, key="weekly:2026-08-22", scheduled_for=REVIEW_AT)
    context = assemble_weekly_context(
        session,
        run_id=run.id,
        as_of=REVIEW_AT,
        period_start=start,
        period_end=end,
        strategy=STRATEGY,
        strategy_version=version,
        portfolio_policy=POLICY,
        role=config.roles["weekly_strategist"],
    )

    assert context.performance.has_sample() is False
    no_change = _no_change()
    assert validate_strategy_recommendation(no_change, context) is no_change
    with pytest.raises(ValueError, match="no decisions"):
        validate_strategy_recommendation(
            _propose("Keep any single new position under ten percent of equity."),
            context,
        )


def test_a_recommendation_must_be_internally_coherent() -> None:
    with pytest.raises(ValueError, match="NO_CHANGE requires a reason"):
        StrategyRecommendation(
            status="NO_CHANGE",
            diagnosis="The sample is too small.",
            process_assessment="Decisions were consistent with the strategy.",
        )
    with pytest.raises(ValueError, match="PROPOSE_CHANGE requires a proposed change"):
        StrategyRecommendation(
            status="PROPOSE_CHANGE",
            diagnosis="Sizing was inconsistent.",
            process_assessment="Sizing logic varied without a stated reason.",
        )
    with pytest.raises(ValueError, match="must differ from the text it replaces"):
        ProposedStrategyChange(
            section_heading="Position sizing",
            current_text="same",
            replacement_text="same",
            hypothesis="h",
            disconfirming_evidence="d",
            expected_effect="e",
            evaluation_plan="p",
            revert_criteria="r",
        )


def test_weekly_run_records_a_reviewable_proposal_and_never_applies_it(tmp_path: Path) -> None:
    session, config, provider, strategy_path = _runnable(tmp_path)
    before = strategy_path.read_text()

    result = weekly_run(
        session,
        tmp_path / "raw",
        config,
        prompt="Return structured output only.",
        strategy=STRATEGY,
        portfolio_policy=POLICY,
        provider=provider,
        as_of=REVIEW_AT,
    )

    assert result.recommendation.status == "PROPOSE_CHANGE"
    assert strategy_path.read_text() == before, "a review must never edit the document itself"

    run = session.get(Run, result.run_id)
    assert run is not None
    assert run.status == "COMPLETED"
    assert run.run_key == "weekly:2026-08-22:America/New_York"
    assert run.strategy_id == result.strategy_version.strategy_id

    proposals = pending_strategy_proposals(session)
    assert len(proposals) == 1
    assert proposals[0].change_id == result.knowledge_change_ids[0]
    assert proposals[0].current_text in STRATEGY

    directory = tmp_path / "raw" / "paper" / "runs" / result.run_id
    manifest = json.loads((directory / "manifest.json").read_text())
    assert "weekly_report.md" in manifest
    assert "weekly_performance.json" in manifest
    assert "agent/weekly_strategist/response.json" in manifest
    report = (directory / "weekly_report.md").read_text()
    assert "pending human review; nothing has been applied" in report

    assert _strategy_change_types(session) == [STRATEGY_CHANGE_PROPOSED]

    with pytest.raises(RuntimeError, match="already claimed"):
        weekly_run(
            session,
            tmp_path / "raw",
            config,
            prompt="Return structured output only.",
            strategy=STRATEGY,
            portfolio_policy=POLICY,
            provider=ReviewProvider(_no_change()),
            as_of=REVIEW_AT,
        )


def test_a_no_change_review_is_recorded_as_an_outcome_not_a_proposal(tmp_path: Path) -> None:
    session, config, _provider, _path = _runnable(tmp_path)

    result = weekly_run(
        session,
        tmp_path / "raw",
        config,
        prompt="Return structured output only.",
        strategy=STRATEGY,
        portfolio_policy=POLICY,
        provider=ReviewProvider(_no_change()),
        as_of=REVIEW_AT,
    )

    assert result.recommendation.status == "NO_CHANGE"
    assert pending_strategy_proposals(session) == ()
    change = session.get(KnowledgeChange, result.knowledge_change_ids[0])
    assert change is not None
    assert change.change_type == STRATEGY_REVIEW_NO_CHANGE


def test_approval_applies_the_anchored_change_and_records_who_decided(tmp_path: Path) -> None:
    session, config, provider, strategy_path = _runnable(tmp_path)
    result = weekly_run(
        session,
        tmp_path / "raw",
        config,
        prompt="Return structured output only.",
        strategy=STRATEGY,
        portfolio_policy=POLICY,
        provider=provider,
        as_of=REVIEW_AT,
    )
    proposal = get_strategy_proposal(session, result.knowledge_change_ids[0])

    updated = apply_anchored_change(
        strategy_path.read_text(),
        current_text=proposal.current_text,
        replacement_text=proposal.replacement_text,
    )
    strategy_path.write_text(updated)
    resolve_strategy_proposal(
        session,
        proposal=proposal,
        approved=True,
        reviewer="jack",
        note="reasonable and revertable",
        as_of=REVIEW_AT + timedelta(hours=1),
        applied_content_hash=strategy_content_hash(updated),
    )

    assert proposal.replacement_text in updated
    assert proposal.current_text not in updated
    assert pending_strategy_proposals(session) == ()
    approval = session.scalar(
        select(KnowledgeChange).where(KnowledgeChange.change_type == STRATEGY_CHANGE_APPROVED)
    )
    assert approval is not None
    assert "Reviewer: jack" in approval.reason
    assert strategy_content_hash(updated) in approval.reason

    with pytest.raises(PersistenceConflictError, match="already"):
        get_strategy_proposal(session, proposal.change_id)

    # The next run records the approved document as a new, superseding version.
    version = record_strategy_version(session, document=updated, as_of=REVIEW_AT)
    assert version.content_hash == strategy_content_hash(updated)
    assert version.strategy_id != result.strategy_version.strategy_id


def test_rejection_records_the_decision_without_touching_the_document(tmp_path: Path) -> None:
    session, config, provider, strategy_path = _runnable(tmp_path)
    before = strategy_path.read_text()
    result = weekly_run(
        session,
        tmp_path / "raw",
        config,
        prompt="Return structured output only.",
        strategy=STRATEGY,
        portfolio_policy=POLICY,
        provider=provider,
        as_of=REVIEW_AT,
    )
    proposal = get_strategy_proposal(session, result.knowledge_change_ids[0])

    resolve_strategy_proposal(
        session,
        proposal=proposal,
        approved=False,
        reviewer="jack",
        note="the sample is one week",
        as_of=REVIEW_AT + timedelta(hours=1),
    )

    assert strategy_path.read_text() == before
    assert pending_strategy_proposals(session) == ()
    rejection = session.scalar(
        select(KnowledgeChange).where(KnowledgeChange.change_type == STRATEGY_CHANGE_REJECTED)
    )
    assert rejection is not None and rejection.after_text == ""

    with pytest.raises(ValueError, match="record the resulting document hash"):
        resolve_strategy_proposal(
            session,
            proposal=proposal,
            approved=True,
            reviewer="jack",
            note="",
            as_of=REVIEW_AT,
        )


def test_a_rejected_review_leaves_no_knowledge_change(tmp_path: Path) -> None:
    session, config, _provider, _path = _runnable(tmp_path)
    # An anchor that does not exist in the document must abort the run, not be recorded.
    invalid = ReviewProvider(_propose("This sentence is absent from the strategy."))

    with pytest.raises(ValueError, match="not anchored"):
        weekly_run(
            session,
            tmp_path / "raw",
            config,
            prompt="Return structured output only.",
            strategy=STRATEGY,
            portfolio_policy=POLICY,
            provider=invalid,
            as_of=REVIEW_AT,
        )

    assert _strategy_change_types(session) == []
    run = session.scalar(select(Run).where(Run.run_key.startswith("weekly:")))
    assert run is not None and run.status == "FAILED"


def test_the_strategist_never_reaches_a_broker() -> None:
    source = (PROJECT_ROOT / "src" / "trader" / "agent" / "weekly_runner.py").read_text()
    imports = [
        line for line in source.splitlines() if line.startswith(("import ", "from ")) or
        line.startswith("    ") and "import" in line
    ]
    assert not [
        line for line in imports if "trader.broker" in line or "trader.execution" in line
    ]
    assert "Broker" not in str(inspect.signature(weekly_run))


def _session(tmp_path: Path) -> Session:
    return create_session_factory(f"sqlite:///{tmp_path}/weekly.sqlite")()


def _strategy_change_types(session: Session) -> list[str]:
    """Only the strategy line; thesis changes come from the daily ledger fixtures."""
    return [
        change.change_type
        for change in session.scalars(
            select(KnowledgeChange)
            .where(KnowledgeChange.entity_type.in_(["strategy", "strategy_change"]))
            .order_by(KnowledgeChange.created_at, KnowledgeChange.id)
        )
    ]


def _run(session: Session, *, key: str, scheduled_for: datetime, status: str = "COMPLETED") -> Run:
    run = Run(run_key=key, scheduled_for=scheduled_for, config_hash="config", status=status)
    session.add(run)
    session.commit()
    return run


def _account(equity: str) -> Account:
    return Account(equity=equity, cash=equity, buying_power=equity)


def _buy(symbol: str) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action="BUY",
        target_notional_usd=Decimal("1000"),
        confidence=0.6,
        time_horizon="weeks",
        rationale=f"{symbol} is the cleanest expression of the current regime.",
        invalidation_conditions=["Support fails"],
        evidence_ids=[],
        max_acceptable_price=Decimal("650"),
    )


def _sell(symbol: str, *, target_position_pct: Decimal) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action="SELL",
        target_position_pct=target_position_pct,
        confidence=0.6,
        time_horizon="weeks",
        rationale=f"{symbol} no longer earns its weight.",
        invalidation_conditions=["Trend reasserts"],
        evidence_ids=[],
        min_acceptable_price=Decimal("10"),
    )


def _fill(
    session: Session,
    *,
    run: Run,
    proposal_id: str,
    side: str,
    qty: Decimal,
    price: Decimal,
) -> None:
    order = BrokerOrderRecord(
        run_id=run.id,
        proposal_id=proposal_id,
        client_order_id=f"{run.id}:{proposal_id}",
        symbol="SPY",
        side=side,
        qty=str(qty),
        limit_price=str(price),
        status="filled",
    )
    session.add(order)
    session.commit()
    record_fill(
        session,
        broker_order_record_id=order.id,
        broker_activity_id=f"{order.id}:1",
        qty=qty,
        price=price,
        side=side,
        transaction_time=run.scheduled_for,
    )


def _risk(session: Session, *, run: Run, proposal_id: str, approved: bool) -> None:
    from trader.persistence.models import RiskDecisionRecord

    session.add(
        RiskDecisionRecord(
            run_id=run.id,
            proposal_id=proposal_id,
            approved=approved,
            reason_codes_json=json.dumps([] if approved else ["POSITION_LIMIT"]),
            explanation="deterministic",
        )
    )
    session.commit()


def _no_change(**overrides: object) -> StrategyRecommendation:
    return StrategyRecommendation(
        status="NO_CHANGE",
        diagnosis="One week of decisions is not a sample worth changing policy over.",
        process_assessment="Every proposal cited admitted evidence and stated invalidation.",
        no_change_reason="The record is too short to distinguish process from luck.",
        **overrides,  # type: ignore[arg-type]
    )


def _propose(anchor: str) -> StrategyRecommendation:
    return StrategyRecommendation(
        status="PROPOSE_CHANGE",
        diagnosis="Sizing was applied inconsistently across the week's entries.",
        process_assessment="Entries cited evidence but sized without a stated rule.",
        proposed_changes=(
            ProposedStrategyChange(
                section_heading="Position sizing",
                current_text=anchor,
                replacement_text=(
                    "Keep any single new position under eight percent of equity, "
                    "and state the sizing rule applied in the proposal rationale."
                ),
                hypothesis="An explicit rule makes sizing reviewable rather than ad hoc.",
                disconfirming_evidence="No entry was rejected for size during the period.",
                expected_effect="Sizing becomes auditable against a stated rule.",
                failure_modes=("Smaller size may under-express a high-confidence view.",),
                evaluation_plan="Compare stated versus applied sizing over the next month.",
                revert_criteria="Revert if sizing rationale becomes boilerplate.",
            ),
        ),
    )


def _context_with_history(tmp_path: Path) -> tuple[Session, AgentConfig, WeeklyAgentContext]:
    session = _session(tmp_path)
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    start, end = review_period(REVIEW_AT)
    daily = _run(session, key="daily:2026-08-18", scheduled_for=start + timedelta(days=3))
    record_performance_snapshot(
        session,
        run_id=daily.id,
        account=_account("10400"),
        as_of=daily.scheduled_for,
    )
    proposal = _buy("SPY")
    persist_trade_proposal(session, daily.id, proposal)
    record_decision_ledger(
        session,
        run_id=daily.id,
        as_of=daily.scheduled_for,
        proposals=(proposal,),
        approved_proposal_ids=frozenset({str(proposal.proposal_id)}),
    )
    version = record_strategy_version(session, document=STRATEGY, as_of=REVIEW_AT)
    review = _run(session, key="weekly:2026-08-22", scheduled_for=REVIEW_AT)
    context = assemble_weekly_context(
        session,
        run_id=review.id,
        as_of=REVIEW_AT,
        period_start=start,
        period_end=end,
        strategy=STRATEGY,
        strategy_version=version,
        portfolio_policy=POLICY,
        role=config.roles["weekly_strategist"],
    )
    assert context.performance.has_sample() is True
    assert session.scalar(select(Thesis)) is not None
    return session, config, context


def _runnable(tmp_path: Path) -> tuple[Session, AgentConfig, ReviewProvider, Path]:
    session, config, _context = _context_with_history(tmp_path)
    # The review run must claim its own key, so drop the placeholder created for the context.
    placeholder = session.scalar(select(Run).where(Run.run_key == "weekly:2026-08-22"))
    assert placeholder is not None
    session.delete(placeholder)
    session.commit()
    strategy_path = tmp_path / "strategy.md"
    strategy_path.write_text(STRATEGY)
    provider = ReviewProvider(_propose("Keep any single new position under ten percent of equity."))
    return session, config, provider, strategy_path
