import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from uuid import uuid4

import pytest

from trader.agent.codex_cli import (
    CodexCLIInvocationError,
    CodexCLIProvider,
    codex_output_schema,
)
from trader.agent.config import AgentConfig, ModelProfile, load_agent_config
from trader.agent.models import TradeProposal
from trader.agent.reasoning import (
    DailyAgentContext,
    DailyDecision,
    DailyUpdate,
    assemble_daily_context,
    validate_daily_decision,
    verify_daily_context_sources,
)
from trader.agent.runtime import ShadowDailyReasoningPipeline
from trader.broker.models import Account, Position
from trader.ledger.models import RecentRunDecision
from trader.ledger.service import record_run_report
from trader.persistence.db import create_session_factory
from trader.persistence.models import AgentInvocation, Run, Thesis, TradeProposalRecord
from trader.persistence.repositories import persist_research_item, persist_trade_proposal
from trader.research.artifacts import ResearchArtifact
from trader.research.collection import ResearchCollection
from trader.research.models import ResearchPlan, ResearchRequest
from trader.research.service import ResearchRunResult
from trader.universe.models import (
    CandidateSignal,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)

PROJECT_ROOT = Path(__file__).parents[2]


class FakeReasoningProvider:
    provider_name = "test"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.received_prompt = ""

    def invoke(self, **kwargs: object):  # type: ignore[no-untyped-def]
        from trader.agent.codex_cli import InvocationResponse

        self.received_prompt = str(kwargs["prompt"])
        return InvocationResponse(self.response_text, "provider stdout", "")


def test_agent_config_is_strict_and_forbids_execution_permission() -> None:
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    unsafe = config.model_dump()
    unsafe["roles"]["daily_trader"]["permissions"]["can_submit_orders"] = True

    with pytest.raises(ValueError, match="can_submit_orders"):
        AgentConfig.model_validate(unsafe)


def test_context_is_bounded_and_decision_must_use_admitted_evidence(tmp_path: Path) -> None:
    session, run, scan, research, research_id = _research_state(tmp_path)
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    context = assemble_daily_context(
        session,
        run_id=run.id,
        as_of=scan.as_of,
        strategy="Strategy",
        portfolio_policy="Policy",
        account=Account(equity="2000", cash="2000", buying_power="2000"),
        positions=(),
        open_orders=(),
        scan=scan,
        research=research,
        role=config.roles["daily_trader"],
    )
    valid = DailyDecision(
        status="PROPOSE_TRADES",
        market_assessment="The evidence supports a bounded paper proposal.",
        strongest_counterargument="The observed move may reverse.",
        daily_update=_briefing(),
        proposals=(
            TradeProposal(
                proposal_id=uuid4(),
                symbol="SPY",
                action="BUY",
                target_notional_usd="100",
                confidence=0.6,
                time_horizon="days",
                rationale="Market context is constructive.",
                key_risks=["Reversal"],
                invalidation_conditions=["Price loses support"],
                evidence_ids=[research_id],
                max_acceptable_price="650",
            ),
        ),
    )

    assert validate_daily_decision(valid, context) is valid
    invented = valid.model_copy(
        update={
            "proposals": (
                valid.proposals[0].model_copy(update={"evidence_ids": ["f" * 64]}),
            )
        }
    )
    with pytest.raises(ValueError, match="outside the invocation manifest"):
        validate_daily_decision(invented, context)


def test_declaring_a_context_source_the_assembler_cannot_supply_fails_closed() -> None:
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    role = config.roles["daily_trader"]
    verify_daily_context_sources(role)

    undelivered = role.model_copy(
        update={"context_sources": (*role.context_sources, "weekly_performance")}
    )
    with pytest.raises(ValueError, match="weekly_performance"):
        verify_daily_context_sources(undelivered)


