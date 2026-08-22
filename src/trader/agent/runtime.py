"""Audited broker-disconnected reasoning that emits paper-trade proposals."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.orm import Session

from trader.agent.codex_cli import (
    CodexCLIInvocationError,
    CodexCLIProvider,
    StructuredReasoningProvider,
    codex_output_schema,
)
from trader.agent.config import AgentConfig, load_agent_config, resolved_prompt_path
from trader.agent.reasoning import (
    DailyDecision,
    assemble_daily_context,
    canonical_context_json,
    validate_daily_decision,
)
from trader.broker.models import Account, BrokerOrder, Position
from trader.persistence.models import AgentInvocation
from trader.persistence.repositories import (
    associate_agent_invocation_evidence,
    persist_trade_proposal,
)
from trader.research.service import ResearchRunResult
from trader.settings import Settings
from trader.universe.models import UniverseScan


@dataclass(frozen=True)
class DailyReasoningResult:
    invocation_id: str
    decision: DailyDecision
    context_hash: str
    prompt_hash: str
    evidence_manifest_hash: str

    def summary(self) -> dict[str, object]:
        return {
            "mode": "paper_proposal",
            "invocation_id": self.invocation_id,
            "status": self.decision.status,
            "proposal_count": len(self.decision.proposals),
            "context_hash": self.context_hash,
            "prompt_hash": self.prompt_hash,
            "evidence_manifest_hash": self.evidence_manifest_hash,
        }


class DailyReasoningPipeline(Protocol):
    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        as_of: datetime,
        account: Account,
        positions: tuple[Position, ...],
        open_orders: tuple[BrokerOrder, ...],
        scan: UniverseScan,
        research: ResearchRunResult,
    ) -> DailyReasoningResult: ...


class ShadowDailyReasoningPipeline:
    """Assemble evidence, invoke a constrained model, and persist proposals without executing."""

    def __init__(
        self,
        session: Session,
        config: AgentConfig,
        *,
        prompt: str,
        strategy: str,
        portfolio_policy: str,
        provider: StructuredReasoningProvider,
    ) -> None:
        if config.mode != "paper_proposal" or not config.automatic_daily_run:
            raise ValueError("automatic daily reasoning must use paper_proposal mode")
        role = config.roles["daily_trader"]
        if not role.enabled:
            raise ValueError("daily_trader role is disabled")
        self.session = session
        self.config = config
        self.role = role
        self.profile = config.model_profiles[role.profile]
        self.prompt = prompt.strip()
        self.strategy = strategy.strip()
        self.portfolio_policy = portfolio_policy.strip()
        self.provider = provider

    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        as_of: datetime,
        account: Account,
        positions: tuple[Position, ...],
        open_orders: tuple[BrokerOrder, ...],
        scan: UniverseScan,
        research: ResearchRunResult,
    ) -> DailyReasoningResult:
        context = assemble_daily_context(
            self.session,
            run_id=run_id,
            as_of=as_of,
            strategy=self.strategy,
            portfolio_policy=self.portfolio_policy,
            account=account,
            positions=positions,
            open_orders=open_orders,
            scan=scan,
            research=research,
            role=self.role,
        )
        context_json = canonical_context_json(context)
        context_hash = hashlib.sha256(context_json.encode()).hexdigest()
        prompt_hash = hashlib.sha256(self.prompt.encode()).hexdigest()
        schema = codex_output_schema(DailyDecision.model_json_schema())
        agent_directory = run_directory / "agent" / "daily_trader"
        agent_directory.mkdir(parents=True, exist_ok=False)
        (agent_directory / "prompt.md").write_text(self.prompt + "\n", encoding="utf-8")
        (agent_directory / "context.json").write_text(context_json + "\n", encoding="utf-8")
        _write_json(agent_directory / "output_schema.json", schema)
        request = {
            "role": "daily_trader",
            "mode": "paper_proposal",
            "provider": self.profile.provider,
            "model": self.profile.model,
            "reasoning_effort": self.profile.reasoning_effort,
            "prompt_hash": prompt_hash,
            "context_hash": context_hash,
            "context_sources": list(self.role.context_sources),
            "limits": {
                "max_context_chars": self.role.max_context_chars,
                "max_output_chars": self.role.max_output_chars,
                "timeout_seconds": self.role.timeout_seconds,
            },
            "permissions": self.role.permissions.model_dump(mode="json"),
            "admitted_evidence_ids": list(context.admitted_evidence_ids),
        }
        _write_json(agent_directory / "request.json", request)
        invocation = AgentInvocation(
            run_id=run_id,
            purpose="daily_trader_paper_proposal",
            model=self.profile.model or "codex-cli-default",
            provider=self.provider.provider_name,
            prompt_version=prompt_hash,
            request_path="agent/daily_trader/request.json",
            status="STARTED",
        )
        self.session.add(invocation)
        self.session.commit()
        invocation_id = invocation.id
        associate_agent_invocation_evidence(
            self.session,
            agent_invocation_id=invocation_id,
            research_ids=context.admitted_evidence_ids,
        )
        try:
            response = self.provider.invoke(
                prompt=_model_input(self.prompt, context_json),
                output_schema=schema,
                profile=self.profile,
                timeout_seconds=self.role.timeout_seconds,
                max_output_chars=self.role.max_output_chars,
            )
            (agent_directory / "provider_stdout.log").write_text(
                response.stdout, encoding="utf-8"
            )
            (agent_directory / "provider_stderr.log").write_text(
                response.stderr, encoding="utf-8"
            )
            try:
                decision = DailyDecision.model_validate_json(response.response_text)
            except ValidationError as exc:
                raise RuntimeError(
                    f"daily agent returned invalid structured output: {exc}"
                ) from exc
            decision = validate_daily_decision(decision, context)
            response_path = agent_directory / "response.json"
            response_path.write_text(decision.model_dump_json(indent=2) + "\n", encoding="utf-8")
            for proposal in decision.proposals:
                persist_trade_proposal(
                    self.session,
                    run_id,
                    proposal,
                    agent_invocation_id=invocation_id,
                )
            completed_invocation = self.session.get(AgentInvocation, invocation_id)
            assert completed_invocation is not None
            completed_invocation.response_path = "agent/daily_trader/response.json"
            completed_invocation.input_token_count = response.input_token_count
            completed_invocation.output_token_count = response.output_token_count
            completed_invocation.completed_at = datetime.now(UTC)
            completed_invocation.status = "COMPLETED"
            self.session.commit()
            if completed_invocation.evidence_manifest_hash is None:
                raise RuntimeError("invocation evidence manifest was not persisted")
            return DailyReasoningResult(
                invocation_id=invocation_id,
                decision=decision,
                context_hash=context_hash,
                prompt_hash=prompt_hash,
                evidence_manifest_hash=completed_invocation.evidence_manifest_hash,
            )
        except Exception as exc:
            self.session.rollback()
            if isinstance(exc, CodexCLIInvocationError):
                (agent_directory / "provider_stdout.log").write_text(
                    exc.stdout, encoding="utf-8"
                )
                (agent_directory / "provider_stderr.log").write_text(
                    exc.stderr, encoding="utf-8"
                )
            _write_json(
                agent_directory / "failure.json",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            failed = self.session.get(AgentInvocation, invocation_id)
            if failed is not None:
                failed.completed_at = datetime.now(UTC)
                failed.status = "FAILED"
                failed.error_summary = str(exc)[:4_000]
                self.session.commit()
            raise


def configured_daily_reasoning_pipeline(
    settings: Settings,
    session: Session,
    *,
    provider: StructuredReasoningProvider | None = None,
) -> ShadowDailyReasoningPipeline | None:
    """Build automatic reasoning only when both config and environment explicitly enable it."""
    config = load_agent_config(settings.trader_agents_config)
    if not settings.trader_reasoning_enabled or not config.automatic_daily_run:
        return None
    prompt_path = resolved_prompt_path(
        settings.trader_agents_config, config.roles["daily_trader"]
    )
    return ShadowDailyReasoningPipeline(
        session,
        config,
        prompt=prompt_path.read_text(encoding="utf-8"),
        strategy=settings.trader_strategy_document.read_text(encoding="utf-8"),
        portfolio_policy=settings.trader_portfolio_policy.read_text(encoding="utf-8"),
        provider=provider or CodexCLIProvider(),
    )


def _model_input(prompt: str, context_json: str) -> str:
    return (
        prompt
        + "\n\nThe following JSON is the complete and only admitted context:\n"
        + context_json
        + "\n"
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
