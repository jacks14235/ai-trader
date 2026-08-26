"""The single audited boundary through which any reasoning role reaches a model.

Every role invocation is placed in its run's workflow before the provider is called, so a
multi-step or retried workflow can be reconstructed from ``agent_invocations`` alone rather than
inferred from a free-text purpose. Nothing here knows which role it is running: role identity,
context shape, and output contract are all supplied by the caller.
"""

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.codex_cli import (
    CodexCLIInvocationError,
    StructuredReasoningProvider,
    codex_output_schema,
)
from trader.agent.config import (
    AgentConfig,
    AgentRoleConfig,
    ContextSource,
    ModelProfile,
    RoleName,
)
from trader.persistence.models import AgentInvocation
from trader.persistence.repositories import associate_agent_invocation_evidence

STEP_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class WorkflowStep:
    """Where one model call sits in a run, and which artifacts therefore belong to it.

    An unnamed step is the role's only call in the run. A workflow that invokes one role more
    than once must name each step, because ``(run, role, step, attempt)`` is unique.
    """

    role: RoleName
    step: str = ""
    attempt: int = 1
    parent_invocation_id: str | None = None

    def __post_init__(self) -> None:
        if self.step and not STEP_PATTERN.fullmatch(self.step):
            raise ValueError(f"workflow step must be a lowercase identifier: {self.step}")
        if not 1 <= self.attempt <= MAX_ATTEMPTS:
            raise ValueError(f"attempt must be between 1 and {MAX_ATTEMPTS}: {self.attempt}")

    @property
    def artifact_directory(self) -> str:
        """The run-relative artifact path no other invocation in the run can claim."""
        name = f"{self.role}/{self.step}" if self.step else self.role
        if self.attempt > 1:
            name = f"{name}__attempt-{self.attempt}"
        return f"agent/{name}"

    def describe(self) -> dict[str, object]:
        return {
            "role": self.role,
            "step": self.step,
            "attempt": self.attempt,
            "parent_invocation_id": self.parent_invocation_id,
        }


@dataclass(frozen=True)
class RoleInvocationResult[OutputModel: BaseModel]:
    """A completed, audited role invocation and the validated object it produced."""

    invocation_id: str
    output: OutputModel
    context_hash: str
    prompt_hash: str
    evidence_manifest_hash: str


def verify_context_sources(
    role: AgentRoleConfig,
    *,
    supported: Mapping[ContextSource, tuple[str, ...]],
    context_model: type[BaseModel],
    label: str,
) -> None:
    """Fail closed when a role declares a context source its assembler cannot supply.

    A declared source absent from ``supported`` is a configuration error rather than an empty
    section, which is what previously allowed a role to require memory it never received.
    """
    unsupported = sorted(source for source in role.context_sources if source not in supported)
    if unsupported:
        raise ValueError(f"{label} cannot supply declared sources: " + ", ".join(unsupported))
    missing = sorted(
        field
        for source in role.context_sources
        for field in supported[source]
        if field not in context_model.model_fields
    )
    if missing:
        raise ValueError(f"{label} is missing fields for declared sources: " + ", ".join(missing))


def resolve_role(config: AgentConfig, role: RoleName) -> tuple[AgentRoleConfig, ModelProfile]:
    """Resolve an enabled role and its profile, refusing anything outside paper proposals."""
    if config.mode != "paper_proposal":
        raise ValueError("reasoning roles may only run in paper_proposal mode")
    role_config = config.roles[role]
    if not role_config.enabled:
        raise ValueError(f"{role} role is disabled")
    if role_config.permissions.can_submit_orders:
        raise ValueError(f"{role} must not claim order permission")
    return role_config, config.model_profiles[role_config.profile]


