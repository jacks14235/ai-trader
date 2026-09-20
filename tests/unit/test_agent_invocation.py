import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from trader.agent.codex_cli import (
    CodexCLIInvocationError,
    InvocationResponse,
    _response_usage,
)
from trader.agent.config import AgentConfig, ModelProfile, load_agent_config
from trader.agent.invocation import (
    WorkflowStep,
    canonical_json,
    invoke_role,
    resolve_role,
    workflow_trail,
)
from trader.cli import app
from trader.persistence.db import create_session_factory
from trader.persistence.models import AgentInvocation, Run

PROJECT_ROOT = Path(__file__).parents[2]


class Packet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding: str


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    note: str


class RecordingProvider:
    provider_name = "test"

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, **kwargs: object) -> InvocationResponse:
        self.prompts.append(str(kwargs["prompt"]))
        return InvocationResponse(self.responses.pop(0), "stdout", "")


class FailingProvider:
    provider_name = "test"

    def invoke(self, **kwargs: object) -> InvocationResponse:
        raise RuntimeError("provider is unavailable")


class UsageProvider:
    provider_name = "test"

    def __init__(self, response: str, *, fail: bool = False) -> None:
        self.response = response
        self.fail = fail

    def invoke(self, **kwargs: object) -> InvocationResponse:
        usage = InvocationResponse(
            self.response,
            "stdout",
            "",
            input_token_count=1_000,
            output_token_count=250,
            cached_input_token_count=400,
            reasoning_output_token_count=100,
        )
        if self.fail:
            raise CodexCLIInvocationError(
                "provider failed after usage",
                stdout="stdout",
                stderr="",
                usage=usage,
            )
        return usage


def test_codex_usage_parser_retains_cached_reasoning_and_optional_cost() -> None:
    stdout = "\n".join(
        [
            json.dumps({"usage": {"input_tokens": 10, "output_tokens": 2}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1_000,
                        "cached_input_tokens": 400,
                        "output_tokens": 250,
                        "reasoning_output_tokens": 100,
                        "cost_usd": "0.0125",
                    },
                }
            ),
        ]
    )

    usage = _response_usage(stdout)

    assert usage.usage_payload() == {
        "input_tokens": 1_000,
        "cached_input_tokens": 400,
        "output_tokens": 250,
        "reasoning_output_tokens": 100,
        "total_tokens": 1_250,
        "cost_usd": "0.0125",
        "cost_source": "CLI_REPORTED",
    }


def test_workflow_step_gives_every_invocation_its_own_artifact_directory() -> None:
    assert WorkflowStep(role="daily_trader").artifact_directory == "agent/daily_trader"
    assert (
        WorkflowStep(role="research_compactor", step="packet").artifact_directory
        == "agent/research_compactor/packet"
    )
    assert (
        WorkflowStep(role="research_compactor", step="packet", attempt=2).artifact_directory
        == "agent/research_compactor/packet__attempt-2"
    )

    with pytest.raises(ValueError, match="lowercase identifier"):
        WorkflowStep(role="daily_trader", step="Follow Up")
    with pytest.raises(ValueError, match="attempt must be between"):
        WorkflowStep(role="daily_trader", attempt=0)


