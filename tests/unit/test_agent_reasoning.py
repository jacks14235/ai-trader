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
    DailyDecision,
    assemble_daily_context,
    validate_daily_decision,
)
from trader.agent.runtime import ShadowDailyReasoningPipeline
from trader.broker.models import Account
from trader.persistence.db import create_session_factory
from trader.persistence.models import AgentInvocation, Run, TradeProposalRecord
from trader.persistence.repositories import persist_research_item
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


def test_shadow_pipeline_persists_invocation_and_never_executes(tmp_path: Path) -> None:
    session, run, scan, research, research_id = _research_state(tmp_path)
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    response = DailyDecision(
        status="NO_ACTION",
        market_assessment="No sufficiently asymmetric setup.",
        strongest_counterargument="A short-term move remains possible.",
        no_action_reason="Evidence is insufficient.",
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