def invoke_role[OutputModel: BaseModel](
    session: Session,
    config: AgentConfig,
    *,
    run_id: str,
    run_directory: Path,
    workflow: WorkflowStep,
    prompt: str,
    context: BaseModel,
    output_model: type[OutputModel],
    admitted_evidence_ids: Sequence[str],
    provider: StructuredReasoningProvider,
    validate: Callable[[OutputModel], OutputModel] | None = None,
    on_output: Callable[[OutputModel, str], None] | None = None,
) -> RoleInvocationResult[OutputModel]:
    """Invoke one role against a bounded context and persist the whole exchange.

    ``validate`` runs against the parsed output before anything is committed, and ``on_output``
    records whatever the caller derives from an accepted output inside the same failure boundary.
    Either raising leaves the invocation ``FAILED`` and writes no derived rows.
    """
    role_config, profile = resolve_role(config, workflow.role)
    _verify_parent(session, run_id=run_id, workflow=workflow)

    prompt_text = prompt.strip()
    context_json = canonical_json(context)
    if len(context_json) > role_config.max_context_chars:
        raise ValueError(
            f"{workflow.role} context has {len(context_json)} chars; "
            f"role limit is {role_config.max_context_chars}"
        )
    context_hash = hashlib.sha256(context_json.encode()).hexdigest()
    prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()
    schema = codex_output_schema(output_model.model_json_schema())

    artifact_directory = workflow.artifact_directory
    directory = run_directory / artifact_directory
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(
            f"{workflow.role} already claimed {artifact_directory} in this run"
        ) from exc
    (directory / "prompt.md").write_text(prompt_text + "\n", encoding="utf-8")
    (directory / "context.json").write_text(context_json + "\n", encoding="utf-8")
    _write_json(directory / "output_schema.json", schema)
    _write_json(
        directory / "request.json",
        {
            **workflow.describe(),
            "mode": config.mode,
            "provider": profile.provider,
            "model": profile.model,
            "reasoning_effort": profile.reasoning_effort,
            "prompt_hash": prompt_hash,
            "context_hash": context_hash,
            "context_sources": list(role_config.context_sources),
            "output_model": output_model.__name__,
            "limits": {
                "max_context_chars": role_config.max_context_chars,
                "max_output_chars": role_config.max_output_chars,
                "timeout_seconds": role_config.timeout_seconds,
            },
            "permissions": role_config.permissions.model_dump(mode="json"),
            "admitted_evidence_ids": list(admitted_evidence_ids),
        },
    )

    invocation = AgentInvocation(
        run_id=run_id,
        role=workflow.role,
        step=workflow.step,
        attempt=workflow.attempt,
        parent_invocation_id=workflow.parent_invocation_id,
        purpose=f"{workflow.role}_{config.mode}",
        model=profile.model or "codex-cli-default",
        provider=provider.provider_name,
        prompt_version=prompt_hash,
        request_path=f"{artifact_directory}/request.json",
        status="STARTED",
    )
    session.add(invocation)
    session.commit()
    invocation_id = invocation.id
    associate_agent_invocation_evidence(
        session,
        agent_invocation_id=invocation_id,
        research_ids=admitted_evidence_ids,
    )
    try:
        response = provider.invoke(
            prompt=model_input(prompt_text, context_json),
            output_schema=schema,
            profile=profile,
            timeout_seconds=role_config.timeout_seconds,
            max_output_chars=role_config.max_output_chars,
        )
        (directory / "provider_stdout.log").write_text(response.stdout, encoding="utf-8")
        (directory / "provider_stderr.log").write_text(response.stderr, encoding="utf-8")
        try:
            output = output_model.model_validate_json(response.response_text)
        except ValidationError as exc:
            raise RuntimeError(
                f"{workflow.role} returned invalid structured output: {exc}"
            ) from exc
        if validate is not None:
            output = validate(output)
        (directory / "response.json").write_text(
            output.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        if on_output is not None:
            on_output(output, invocation_id)
        completed = session.get(AgentInvocation, invocation_id)
        assert completed is not None
        completed.response_path = f"{artifact_directory}/response.json"
        completed.input_token_count = response.input_token_count
        completed.output_token_count = response.output_token_count
        completed.completed_at = datetime.now(UTC)
        completed.status = "COMPLETED"
        session.commit()
        if completed.evidence_manifest_hash is None:
            raise RuntimeError("invocation evidence manifest was not persisted")
        return RoleInvocationResult(
            invocation_id=invocation_id,
            output=output,
            context_hash=context_hash,
            prompt_hash=prompt_hash,
            evidence_manifest_hash=completed.evidence_manifest_hash,
        )
    except Exception as exc:
        session.rollback()
        if isinstance(exc, CodexCLIInvocationError):
            (directory / "provider_stdout.log").write_text(exc.stdout, encoding="utf-8")
            (directory / "provider_stderr.log").write_text(exc.stderr, encoding="utf-8")
        _write_json(
            directory / "failure.json",
            {"error_type": type(exc).__name__, "message": str(exc)},
        )
        failed = session.get(AgentInvocation, invocation_id)
        if failed is not None:
            failed.completed_at = datetime.now(UTC)
            failed.status = "FAILED"
            failed.error_summary = str(exc)[:4_000]
            session.commit()
        raise


def workflow_trail(session: Session, run_id: str) -> tuple[AgentInvocation, ...]:
    """Return one run's invocations in workflow order, for audit and operator review."""
    return tuple(
        session.scalars(
            select(AgentInvocation)
            .where(AgentInvocation.run_id == run_id)
            .order_by(
                AgentInvocation.started_at,
                AgentInvocation.role,
                AgentInvocation.step,
                AgentInvocation.attempt,
            )
        )
    )


def canonical_json(model: BaseModel) -> str:
    """Serialize a context deterministically so its hash identifies the exact bytes sent."""
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def model_input(prompt: str, context_json: str) -> str:
    return (
        prompt
        + "\n\nThe following JSON is the complete and only admitted context:\n"
        + context_json
        + "\n"
    )


def _verify_parent(session: Session, *, run_id: str, workflow: WorkflowStep) -> None:
    if workflow.parent_invocation_id is None:
        return
    parent = session.get(AgentInvocation, workflow.parent_invocation_id)
    if parent is None:
        raise LookupError(f"parent invocation not found: {workflow.parent_invocation_id}")
    if parent.run_id != run_id:
        raise ValueError("a child invocation must belong to its parent's run")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