def test_context_supplies_prior_decisions_and_open_theses_as_memory(tmp_path: Path) -> None:
    session, run, scan, research, research_id = _research_state(tmp_path)
    prior = Run(
        run_key="daily:2026-08-21",
        scheduled_for=scan.as_of - timedelta(days=1),
        config_hash="config",
        status="COMPLETED",
    )
    thesis = Thesis(
        symbol="SPY",
        title="SPY — the regime still favors the benchmark",
        status="active",
        summary="Rationale: breadth keeps improving.",
        confidence=0.55,
        invalidation_json=json.dumps(["Breadth narrows"]),
        created_at=scan.as_of - timedelta(days=1),
        updated_at=scan.as_of - timedelta(days=1),
    )
    session.add_all([prior, thesis])
    session.commit()
    persist_trade_proposal(
        session,
        prior.id,
        TradeProposal(
            symbol="SPY",
            action="BUY",
            target_notional_usd="100",
            confidence=0.55,
            time_horizon="weeks",
            rationale="Breadth keeps improving.",
            evidence_ids=[],
            max_acceptable_price="650",
        ),
    )
    record_run_report(
        session,
        run_id=prior.id,
        report_path="daily_report.md",
        content="# Report\n",
        summary="PROPOSE_TRADES: one entry authorized.",
    )
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")

    context = assemble_daily_context(
        session,
        run_id=run.id,
        as_of=scan.as_of,
        strategy="Strategy",
        portfolio_policy="Policy",
        account=Account(equity="2000", cash="2000", buying_power="2000"),
        positions=(
            Position(symbol="QQQ", qty="1", market_value="500", current_price="500"),
        ),
        open_orders=(),
        scan=scan,
        research=research,
        role=config.roles["daily_trader"],
    )

    assert [record.run_key for record in context.recent_decisions] == ["daily:2026-08-21"]
    assert context.recent_decisions[0].decision_summary == "PROPOSE_TRADES: one entry authorized."
    assert [item.symbol for item in context.open_theses] == ["SPY"]
    assert context.open_theses[0].thesis_id == thesis.id

    def proposal(**overrides: object) -> DailyDecision:
        return DailyDecision(
            status="PROPOSE_TRADES",
            market_assessment="The prior thesis still holds.",
            strongest_counterargument="Breadth could narrow quickly.",
            daily_update=_briefing(),
            proposals=(
                TradeProposal(
                    symbol="SPY",
                    action="BUY",
                    target_notional_usd="100",
                    confidence=0.6,
                    time_horizon="weeks",
                    rationale="Adding to the existing position.",
                    evidence_ids=[research_id],
                    max_acceptable_price="650",
                    **overrides,  # type: ignore[arg-type]
                ),
            ),
        )

    continued = proposal(thesis_id=thesis.id)
    assert validate_daily_decision(continued, context) is continued
    with pytest.raises(ValueError, match="outside the supplied context"):
        validate_daily_decision(proposal(thesis_id=uuid4()), context)

    mismatched = continued.model_copy(
        update={"proposals": (continued.proposals[0].model_copy(update={"symbol": "QQQ"}),)}
    )
    with pytest.raises(ValueError, match="belonging to another symbol"):
        validate_daily_decision(mismatched, context)


def test_context_refuses_decisions_that_postdate_the_cutoff(tmp_path: Path) -> None:
    session, run, scan, research, _ = _research_state(tmp_path)
    later = Run(
        run_key="daily:2026-08-23",
        scheduled_for=scan.as_of + timedelta(days=1),
        config_hash="config",
        status="COMPLETED",
    )
    session.add(later)
    session.commit()
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")

    context = assemble_daily_context(
        session,
        run_id=run.id,
        as_of=scan.as_of,
        strategy="Strategy",
        portfolio_policy="Policy",
        account=Account(equity="2000", cash="2000", buying_power="2000"),
        positions=(),
        open_orders=(),
        scan=scan,
        research=research,
        role=config.roles["daily_trader"],
    )

    assert context.recent_decisions == ()

    payload = context.model_dump()
    payload["recent_decisions"] = (
        RecentRunDecision(
            run_id=later.id,
            run_key=later.run_key,
            scheduled_for=later.scheduled_for,
        ).model_dump(),
    )
    with pytest.raises(ValueError, match="cannot postdate the context cutoff"):
        DailyAgentContext.model_validate(payload)


def test_shadow_pipeline_persists_invocation_and_never_executes(tmp_path: Path) -> None:
    session, run, scan, research, research_id = _research_state(tmp_path)
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    response = DailyDecision(
        status="NO_ACTION",
        market_assessment="No sufficiently asymmetric setup.",
        strongest_counterargument="A short-term move remains possible.",
        no_action_reason="Evidence is insufficient.",
        daily_update=_briefing(),
    ).model_dump_json()
    provider = FakeReasoningProvider(response)
    pipeline = ShadowDailyReasoningPipeline(
        session,
        config,
        prompt="Return structured output only.",
        strategy="Strategy",
        portfolio_policy="Policy",
        provider=provider,
    )
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    result = pipeline.run(
        run_id=run.id,
        run_directory=run_directory,
        as_of=scan.as_of,
        account=Account(equity="2000", cash="2000", buying_power="2000"),
        positions=(),
        open_orders=(),
        scan=scan,
        research=research,
    )

    invocation = session.get(AgentInvocation, result.invocation_id)
    assert invocation is not None
    assert invocation.status == "COMPLETED"
    assert invocation.evidence_manifest_hash is not None
    assert (invocation.role, invocation.step, invocation.attempt) == ("daily_trader", "", 1)
    assert invocation.parent_invocation_id is None
    assert invocation.purpose == "daily_trader_paper_proposal"
    assert invocation.request_path == "agent/daily_trader/request.json"
    assert invocation.response_path == "agent/daily_trader/response.json"
    assert research_id in provider.received_prompt
    assert session.query(TradeProposalRecord).count() == 0
    assert (run_directory / "agent" / "daily_trader" / "response.json").is_file()


