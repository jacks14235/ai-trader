"""Integration checks for book teams, evidence projection and experimental provenance."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_books import (
    AS_OF,
    PROJECT_ROOT,
    BookMarket,
    _catalog,
    _decision,
    _open,
    _research_state,
    _run,
)
from typer.testing import CliRunner

from trader.agent.codex_cli import InvocationResponse
from trader.agent.config import AgentConfig, load_agent_config
from trader.agent.invocation import canonical_json, workflow_trail
from trader.agent.models import TradeProposal
from trader.agent.packets import (
    CitedClaim,
    NamedPacket,
    ResearchPacket,
    SymbolPacket,
    packet_content_hash,
)
from trader.agent.profile_context import (
    load_research_bundle,
    project_book_context,
    project_research_context,
)
from trader.agent.reasoning import AbstentionRecord, DissentDisposition, WaitTrigger
from trader.agent.runtime import ShadowDailyReasoningPipeline
from trader.books.models import FillAssumptions, SimulatedFillResult
from trader.books.references import record_spy_reference
from trader.books.runtime import (
    BookEvaluationPipeline,
    book_ledger,
    configured_book_evaluation_pipeline,
)
from trader.books.service import load_book_state, persist_simulated_fill
from trader.broker.models import Account, Quote
from trader.cli import app
from trader.ledger.history import load_latest_book_wait
from trader.persistence.models import (
    AgentDecisionRecord,
    AgentInvocation,
    BookEvaluation,
    BookExperimentPhase,
    BookReferencePoint,
    BrokerOrderRecord,
    PerformanceSnapshot,
    ResearchItem,
    ResearchItemSymbol,
    SimulatedFill,
    TradeProposalRecord,
)
from trader.persistence.repositories import (
    load_completed_agent_decisions,
    persist_agent_decision,
    persist_trade_proposal,
)
from trader.risk.config import load_risk_config
from trader.settings import Settings


class TeamProvider:
    provider_name = "recording_team"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.contexts = []
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        self.contexts.append(
            json.loads(
                kwargs["prompt"].split(
                    "The following JSON is the complete and only admitted context:\n", 1
                )[1]
            )
        )
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        text = response if isinstance(response, str) else response.model_dump_json()
        return InvocationResponse(text, "test provider", "", 120, 60)


def packet(evidence_id, *, claim_id="observation"):
    return ResearchPacket(
        symbols=(
            SymbolPacket(
                symbol="SPY",
                facts=(
                    CitedClaim(
                        claim_id=claim_id,
                        text="The admitted record describes current market conditions.",
                        evidence_ids=(evidence_id,),
                    ),
                ),
            ),
        )
    )


def challenged_packet(evidence_id, *, claim_id, category):
    claim = CitedClaim(
        claim_id=claim_id,
        text="The evidence may support a materially different interpretation.",
        evidence_ids=(evidence_id,),
    )
    return ResearchPacket(
        symbols=(SymbolPacket(symbol="SPY", **{category: (claim,)}),)
    )


def no_action(evidence_id):
    return _decision(evidence_id).model_copy(
        update={
            "status": "NO_ACTION",
            "proposals": (),
            "abstention": AbstentionRecord(
                classification="DELIBERATE_WAIT",
                insufficient_evidence="No decisive evidence.",
                triggers=(
                    WaitTrigger(
                        trigger_id="spy_better_price",
                        kind="PRICE",
                        description="Wait for SPY to reach a more attractive entry.",
                        symbol="SPY",
                        comparison="AT_OR_BELOW",
                        target_price="9.50",
                    ),
                ),
                reconsider_on="The next daily review with fresh market evidence.",
            ),
        }
    )


def pipeline(session, tmp_path, provider, *, config=None, catalog=None, market=None):
    return BookEvaluationPipeline(
        session,
        config or load_agent_config(PROJECT_ROOT / "config/agents.yaml"),
        market or BookMarket(),
        load_risk_config(PROJECT_ROOT / "config/risk.yaml"),
        prompt="Return an evidence-led decision.",
        portfolio_policy="# Policy\nPaper only.",
        provider=provider,
        catalog=catalog or _catalog(),
        project_root=tmp_path,
    )


def evaluate(evaluator, run, tmp_path, scan, research, *, allowed_symbols=frozenset({"SPY"})):
    return evaluator.run(
        run_id=run.id,
        run_directory=tmp_path / "raw" / run.id,
        as_of=AS_OF,
        scan=scan,
        research=research,
        allowed_symbols=allowed_symbols,
    )


def rich_book(session, tmp_path, name="team"):
    book = _open(session, tmp_path, name=name)
    book.process_profile = "research_then_adversary"
    session.commit()
    return book


def test_three_step_book_retains_all_predecessors_and_only_manager_proposes(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    book = rich_book(session, tmp_path)
    provider = TeamProvider(
        [packet(evidence), packet(evidence, claim_id="alternative"), _decision(evidence)]
    )
    result = evaluate(
        pipeline(session, tmp_path, provider, market=BookMarket(bid=Decimal("9.98"))),
        run,
        tmp_path,
        scan,
        research,
    )
    assert result.failures == ()
    summary = result.summaries[0]
    assert summary.filled_count == 1
    rows = workflow_trail(session, run.id)
    assert [row.role for row in rows] == [
        "research_compactor",
        "research_compactor",
        "daily_trader",
    ]
    assert rows[0].parent_invocation_id is None
    assert rows[1].parent_invocation_id == rows[0].id
    assert rows[2].parent_invocation_id == rows[1].id
    assert summary.invocation_trail == tuple(row.id for row in rows)
    assert rows[-1].step == f"book_{book.id.replace('-', '')}_team_decide"
    for context in provider.contexts[:2]:
        assert (
            not {"account", "positions", "strategy", "portfolio_policy", "open_theses"}
            & context.keys()
        )
    assert provider.contexts[0]["research_packets"] == []
    assert [item["step"] for item in provider.contexts[2]["research_packets"]] == [
        "packet",
        "adversary",
    ]
    assert "Packet and claim IDs" in provider.calls[2]["prompt"]
    proposal = session.scalar(select(TradeProposalRecord))
    assert proposal.book_id == book.id and proposal.agent_invocation_id == rows[-1].id
    assert len(session.scalars(select(TradeProposalRecord)).all()) == 1
    assert session.scalar(select(BrokerOrderRecord)) is None
    assert session.scalar(select(SimulatedFill)).book_id == book.id
    references = session.scalars(
        select(BookReferencePoint).order_by(BookReferencePoint.kind)
    ).all()
    assert [(item.kind, item.status) for item in references] == [
        ("CASH", "COMPLETED"),
        ("SPY_BUY_HOLD", "COMPLETED"),
    ]
    assert Decimal(references[0].equity) == Decimal("2000")
    assert Decimal(references[1].quantity) == Decimal("200")
    assert Decimal(references[1].entry_price) == Decimal("10.00")
    assert Decimal(references[1].equity) == Decimal("1998.00000000")
    evaluation = session.scalar(select(BookEvaluation))
    assert evaluation.status == "COMPLETED"
    assert evaluation.terminal_invocation_id == summary.invocation_id
    assert all(item.book_id == book.id for item in rows)
    assert all(item.book_evaluation_id == evaluation.id for item in rows)
    assert [item.model_profile for item in rows] == ["fast", "fast", "deep"]
    assert [item.reasoning_effort for item in rows] == ["medium", "medium", "high"]
    assert all(item.input_token_count == 120 for item in rows)
    assert all(item.output_token_count == 60 for item in rows)
    assert all(item.total_token_count == 180 for item in rows)
    assert all(item.cost_source == "NOT_REPORTED" for item in rows)
    usage = book_ledger(session, book)["model_usage"]
    assert usage["invocation_count"] == 3
    assert usage["by_model"]["gpt-5.6-terra"]["total_tokens"] == 360
    assert usage["by_model"]["gpt-5.6-sol"]["total_tokens"] == 180
    directory = tmp_path / "raw" / run.id
    manifest = json.loads((directory / "books/team/profile/result.json").read_text())
    consumed = manifest["steps"][-1]["consumed"]
    assert [item["invocation_id"] for item in consumed] == [row.id for row in rows[:2]]
    for envelope in provider.contexts[2]["research_packets"]:
        NamedPacket.model_validate(envelope)
    raw_bundle = (directory / "books/research_bundle.json").read_text().strip()
    inputs = json.loads(evaluation.manifest_json)["inputs"]
    assert hashlib.sha256(raw_bundle.encode()).hexdigest() == inputs["research_bundle_hash"]


def test_completed_no_action_becomes_book_waiting_memory(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    book = _open(session, tmp_path)
    result = evaluate(
        pipeline(session, tmp_path, TeamProvider([no_action(evidence)])),
        run,
        tmp_path,
        scan,
        research,
    )
    assert result.failures == ()

    prior = load_latest_book_wait(
        session,
        book_id=book.id,
        exclude_run_id="next-run",
        as_of=AS_OF + timedelta(days=1),
    )

    assert prior is not None
    assert prior.run_id == run.id
    assert prior.scope_symbols == ("SPY",)
    assert prior.triggers[0].trigger_id == "spy_better_price"
    assert len(prior.prior_evidence_content_hashes) == 1
    bundle = load_research_bundle(
        session,
        run_id=run.id,
        as_of=AS_OF,
        scan=scan,
        research=research,
    )
    waiting, _cutoff = pipeline(
        session, tmp_path, TeamProvider([])
    )._waiting_memory(prior, bundle=bundle, minimum_as_of=AS_OF)
    assert waiting[0].new_evidence_ids == ()
    assert waiting[0].trigger_assessments[0].status == "UNSATISFIED"
    assert waiting[0].reopenable is False


def test_spy_reference_failure_is_audited_without_failing_the_book(tmp_path):
    class MissingSpyMarket(BookMarket):
        def get_quote(self, symbol):
            raise RuntimeError(f"quote unavailable: {symbol}")

    session, run, scan, research, evidence = _research_state(tmp_path)
    _open(session, tmp_path)
    result = evaluate(
        pipeline(
            session,
            tmp_path,
            TeamProvider([no_action(evidence)]),
            market=MissingSpyMarket(),
        ),
        run,
        tmp_path,
        scan,
        research,
    )

    assert result.failures == ()
    points = session.scalars(
        select(BookReferencePoint).order_by(BookReferencePoint.kind)
    ).all()
    assert [(item.kind, item.status) for item in points] == [
        ("CASH", "COMPLETED"),
        ("SPY_BUY_HOLD", "FAILED"),
    ]
    assert points[1].error == "no SPY quote is available"


def test_spy_reference_buys_once_and_never_rebalances(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    book = _open(session, tmp_path)
    assert not evaluate(
        pipeline(session, tmp_path, TeamProvider([no_action(evidence)])),
        run,
        tmp_path,
        scan,
        research,
    ).failures
    first = session.scalar(
        select(BookReferencePoint).where(BookReferencePoint.kind == "SPY_BUY_HOLD")
    )
    assert first is not None
    next_as_of = AS_OF + timedelta(days=1)
    next_run = _run(session, "daily:reference-next", scheduled_for=next_as_of)
    evaluation = BookEvaluation(
        book_id=book.id,
        run_id=next_run.id,
        phase_id=session.scalar(select(BookExperimentPhase.id)),
        as_of=next_as_of,
        status="STARTED",
        manifest_json='{"inputs":{}}',
    )
    session.add(evaluation)
    session.commit()

    second = record_spy_reference(
        session,
        book=book,
        evaluation=evaluation,
        run_id=next_run.id,
        as_of=next_as_of,
        quote=Quote(
            symbol="SPY",
            bid="20.00",
            ask="20.02",
            timestamp=next_as_of,
            feed="test",
        ),
        assumptions=FillAssumptions(slippage_bps="25"),
    )

    assert second.quantity == first.quantity
    assert second.entry_price == first.entry_price
    assert second.cash == first.cash
    assert second.definition_hash == first.definition_hash
    assert second.mark_price == "20.01"
    assert Decimal(second.equity) > Decimal(first.equity)


def test_manager_must_address_every_consumed_contradiction_and_dissent(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    book = rich_book(session, tmp_path)
    compacted = challenged_packet(evidence, claim_id="weak_case", category="contradictions")
    challenged = challenged_packet(evidence, claim_id="alternative", category="dissent")
    decision = _decision(evidence).model_copy(
        update={
            "dissent_dispositions": (
                DissentDisposition(
                    packet_step="packet",
                    claim_id="weak_case",
                    resolution="ACCEPTED",
                    rationale="The contradiction lowers confidence but does not erase the setup.",
                    evidence_ids=(evidence,),
                ),
                DissentDisposition(
                    packet_step="adversary",
                    claim_id="alternative",
                    resolution="DEFERRED",
                    rationale="The alternative needs a specific confirming observation.",
                    evidence_ids=(evidence,),
                    defer_until=WaitTrigger(
                        trigger_id="alternative_confirmation",
                        kind="EVIDENCE",
                        description="Reassess when the next primary-source update is available.",
                        evidence_needed=(
                            "A primary-source update that distinguishes the explanations."
                        ),
                    ),
                ),
            )
        }
    )

    result = evaluate(
        pipeline(session, tmp_path, TeamProvider([compacted, challenged, decision])),
        run,
        tmp_path,
        scan,
        research,
    )

    assert result.failures == ()
    record = session.scalar(select(AgentDecisionRecord))
    assert record is not None
    assert record.book_id == book.id
    dispositions = json.loads(record.dissent_dispositions_json)
    assert [(item["packet_step"], item["resolution"]) for item in dispositions] == [
        ("packet", "ACCEPTED"),
        ("adversary", "DEFERRED"),
    ]
    with pytest.raises(ValueError, match="unnamed daily invocation"):
        persist_agent_decision(
            session,
            run.id,
            record.agent_invocation_id,
            decision,
        )


def test_unaddressed_consumed_dissent_fails_the_book_without_decision_or_trade(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    rich_book(session, tmp_path)
    challenged = challenged_packet(evidence, claim_id="missing_response", category="dissent")
    provider = TeamProvider([challenged, packet(evidence), _decision(evidence)])

    result = evaluate(pipeline(session, tmp_path, provider), run, tmp_path, scan, research)

    assert len(result.failures) == 1
    assert "did not address consumed dissent" in result.failures[0].reason
    assert session.scalar(select(AgentDecisionRecord)) is None
    assert session.scalar(select(TradeProposalRecord)) is None
    assert session.scalar(select(SimulatedFill)) is None


def test_failed_book_evaluation_keeps_attempt_but_not_completed_decision(tmp_path):
    class PostDecisionFailureMarket(BookMarket):
        def get_quote(self, symbol):
            raise RuntimeError(f"quote unavailable after terminal decision: {symbol}")

    session, run, scan, research, evidence = _research_state(tmp_path)
    book = _open(session, tmp_path)
    result = evaluate(
        pipeline(
            session,
            tmp_path,
            TeamProvider([_decision(evidence)]),
                market=PostDecisionFailureMarket(),
        ),
        run,
        tmp_path,
        scan,
        research,
    )

    assert len(result.failures) == 1
    record = session.scalar(select(AgentDecisionRecord))
    evaluation = session.scalar(select(BookEvaluation))
    assert record is not None and evaluation is not None
    assert record.book_evaluation_id == evaluation.id
    assert evaluation.status == "FAILED"
    assert load_completed_agent_decisions(
        session, run_ids=[run.id], book_id=book.id
    ) == ()


def test_book_profile_does_not_change_incumbent_daily_contract(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    rich_book(session, tmp_path)
    config = load_agent_config(PROJECT_ROOT / "config/agents.yaml")
    live_provider = TeamProvider([no_action(evidence)])
    live = ShadowDailyReasoningPipeline(
        session,
        config,
        prompt="Daily contract",
        strategy="# Strategy",
        portfolio_policy="# Policy",
        provider=live_provider,
    )
    result = live.run(
        run_id=run.id,
        run_directory=tmp_path / "raw" / run.id,
        as_of=AS_OF,
        account=Account(equity="2000", cash="2000", buying_power="2000"),
        positions=(),
        open_orders=(),
        scan=scan,
        research=research,
    )
    book_provider = TeamProvider([packet(evidence), packet(evidence), no_action(evidence)])
    assert not evaluate(
        pipeline(session, tmp_path, book_provider), run, tmp_path, scan, research
    ).failures
    assert result.decision.status == "NO_ACTION"
    assert "research_packets" not in live_provider.contexts[0]
    assert session.get(AgentInvocation, result.invocation_id).step == ""
    assert (tmp_path / "raw" / run.id / "agent/daily_trader/context.json").is_file()
    assert session.scalar(select(BrokerOrderRecord)) is None


@pytest.mark.parametrize(
    "failure", ["citation", "trade_field", "provider", "oversized", "terminal"]
)
def test_failed_team_cannot_persist_proposals_and_does_not_stop_sibling(tmp_path, failure):
    session, run, scan, research, evidence = _research_state(tmp_path)
    broken = rich_book(session, tmp_path, "aaa-broken")
    healthy = _open(session, tmp_path, name="zzz-healthy")
    responses = []
    if failure == "citation":
        responses.append(packet("f" * 64))
    elif failure == "trade_field":
        malformed = packet(evidence).model_dump(mode="json")
        malformed["proposals"] = [{"symbol": "SPY", "action": "BUY"}]
        responses.append(json.dumps(malformed))
    elif failure == "provider":
        responses.append(RuntimeError("provider failed"))
    elif failure == "oversized":
        responses.append(" " * 30_001)
    else:
        responses.extend([packet(evidence), packet(evidence), _decision("f" * 64)])
    responses.append(no_action(evidence))
    provider = TeamProvider(responses)
    result = evaluate(pipeline(session, tmp_path, provider), run, tmp_path, scan, research)
    assert [item.name for item in result.summaries] == [healthy.name]
    assert [item.book_name for item in result.failures] == [broken.name]
    evaluations = {item.book_id: item for item in session.scalars(select(BookEvaluation))}
    assert evaluations[broken.id].status == "FAILED"
    assert evaluations[healthy.id].status == "COMPLETED"
    assert session.scalar(select(TradeProposalRecord)) is None
    assert session.scalar(select(SimulatedFill)) is None
    assert session.scalar(select(BrokerOrderRecord)) is None
    failures = list((tmp_path / "raw" / run.id / "agent").rglob("failure.json"))
    assert len(failures) == 1
    trail = workflow_trail(session, run.id)
    assert sum(row.status == "FAILED" for row in trail) == 1


def test_terminal_proposal_batch_rolls_back_if_any_proposal_conflicts(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    _open(session, tmp_path)
    conflict = _decision(evidence).proposals[0]
    persist_trade_proposal(session, run.id, conflict)
    fresh = _decision(evidence).proposals[0]
    decision = _decision(evidence).model_copy(update={"proposals": (fresh, conflict)})

    result = evaluate(
        pipeline(session, tmp_path, TeamProvider([decision])),
        run,
        tmp_path,
        scan,
        research,
    )

    assert len(result.failures) == 1
    proposals = session.scalars(select(TradeProposalRecord)).all()
    assert [item.id for item in proposals] == [str(conflict.proposal_id)]
    assert proposals[0].book_id is None
    assert session.scalar(select(AgentDecisionRecord)) is None
    assert session.scalar(select(SimulatedFill)) is None


def test_repeating_a_run_does_not_overwrite_artifacts_or_invoke_again(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    _open(session, tmp_path)
    provider = TeamProvider([no_action(evidence)])
    evaluator = pipeline(session, tmp_path, provider)
    assert not evaluate(evaluator, run, tmp_path, scan, research).failures
    root = tmp_path / "raw" / run.id
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(FileExistsError):
        evaluate(evaluator, run, tmp_path, scan, research)
    assert {path: path.read_bytes() for path in before} == before
    assert len(provider.calls) == 1
    assert len(session.scalars(select(BookEvaluation)).all()) == 1


def test_role_limits_reserve_whole_packets_before_trimming_raw_evidence(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    item = session.get(ResearchItem, evidence)
    item.normalized_text = 'Raw \\" evidence 雪 ' * 5000
    session.commit()
    bundle = load_research_bundle(session, run_id=run.id, as_of=AS_OF, scan=scan, research=research)
    config = load_agent_config(PROJECT_ROOT / "config/agents.yaml")
    role = config.roles["research_compactor"].model_copy(
        update={
            "max_context_chars": 10_000,
            "max_document_chars": 20_000,
        }
    )
    memo = ResearchPacket(limitations=("q" * 1000,) * 6)
    envelope = NamedPacket(
        step="packet", invocation_id="producer", packet=memo, content_hash=packet_content_hash(memo)
    )
    projected = project_research_context(bundle, role, (envelope,))
    assert len(canonical_json(projected)) <= 10_000
    assert projected.research_packets == (envelope,)
    assert len(projected.deep_evidence[0].excerpt) < 4000
    assert "[TRUNCATED]" in projected.deep_evidence[0].excerpt
    small_role = role.model_copy(update={"max_document_chars": 500})
    assert len(project_research_context(bundle, small_role, ()).deep_evidence[0].excerpt) == 500
    too_large = ResearchPacket(limitations=("q" * 1000,) * 11)
    huge = NamedPacket(
        step="packet",
        invocation_id="producer",
        packet=too_large,
        content_hash=packet_content_hash(too_large),
    )
    with pytest.raises(ValueError, match="including consumed packets"):
        project_research_context(bundle, role, (huge,))
    undeclared = role.model_copy(
        update={"context_sources": ("candidate_overview", "deep_research")}
    )
    with pytest.raises(ValueError, match="context source"):
        project_research_context(bundle, undeclared, (envelope,))
    private_source = role.model_copy(
        update={"context_sources": (*role.context_sources, "positions")}
    )
    with pytest.raises(ValueError, match="cannot supply"):
        project_research_context(bundle, private_source, ())
    manager_role = config.roles["daily_trader"].model_copy(
        update={
            "context_sources": (*config.roles["daily_trader"].context_sources, "research_packets"),
            "max_context_chars": 10_000,
        }
    )
    manager = project_book_context(
        bundle,
        manager_role,
        (envelope,),
        strategy="Strategy",
        portfolio_policy="Policy",
        account=Account(equity="2000", cash="2000", buying_power="2000"),
        positions=(),
        recent_decisions=(),
    )
    assert manager.research_packets == (envelope,)
    assert len(canonical_json(manager)) <= 10_000


@pytest.mark.parametrize("defect", ["stale", "future", "wrong_symbol", "crossed", "nonfinite"])
def test_book_valuation_rejects_invalid_held_position_quotes(tmp_path, defect):
    class ValuationMarket(BookMarket):
        def get_quote(self, symbol):
            quote = super().get_quote(symbol)
            changes = {}
            if defect == "stale":
                changes["timestamp"] = AS_OF - timedelta(seconds=901)
            elif defect == "future":
                changes["timestamp"] = AS_OF + timedelta(seconds=1)
            elif defect == "wrong_symbol":
                changes["symbol"] = "QQQ"
            elif defect == "crossed":
                changes.update({"bid": Decimal("11"), "ask": Decimal("10")})
            else:
                changes["bid"] = Decimal("NaN")
            return quote.model_copy(update=changes)

    session, _run_record, _scan, _research, _evidence = _research_state(tmp_path)
    _open(session, tmp_path)
    evaluator = pipeline(
        session,
        tmp_path,
        TeamProvider([]),
        market=ValuationMarket(),
    )

    with pytest.raises(ValueError, match="cannot price SPY"):
        evaluator._prices(("SPY",), minimum_as_of=AS_OF)


def test_book_peak_drawdown_ignores_future_performance_snapshots(tmp_path):
    session, run, _scan, _research, _evidence = _research_state(tmp_path)
    book = _open(session, tmp_path)
    session.add(
        PerformanceSnapshot(
            run_id=run.id,
            book_id=book.id,
            period="daily",
            captured_at=AS_OF + timedelta(days=1),
            equity="10000",
            cash="10000",
            drawdown_pct="0",
            raw_json='{"peak_equity":"10000"}',
        )
    )
    session.commit()
    evaluator = pipeline(session, tmp_path, TeamProvider([]))

    assert evaluator._peak_drawdown(
        load_book_state(session, book, as_of=AS_OF), {}, as_of=AS_OF
    ) == 0


@pytest.mark.parametrize("defect", ["missing", "foreign_run", "future", "cutoff"])
def test_invalid_research_fails_all_books_without_model_calls(tmp_path, defect):
    session, run, scan, research, evidence = _research_state(tmp_path)
    _open(session, tmp_path)
    if defect == "missing":
        research = replace(research, persisted_research_ids=("f" * 64,))
    elif defect == "foreign_run":
        run = _run(session, "daily-test:foreign")
    elif defect == "future":
        session.get(ResearchItem, evidence).published_at = AS_OF + timedelta(seconds=1)
        session.commit()
    else:
        scan = scan.model_copy(update={"as_of": AS_OF - timedelta(seconds=1)})
    provider = TeamProvider([])
    result = evaluate(pipeline(session, tmp_path, provider), run, tmp_path, scan, research)
    assert len(result.failures) == 1
    assert provider.calls == []
    assert session.scalar(select(AgentInvocation)) is None
    assert session.scalar(select(TradeProposalRecord)) is None


def test_market_retrieval_after_cutoff_uses_underlying_observation_time(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    item = session.get(ResearchItem, evidence)
    observed = AS_OF - timedelta(minutes=5)
    item.published_at = AS_OF + timedelta(minutes=2)
    item.retrieved_at = AS_OF + timedelta(minutes=2)
    item.metadata_json = json.dumps({"data_timestamps": [observed.isoformat()]})
    session.commit()

    bundle = load_research_bundle(
        session, run_id=run.id, as_of=AS_OF, scan=scan, research=research
    )

    assert bundle.evidence_catalog[0].published_at == observed
    assert bundle.documents[0].effective_at == observed
    assert bundle.documents[0].recorded_published_at == AS_OF + timedelta(minutes=2)


def test_market_observation_after_cutoff_is_rejected_even_if_retrieval_is_later(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    item = session.get(ResearchItem, evidence)
    item.published_at = AS_OF + timedelta(minutes=2)
    item.retrieved_at = AS_OF + timedelta(minutes=2)
    item.metadata_json = json.dumps(
        {"data_timestamps": [(AS_OF + timedelta(minutes=3)).isoformat()]}
    )
    session.commit()

    with pytest.raises(ValueError, match="postdates the profile cutoff"):
        load_research_bundle(
            session, run_id=run.id, as_of=AS_OF, scan=scan, research=research
        )


def test_research_plan_must_belong_to_the_supplied_candidate_scan(tmp_path):
    session, run, scan, research, _evidence = _research_state(tmp_path)
    mismatched = research.plan.model_copy(update={"candidate_symbols": ("QQQ",)})
    research = replace(research, plan=mismatched)

    with pytest.raises(ValueError, match="exactly match"):
        load_research_bundle(
            session, run_id=run.id, as_of=AS_OF, scan=scan, research=research
        )


def test_missing_book_catalog_is_deferred_and_isolated_from_completed_run(tmp_path):
    session, run, scan, research, _evidence = _research_state(tmp_path)
    _open(session, tmp_path)
    run.status = "COMPLETED"
    session.commit()
    settings = Settings(
        _env_file=None,
        trader_reasoning_enabled=True,
        trader_agents_config=PROJECT_ROOT / "config/agents.yaml",
        trader_pipelines_config=tmp_path / "missing-pipelines.yaml",
        trader_risk_config=PROJECT_ROOT / "config/risk.yaml",
        trader_portfolio_policy=PROJECT_ROOT / "knowledge/portfolio_policy.md",
    )
    service = configured_book_evaluation_pipeline(
        settings, session, BookMarket(), provider=TeamProvider([])
    )
    assert service is not None
    directory = tmp_path / "raw" / run.id
    directory.mkdir(parents=True)

    result = service.run(
        run_id=run.id,
        run_directory=directory,
        as_of=AS_OF,
        scan=scan,
        research=research,
        allowed_symbols=frozenset({"SPY"}),
    )

    assert len(result.failures) == 1
    assert "missing-pipelines.yaml" in result.failures[0].reason
    assert session.get(type(run), run.id).status == "COMPLETED"
    assert session.scalar(select(AgentInvocation)) is None
    assert json.loads((directory / "books_summary.json").read_text())["failure_count"] == 1


def test_no_active_books_never_reads_the_optional_catalog(tmp_path):
    session, _run_record, _scan, _research, _evidence = _research_state(tmp_path)
    settings = Settings(
        _env_file=None,
        trader_reasoning_enabled=True,
        trader_pipelines_config=tmp_path / "missing-pipelines.yaml",
    )
    assert configured_book_evaluation_pipeline(settings, session, BookMarket()) is None


def test_role_disabled_later_in_profile_is_rejected_before_any_call(tmp_path):
    session, run, scan, research, _evidence = _research_state(tmp_path)
    rich_book(session, tmp_path)
    config = load_agent_config(PROJECT_ROOT / "config/agents.yaml")
    data = config.model_dump(mode="json")
    data["roles"]["research_compactor"]["enabled"] = False
    provider = TeamProvider([])
    result = evaluate(
        pipeline(session, tmp_path, provider, config=AgentConfig.model_validate(data)),
        run,
        tmp_path,
        scan,
        research,
    )
    assert "disabled" in result.failures[0].reason
    assert provider.calls == []


def test_same_bundle_loaded_once_and_book_packets_never_cross_books(tmp_path, monkeypatch):
    import trader.books.runtime as runtime

    session, run, scan, research, evidence = _research_state(tmp_path)
    rich_book(session, tmp_path, "aaa-team")
    rich_book(session, tmp_path, "bbb-team")
    original = runtime.load_research_bundle
    loads = []

    def load(*args, **kwargs):
        loads.append(kwargs["run_id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "load_research_bundle", load)
    provider = TeamProvider([packet(evidence), packet(evidence), no_action(evidence)] * 2)
    assert not evaluate(
        pipeline(session, tmp_path, provider), run, tmp_path, scan, research
    ).failures
    assert loads == [run.id]
    assert (
        provider.contexts[0]["research_packets"] == provider.contexts[3]["research_packets"] == []
    )
    first = {p["invocation_id"] for p in provider.contexts[2]["research_packets"]}
    second = {p["invocation_id"] for p in provider.contexts[5]["research_packets"]}
    assert first.isdisjoint(second)


def test_runtime_phases_change_for_note_versions_but_not_daily_inputs(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    book = _open(session, tmp_path)
    note = tmp_path / "note.md"
    note.write_text("Investigate uncertainty.")
    book.operating_note_path = str(note)
    session.commit()
    original = session.get(ResearchItem, evidence)
    source_fields = {
        col.name: getattr(original, col.name)
        for col in ResearchItem.__table__.columns
        if col.name not in {"id", "run_id"}
    }
    phases = []
    for index, note_text in enumerate(
        [
            "Investigate uncertainty.",
            "Investigate uncertainty.",
            "Investigate alternative explanations.",
            "Investigate uncertainty.",
        ]
    ):
        if index:
            run = _run(session, key=f"daily-test:phase-{index}")
            evidence = hashlib.sha256(f"{run.id}:source".encode()).hexdigest()
            session.add(ResearchItem(id=evidence, run_id=run.id, **source_fields))
            session.add(ResearchItemSymbol(research_id=evidence, symbol="SPY"))
            session.commit()
            research = replace(research, persisted_research_ids=(evidence,))
        note.write_text(note_text)
        provider = TeamProvider([no_action(evidence)])
        result = evaluate(
            pipeline(session, tmp_path, provider),
            run,
            tmp_path,
            scan,
            research,
            allowed_symbols=frozenset({"SPY", "QQQ"}) if index else frozenset({"SPY"}),
        )
        assert not result.failures
        phases.append(result.summaries[0].experiment_phase_id)
    assert phases[0] == phases[1]
    assert len({phases[0], phases[2], phases[3]}) == 3
    phase_rows = session.scalars(
        select(BookExperimentPhase).order_by(BookExperimentPhase.ordinal)
    ).all()
    assert [row.ordinal for row in phase_rows] == [1, 2, 3]
    assert phase_rows[0].configuration_hash == phase_rows[2].configuration_hash
    assert (
        json.loads(phase_rows[0].manifest_json)["configuration"]["model_identity_pinned"] is True
    )


def test_cli_reports_profiles_and_opens_simulated_books_with_selected_note(tmp_path, monkeypatch):
    import trader.cli as cli

    session, _run_record, _scan, _research, _evidence = _research_state(tmp_path)
    _open(session, tmp_path, name="fixture-book")
    (tmp_path / "config").mkdir()
    for name in ("risk.yaml", "agents.yaml", "pipelines.yaml"):
        (tmp_path / "config" / name).write_bytes((PROJECT_ROOT / "config" / name).read_bytes())
    settings = Settings(
        _env_file=None,
        trader_agents_config=tmp_path / "config/agents.yaml",
        trader_pipelines_config=tmp_path / "config/pipelines.yaml",
        trader_risk_config=tmp_path / "config/risk.yaml",
    )
    monkeypatch.setattr(cli, "database_service", lambda: (settings, session))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    note = tmp_path / "note.md"
    note.write_text("Prioritize disconfirming evidence.")
    args = [
        "books",
        "open",
        "new-team",
        "--cash",
        "2000",
        "--strategy",
        "fixture-book.md",
        "--profile",
        "research_then_adversary",
        "--operating-note",
        "note.md",
    ]
    opened = CliRunner().invoke(app, args)
    assert opened.exit_code == 0, opened.output
    assert json.loads(opened.stdout)["process_profile"] == "research_then_adversary"
    listed = CliRunner().invoke(app, ["books", "list"])
    assert listed.exit_code == 0 and "research_then_adversary" in listed.stdout
    shown = CliRunner().invoke(app, ["books", "show", "new-team"])
    assert shown.exit_code == 0 and "experiment_phases" in shown.stdout
    validated = CliRunner().invoke(app, ["agents", "validate"])
    assert validated.exit_code == 0, validated.output
    assert (
        json.loads(validated.stdout)["simulated_book_profiles"]["profiles"][
            "research_then_adversary"
        ]["step_count"]
        == 3
    )


def sell_everything(evidence_id):
    return _decision(evidence_id).model_copy(
        update={
            "proposals": (
                TradeProposal(
                    proposal_id=uuid4(),
                    symbol="SPY",
                    action="SELL",
                    target_position_pct="0",
                    confidence=0.6,
                    time_horizon="days",
                    rationale="The book exits a holding the shared slate no longer covers.",
                    key_risks=["The exit may prove early."],
                    invalidation_conditions=["Price reclaims its prior support."],
                    evidence_ids=[evidence_id],
                    min_acceptable_price="9.98",
                ),
            ),
        }
    )


def _hold(session, book, *, as_of, qty=Decimal("10"), key="daily:prior"):
    persist_simulated_fill(
        session,
        book=book,
        run_id=_run(session, key=key).id,
        fill=SimulatedFillResult(
            proposal_id=str(uuid4()),
            symbol="SPY",
            side="buy",
            outcome="FILLED",
            qty=qty,
            price=Decimal("10"),
            commission=Decimal("0"),
        ),
        assumptions=FillAssumptions(),
        as_of=as_of,
    )


def test_a_book_may_exit_a_holding_the_shared_slate_no_longer_admits(tmp_path):
    """The live line admits its own positions; a book must admit its own or it cannot sell."""
    session, run, scan, research, evidence = _research_state(tmp_path)
    book = _open(session, tmp_path)
    _hold(session, book, as_of=AS_OF - timedelta(days=1))
    result = evaluate(
        pipeline(session, tmp_path, TeamProvider([sell_everything(evidence)])),
        run,
        tmp_path,
        scan,
        research,
        allowed_symbols=frozenset(),
    )
    assert result.failures == ()
    summary = result.summaries[0]
    assert (summary.approved_count, summary.filled_count) == (1, 1)
    assert load_book_state(session, book).positions == ()


def test_a_book_counts_daily_activity_on_the_eastern_trading_day(tmp_path):
    """A UTC day rolls over at 19:00/20:00 Eastern and would reset per-day limits mid-session."""
    session, _run_record, _scan, _research, _evidence = _research_state(tmp_path)
    book = _open(session, tmp_path)
    _hold(session, book, as_of=datetime(2026, 8, 20, 23, 30, tzinfo=UTC))
    evaluator = pipeline(session, tmp_path, TeamProvider([]))
    same_session_after_utc_midnight = datetime(2026, 8, 21, 1, 0, tzinfo=UTC)
    orders, exposure, trades = evaluator._activity(book.id, same_session_after_utc_midnight)
    assert (orders, exposure, trades) == (1, Decimal("100"), {"SPY": 1})
    assert evaluator._activity(book.id, datetime(2026, 8, 21, 16, 0, tzinfo=UTC)) == (
        0,
        Decimal("0"),
        {},
    )


def test_code_identity_belongs_to_the_evaluation_not_the_experiment_phase(tmp_path):
    session, run, scan, research, evidence = _research_state(tmp_path)
    _open(session, tmp_path)
    result = evaluate(
        pipeline(session, tmp_path, TeamProvider([_decision(evidence)])),
        run,
        tmp_path,
        scan,
        research,
    )
    assert result.failures == ()
    phase = session.scalars(select(BookExperimentPhase)).one()
    evaluation = session.scalars(select(BookEvaluation)).one()
    assert "implementation_hash" not in json.loads(phase.manifest_json)["configuration"]
    assert len(json.loads(evaluation.manifest_json)["inputs"]["implementation_hash"]) == 64