def test_a_multi_step_workflow_is_reconstructable_from_the_audit_trail(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    provider = RecordingProvider(
        Packet(finding="first").model_dump_json(),
        Packet(finding="second").model_dump_json(),
        Packet(finding="third").model_dump_json(),
    )
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    parent = _invoke(
        session, config, run, run_directory, provider, WorkflowStep(role="daily_trader")
    )
    _invoke(
        session,
        config,
        run,
        run_directory,
        provider,
        WorkflowStep(
            role="research_compactor",
            step="packet",
            parent_invocation_id=parent.invocation_id,
        ),
    )
    _invoke(
        session,
        config,
        run,
        run_directory,
        provider,
        WorkflowStep(
            role="research_compactor",
            step="follow_up",
            parent_invocation_id=parent.invocation_id,
        ),
    )

    trail = workflow_trail(session, run.id)
    assert [(item.role, item.step, item.attempt) for item in trail] == [
        ("daily_trader", "", 1),
        ("research_compactor", "packet", 1),
        ("research_compactor", "follow_up", 1),
    ]
    assert [item.parent_invocation_id for item in trail] == [
        None,
        parent.invocation_id,
        parent.invocation_id,
    ]
    assert {item.status for item in trail} == {"COMPLETED"}
    assert [item.request_path for item in trail] == [
        "agent/daily_trader/request.json",
        "agent/research_compactor/packet/request.json",
        "agent/research_compactor/follow_up/request.json",
    ]
    for item in trail:
        assert (run_directory / item.request_path).is_file()
        assert item.response_path is not None
        assert (run_directory / item.response_path).is_file()

    request = json.loads(
        (run_directory / "agent" / "research_compactor" / "packet" / "request.json").read_text()
    )
    assert request["role"] == "research_compactor"
    assert request["step"] == "packet"
    assert request["attempt"] == 1
    assert request["parent_invocation_id"] == parent.invocation_id
    assert request["output_model"] == "Packet"
    assert request["permissions"]["can_submit_orders"] is False


def test_one_role_cannot_claim_the_same_step_twice_in_a_run(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    provider = RecordingProvider(
        Packet(finding="first").model_dump_json(),
        Packet(finding="second").model_dump_json(),
    )
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    step = WorkflowStep(role="research_compactor", step="packet")

    _invoke(session, config, run, run_directory, provider, step)
    with pytest.raises(ValueError, match="already claimed"):
        _invoke(session, config, run, run_directory, provider, step)

    session.add(
        AgentInvocation(
            run_id=run.id,
            role="research_compactor",
            step="packet",
            attempt=1,
            purpose="research_compactor_paper_proposal",
            model="m",
            provider="test",
            prompt_version="hash",
            request_path="agent/elsewhere/request.json",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_retry_is_recorded_as_a_separate_attempt(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    step = WorkflowStep(role="research_compactor", step="packet")

    with pytest.raises(RuntimeError, match="provider is unavailable"):
        _invoke(session, config, run, run_directory, FailingProvider(), step)

    retry = _invoke(
        session,
        config,
        run,
        run_directory,
        RecordingProvider(Packet(finding="recovered").model_dump_json()),
        WorkflowStep(role="research_compactor", step="packet", attempt=2),
    )

    assert retry.output.finding == "recovered"
    trail = workflow_trail(session, run.id)
    assert [(item.attempt, item.status) for item in trail] == [(1, "FAILED"), (2, "COMPLETED")]
    assert trail[0].error_summary == "provider is unavailable"
    failure = json.loads(
        (run_directory / "agent" / "research_compactor" / "packet" / "failure.json").read_text()
    )
    assert failure["error_type"] == "RuntimeError"
    assert (
        run_directory / "agent" / "research_compactor" / "packet__attempt-2" / "response.json"
    ).is_file()


def test_a_rejected_output_leaves_no_derived_rows(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    derived: list[str] = []

    def refuse(packet: Packet) -> Packet:
        raise ValueError("finding cites an unadmitted source")

    with pytest.raises(ValueError, match="unadmitted source"):
        invoke_role(
            session,
            config,
            run_id=run.id,
            run_directory=run_directory,
            workflow=WorkflowStep(role="research_compactor"),
            prompt="Return structured output only.",
            context=_context(),
            output_model=Packet,
            admitted_evidence_ids=(),
            provider=UsageProvider(Packet(finding="invented").model_dump_json()),
            validate=refuse,
            on_output=lambda packet, invocation_id: derived.append(invocation_id),
        )

    assert derived == []
    trail = workflow_trail(session, run.id)
    assert [item.status for item in trail] == ["FAILED"]
    assert trail[0].response_path is None
    assert trail[0].input_token_count == 1_000
    assert trail[0].cached_input_token_count == 400
    assert trail[0].output_token_count == 250
    assert trail[0].reasoning_output_token_count == 100
    assert trail[0].total_token_count == 1_250
    assert json.loads(trail[0].usage_json) == {
        "input_tokens": 1_000,
        "cached_input_tokens": 400,
        "output_tokens": 250,
        "reasoning_output_tokens": 100,
        "total_tokens": 1_250,
        "cost_usd": None,
        "cost_source": "NOT_REPORTED",
    }
    assert not (run_directory / "agent" / "research_compactor" / "response.json").exists()


def test_failed_cli_invocation_retains_reported_usage(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    with pytest.raises(CodexCLIInvocationError, match="failed after usage"):
        _invoke(
            session,
            config,
            run,
            run_directory,
            UsageProvider(Packet(finding="unused").model_dump_json(), fail=True),
            WorkflowStep(role="daily_trader"),
        )

    invocation = workflow_trail(session, run.id)[0]
    assert invocation.status == "FAILED"
    assert invocation.total_token_count == 1_250
    assert invocation.cost_source == "NOT_REPORTED"


def test_agent_usage_command_aggregates_models_and_live_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, run, config = _state(tmp_path)
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    _invoke(
        session,
        config,
        run,
        run_directory,
        UsageProvider(Packet(finding="counted").model_dump_json()),
        WorkflowStep(role="daily_trader"),
    )
    monkeypatch.setattr("trader.cli.database_service", lambda: (object(), session))

    result = CliRunner().invoke(app, ["agents", "usage"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["totals"]["invocation_count"] == 1
    assert payload["totals"]["total_tokens"] == 1_250
    assert payload["by_model"]["gpt-5.6-sol"]["cached_input_tokens"] == 400
    assert payload["by_book"]["live"]["reasoning_output_tokens"] == 100
    assert payload["totals"]["reported_cost_usd"] is None


def test_disabled_roles_and_foreign_parents_are_refused(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    role = config.roles["research_compactor"]
    disabled = config.model_copy(
        update={
            "roles": {
                **config.roles,
                "research_compactor": role.model_copy(update={"enabled": False}),
            }
        }
    )
    with pytest.raises(ValueError, match="research_compactor role is disabled"):
        resolve_role(disabled, "research_compactor")

    other = Run(run_key="daily:other", scheduled_for=datetime.now(UTC), config_hash="config")
    session.add(other)
    session.commit()
    foreign = _invoke(
        session,
        config,
        other,
        run_directory / "other",
        RecordingProvider(Packet(finding="elsewhere").model_dump_json()),
        WorkflowStep(role="daily_trader"),
    )

    with pytest.raises(ValueError, match="must belong to its parent's run"):
        _invoke(
            session,
            config,
            run,
            run_directory,
            RecordingProvider(Packet(finding="child").model_dump_json()),
            WorkflowStep(
                role="research_compactor",
                step="packet",
                parent_invocation_id=foreign.invocation_id,
            ),
        )
    with pytest.raises(LookupError, match="parent invocation not found"):
        _invoke(
            session,
            config,
            run,
            run_directory,
            RecordingProvider(Packet(finding="orphan").model_dump_json()),
            WorkflowStep(
                role="research_compactor",
                step="orphan",
                parent_invocation_id="missing",
            ),
        )


def test_a_context_exceeding_the_role_limit_never_reaches_the_provider(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    role = config.roles["research_compactor"]
    bounded = config.model_copy(
        update={
            "roles": {
                **config.roles,
                "research_compactor": role.model_copy(update={"max_context_chars": 10_000}),
            }
        }
    )
    provider = RecordingProvider(Packet(finding="unused").model_dump_json())
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    with pytest.raises(ValueError, match="role limit is 10000"):
        invoke_role(
            session,
            bounded,
            run_id=run.id,
            run_directory=run_directory,
            workflow=WorkflowStep(role="research_compactor"),
            prompt="Return structured output only.",
            context=Context(as_of=datetime.now(UTC), note="x" * 20_000),
            output_model=Packet,
            admitted_evidence_ids=(),
            provider=provider,
        )

    assert provider.prompts == []
    assert session.query(AgentInvocation).count() == 0
    assert not (run_directory / "agent").exists()


def test_the_context_hash_identifies_the_exact_bytes_sent(tmp_path: Path) -> None:
    session, run, config = _state(tmp_path)
    context = _context()
    provider = RecordingProvider(Packet(finding="stable").model_dump_json())
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    result = _invoke(
        session, config, run, run_directory, provider, WorkflowStep(role="daily_trader"), context
    )

    context_json = canonical_json(context)
    assert context_json in provider.prompts[0]
    written = (run_directory / "agent" / "daily_trader" / "context.json").read_text()
    assert written == context_json + "\n"
    request = json.loads((run_directory / "agent" / "daily_trader" / "request.json").read_text())
    assert request["context_hash"] == result.context_hash


def _state(tmp_path: Path) -> tuple[object, Run, AgentConfig]:
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:2026-08-22", scheduled_for=datetime.now(UTC), config_hash="config")
    session.add(run)
    session.commit()
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    return session, run, config


def _context() -> Context:
    return Context(as_of=datetime(2026, 8, 22, 19, 15, tzinfo=UTC), note="bounded")


def _invoke(session, config, run, run_directory, provider, workflow, context=None):  # type: ignore[no-untyped-def]
    return invoke_role(
        session,
        config,
        run_id=run.id,
        run_directory=run_directory,
        workflow=workflow,
        prompt="Return structured output only.",
        context=context or _context(),
        output_model=Packet,
        admitted_evidence_ids=(),
        provider=provider,
    )


def test_model_profiles_stay_reachable_for_every_enabled_role() -> None:
    config = load_agent_config(PROJECT_ROOT / "config" / "agents.yaml")
    for name, role in config.roles.items():
        if not role.enabled:
            continue
        _, profile = resolve_role(config, name)
        assert isinstance(profile, ModelProfile)
        assert profile.provider == "codex_cli"