def test_codex_cli_uses_stdin_schema_ephemeral_read_only_and_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        response_path = Path(command[command.index("--output-last-message") + 1])
        response_path.write_text('{"status":"ok"}', encoding="utf-8")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("trader.agent.codex_cli.subprocess.run", fake_run)
    provider = CodexCLIProvider()
    response = provider.invoke(
        prompt="evidence only",
        output_schema={"type": "object"},
        profile=ModelProfile(provider="codex_cli", model=None, reasoning_effort="medium"),
        timeout_seconds=30,
        max_output_chars=1_000,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    assert captured["shell"] is False
    assert captured["input"] == "evidence only"
    assert response.response_text == '{"status":"ok"}'


def test_codex_output_schema_requires_every_property_and_removes_defaults() -> None:
    schema = codex_output_schema(DailyDecision.model_json_schema())
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert schema["required"] == list(properties)
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    assert "DailyUpdate" in definitions
    update = definitions["DailyUpdate"]
    assert isinstance(update, dict)
    update_properties = update["properties"]
    assert isinstance(update_properties, dict)
    assert update["required"] == list(update_properties)
    proposal = definitions["TradeProposal"]
    assert isinstance(proposal, dict)
    proposal_properties = proposal["properties"]
    assert isinstance(proposal_properties, dict)
    assert proposal["required"] == list(proposal_properties)
    assert '"default"' not in json.dumps(schema)
    assert '"pattern"' not in json.dumps(schema)


def test_codex_cli_surfaces_jsonl_error_when_stderr_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Invalid schema: every property must be required"

    def fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        del kwargs
        stdout = json.dumps({"type": "error", "message": message}) + "\n"
        return CompletedProcess(command, 1, stdout=stdout, stderr="")

    monkeypatch.setattr("trader.agent.codex_cli.subprocess.run", fake_run)
    provider = CodexCLIProvider()
    with pytest.raises(CodexCLIInvocationError, match=message) as raised:
        provider.invoke(
            prompt="evidence only",
            output_schema={"type": "object", "properties": {}, "required": []},
            profile=ModelProfile(
                provider="codex_cli", model=None, reasoning_effort="medium"
            ),
            timeout_seconds=30,
            max_output_chars=1_000,
        )

    assert message in raised.value.stdout
    assert raised.value.stderr == ""


def _briefing(**overrides: object) -> DailyUpdate:
    payload: dict[str, object] = {
        "headline": "Cash is still a position",
        "lesson_title": "Waiting is a decision",
        "lesson": "A portfolio that does not trade is still making a choice about risk.",
        "overview": "No idea cleared the evidence bar, so the paper account stays as it is.",
        "next_day_plan": "Look again at the same names only if the evidence changes.",
    }
    payload.update(overrides)
    return DailyUpdate.model_validate(payload)


def _research_state(
    tmp_path: Path,
) -> tuple[object, Run, UniverseScan, ResearchRunResult, str]:
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    as_of = datetime(2026, 8, 22, 14, tzinfo=UTC)
    run = Run(run_key="daily:test", scheduled_for=as_of, config_hash="config")
    session.add(run)
    session.commit()
    content_hash = hashlib.sha256(b"payload").hexdigest()
    research_id = hashlib.sha256(f"{run.id}:{content_hash}".encode()).hexdigest()
    persist_research_item(
        session,
        research_id=research_id,
        run_id=run.id,
        symbols=("SPY",),
        source_tier="BROKER",
        source_type="MARKET_DATA",
        source_name="alpaca-market-context",
        provider="alpaca",
        provider_item_id="SPY:context",
        research_question_id="a" * 64,
        research_question="What changed?",
        raw_artifact_path="research/alpaca/context.json",
        normalized_summary="SPY market context summary",
        normalized_text="SPY detailed market context",
        published_at=as_of - timedelta(minutes=5),
        retrieved_at=as_of,
        content_hash=content_hash,
        headline="SPY market context",
    )
    asset = UniverseAsset(
        symbol="SPY",
        name="SPDR S&P 500 ETF",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )
    scan = UniverseScan(
        as_of=as_of,
        asset_content_hash="b" * 64,
        eligible_assets=(asset,),
        candidates=(
            ResearchCandidate(
                symbol="SPY",
                score=100,
                asset=asset,
                signals=(CandidateSignal(source="BENCHMARK"),),
            ),
        ),
        most_active_volume_updated_at=as_of,
        most_active_trades_updated_at=as_of,
        market_movers_updated_at=as_of,
        skipped_screener_symbols=0,
    )
    question = ResearchRequest.create(
        symbol="SPY",
        question_type="MARKET_CONTEXT",
        query="What changed?",
        window_start=as_of - timedelta(hours=1),
        window_end=as_of,
        priority=100,
    )
    research = ResearchRunResult(
        plan=ResearchPlan(
            as_of=as_of,
            candidate_symbols=("SPY",),
            deep_symbols=("SPY",),
            questions=(question,),
        ),
        collection=ResearchCollection(batches=(), request_count=1, response_bytes=7),
        artifacts={
            "document": ResearchArtifact(
                relative_path="alpaca/context.json",
                content_hash=content_hash,
                byte_count=7,
                created=True,
            )
        },
        persisted_research_ids=(research_id,),
        elapsed_seconds=0.1,
    )
    return session, run, scan, research, research_id
