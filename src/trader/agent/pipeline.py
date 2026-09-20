"""Execute an audited process profile; no market-data or execution adapter enters this module.

The terminal callback persists proposals, never orders. Context projection is supplied by the
caller, allowing a later reviewed adapter to use the same decision contracts without lending
researchers account or execution access.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from sqlalchemy.orm import Session

from trader.agent.catalog import PipelineCatalog
from trader.agent.codex_cli import InvocationResponse, StructuredReasoningProvider
from trader.agent.config import AgentConfig, AgentRoleConfig, ModelProfile
from trader.agent.invocation import (
    RoleInvocationResult,
    WorkflowStep,
    canonical_json,
    invoke_role,
    resolve_role,
    verify_context_sources,
)
from trader.agent.packets import NamedPacket, ResearchPacket, validate_research_packet
from trader.agent.profile_context import (
    BOOK_CONTEXT_SOURCES,
    RESEARCH_CONTEXT_SOURCES,
    BookAgentContext,
    ResearchAgentContext,
)
from trader.agent.prompts import compose_prompt
from trader.agent.reasoning import DailyDecision, validate_daily_decision


@dataclass(frozen=True)
class ProfileRunResult:
    terminal: RoleInvocationResult[DailyDecision]
    trail: tuple[str, ...]


def run_profile(
    session: Session,
    config: AgentConfig,
    catalog: PipelineCatalog,
    profile_name: str,
    *,
    run_id: str,
    run_directory: Path,
    artifact_directory: Path,
    provider: StructuredReasoningProvider,
    step_prefix: str,
    skeleton_prompts: Mapping[str, str],
    operating_note: str,
    assemble_research_context: Callable[
        [AgentRoleConfig, tuple[NamedPacket, ...]], ResearchAgentContext
    ],
    assemble_daily_context: Callable[[AgentRoleConfig, tuple[NamedPacket, ...]], BookAgentContext],
    on_decision: Callable[[DailyDecision, str], None],
    book_id: str,
    book_evaluation_id: str,
) -> ProfileRunResult:
    """Run each declared step once, preserving complete inputs and merge provenance."""
    catalog = PipelineCatalog.model_validate(catalog)
    if profile_name not in catalog.profiles:
        raise ValueError(f"unknown process profile: {profile_name}")
    profile = catalog.profiles[profile_name]
    # Preflight every role and path before making the first paid call.
    for step in profile.steps:
        role, _ = resolve_role(config, step.role)
        research_step = step.output == "research_packet"
        verify_context_sources(
            role,
            supported=RESEARCH_CONTEXT_SOURCES if research_step else BOOK_CONTEXT_SOURCES,
            context_model=ResearchAgentContext if research_step else BookAgentContext,
            label="profile step",
        )
        if step.consumes and "research_packets" not in role.context_sources:
            raise ValueError("consumed packets require the research_packets context source")
        workflow = WorkflowStep(role=step.role, step=step_prefix + step.step)
        if (run_directory / workflow.artifact_directory).exists():
            raise ValueError(
                f"profile invocation path already exists: {workflow.artifact_directory}"
            )
        if step.step not in skeleton_prompts:
            raise ValueError(f"profile prompt is missing for step {step.step}")
    if not artifact_directory.resolve().is_relative_to(run_directory.resolve()):
        raise ValueError("profile artifacts must stay inside the run directory")
    artifact_directory.mkdir(parents=True, exist_ok=False)
    provider = _BoundedProfileProvider(provider)
    _write_json(
        artifact_directory / "plan.json",
        {
            "schema_version": 1,
            "profile_name": profile_name,
            "profile": profile.model_dump(mode="json"),
            "step_prefix": step_prefix,
            "run_id": run_id,
        },
    )
    packets: dict[str, NamedPacket] = {}
    trail: list[str] = []
    steps: list[dict[str, object]] = []
    try:
        for step in profile.steps:
            role, _model = resolve_role(config, step.role)
            consumed = tuple(packets[name] for name in step.consumes)
            parent_id = consumed[-1].invocation_id if consumed else None
            workflow = WorkflowStep(
                role=step.role,
                step=step_prefix + step.step,
                parent_invocation_id=parent_id,
            )
            prompt = compose_prompt(skeleton_prompts[step.step], operating_note)
            step_record: dict[str, object] = {
                "step": step.step,
                "workflow_step": workflow.step,
                "role": step.role,
                "consumed": [
                    {
                        "step": item.step,
                        "invocation_id": item.invocation_id,
                        "content_hash": item.content_hash,
                    }
                    for item in consumed
                ],
            }
            steps.append(step_record)
            if step.output == "research_packet":
                research_context = assemble_research_context(role, consumed)
                _verify_projection(run_id, consumed, research_context)
                allowed_symbols = frozenset(item.symbol for item in research_context.candidates)
                packet_result = invoke_role(
                    session,
                    config,
                    run_id=run_id,
                    run_directory=run_directory,
                    workflow=workflow,
                    prompt=prompt,
                    context=research_context,
                    output_model=ResearchPacket,
                    admitted_evidence_ids=research_context.admitted_evidence_ids,
                    provider=provider,
                    validate=partial(
                        validate_research_packet,
                        admitted_evidence_ids=research_context.admitted_evidence_ids,
                        allowed_symbols=allowed_symbols,
                    ),
                    book_id=book_id,
                    book_evaluation_id=book_evaluation_id,
                )
                packets[step.step] = NamedPacket(
                    step=step.step,
                    invocation_id=packet_result.invocation_id,
                    content_hash=hashlib.sha256(
                        canonical_json(packet_result.output).encode()
                    ).hexdigest(),
                    packet=packet_result.output,
                )
                trail.append(packet_result.invocation_id)
                step_record["invocation_id"] = packet_result.invocation_id
                step_record["packet_hash"] = packets[step.step].content_hash
            else:
                daily_context = assemble_daily_context(role, consumed)
                _verify_projection(run_id, consumed, daily_context)
                terminal = invoke_role(
                    session,
                    config,
                    run_id=run_id,
                    run_directory=run_directory,
                    workflow=workflow,
                    prompt=prompt,
                    context=daily_context,
                    output_model=DailyDecision,
                    admitted_evidence_ids=daily_context.admitted_evidence_ids,
                    provider=provider,
                    validate=partial(validate_daily_decision, context=daily_context),
                    on_output=on_decision,
                    book_id=book_id,
                    book_evaluation_id=book_evaluation_id,
                )
                trail.append(terminal.invocation_id)
                step_record["invocation_id"] = terminal.invocation_id
                _write_json(
                    artifact_directory / "result.json",
                    {
                        "status": "COMPLETED",
                        "steps": steps,
                        "trail": trail,
                        "terminal_invocation_id": terminal.invocation_id,
                    },
                )
                return ProfileRunResult(terminal=terminal, trail=tuple(trail))
    except Exception as error:
        _write_json(
            artifact_directory / "failure.json",
            {
                "status": "FAILED",
                "error": str(error),
                "steps": steps,
                "trail": trail,
            },
        )
        raise
    raise ValueError("profile has no terminal decision")


def _verify_projection(
    run_id: str,
    consumed: tuple[NamedPacket, ...],
    context: ResearchAgentContext | BookAgentContext,
) -> None:
    if context.run_id != run_id:
        raise ValueError("profile context belongs to another run")
    if context.research_packets != consumed:
        raise ValueError("profile context must include exactly the declared consumed packets")


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


class _BoundedProfileProvider:
    """Enforce the role's response bound even for an alternate injected provider adapter."""

    def __init__(self, delegate: StructuredReasoningProvider) -> None:
        self.delegate = delegate
        self.provider_name = delegate.provider_name

    def invoke(
        self,
        *,
        prompt: str,
        output_schema: dict[str, object],
        profile: ModelProfile,
        timeout_seconds: int,
        max_output_chars: int,
    ) -> InvocationResponse:
        response = self.delegate.invoke(
            prompt=prompt,
            output_schema=output_schema,
            profile=profile,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        if len(response.response_text) > max_output_chars:
            raise ValueError("profile provider response exceeds role output limit")
        return response
