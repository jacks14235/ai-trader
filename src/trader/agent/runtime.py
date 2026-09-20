"""Audited broker-disconnected reasoning that emits paper-trade proposals."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from trader.agent.codex_cli import CodexCLIProvider, StructuredReasoningProvider
from trader.agent.config import AgentConfig, load_agent_config, resolved_prompt_path
from trader.agent.invocation import WorkflowStep, invoke_role, resolve_role
from trader.agent.reasoning import (
    DailyDecision,
    assemble_daily_context,
    validate_daily_decision,
)
from trader.broker.models import Account, BrokerOrder, Position
from trader.persistence.repositories import persist_agent_decision, persist_trade_proposal
from trader.research.service import ResearchRunResult
from trader.settings import Settings
from trader.universe.models import UniverseScan

DAILY_TRADER = WorkflowStep(role="daily_trader")


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
            "abstention_classification": (
                None
                if self.decision.abstention is None
                else self.decision.abstention.classification
            ),
            "dissent_disposition_count": len(self.decision.dissent_dispositions),
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
    """Assemble the daily context, invoke the daily trader, and persist proposals only."""

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
        if not config.automatic_daily_run:
            raise ValueError("automatic daily reasoning is not enabled")
        self.role, self.profile = resolve_role(config, "daily_trader")
        self.session = session
        self.config = config
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

        def persist_proposals(decision: DailyDecision, invocation_id: str) -> None:
            persist_agent_decision(
                self.session,
                run_id,
                invocation_id,
                decision,
                commit=False,
            )
            for proposal in decision.proposals:
                persist_trade_proposal(
                    self.session,
                    run_id,
                    proposal,
                    agent_invocation_id=invocation_id,
                    commit=False,
                )

        result = invoke_role(
            self.session,
            self.config,
            run_id=run_id,
            run_directory=run_directory,
            workflow=DAILY_TRADER,
            prompt=self.prompt,
            context=context,
            output_model=DailyDecision,
            admitted_evidence_ids=context.admitted_evidence_ids,
            provider=self.provider,
            validate=lambda decision: validate_daily_decision(decision, context),
            on_output=persist_proposals,
        )
        return DailyReasoningResult(
            invocation_id=result.invocation_id,
            decision=result.output,
            context_hash=result.context_hash,
            prompt_hash=result.prompt_hash,
            evidence_manifest_hash=result.evidence_manifest_hash,
        )


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
