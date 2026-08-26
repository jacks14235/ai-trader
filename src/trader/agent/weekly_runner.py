"""Weekend strategy review over a completed period.

This module deliberately accepts no ``Broker``. The strategist reviews decisions that were already
made and authorized; it has nothing to execute and no market to reach. Its only output is a
proposal recorded for human review.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from trader.agent.codex_cli import CodexCLIProvider, StructuredReasoningProvider
from trader.agent.config import AgentConfig, load_agent_config, resolved_prompt_path
from trader.agent.invocation import WorkflowStep, invoke_role, resolve_role
from trader.agent.weekly import (
    StrategyRecommendation,
    WeeklyAgentContext,
    assemble_weekly_context,
    validate_strategy_recommendation,
)
from trader.ledger.knowledge import record_strategy_review
from trader.ledger.models import StrategyVersion, WeeklyPerformance
from trader.ledger.service import record_run_report
from trader.ledger.strategy import attribute_run_to_strategy, record_strategy_version
from trader.logging.audit import audit
from trader.persistence.repositories import claim_run, event
from trader.settings import Settings

SCHEDULE_ZONE = ZoneInfo("America/New_York")
REVIEW_PERIOD_DAYS = 7
WEEKLY_STRATEGIST = WorkflowStep(role="weekly_strategist")


@dataclass(frozen=True)
class WeeklyReviewResult:
    run_id: str
    invocation_id: str
    recommendation: StrategyRecommendation
    performance: WeeklyPerformance
    strategy_version: StrategyVersion
    knowledge_change_ids: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "invocation_id": self.invocation_id,
            "status": self.recommendation.status,
            "strategy_id": self.strategy_version.strategy_id,
            "strategy_name": self.strategy_version.name,
            "knowledge_change_ids": list(self.knowledge_change_ids),
            "period_start": self.performance.period_start.isoformat(),
            "period_end": self.performance.period_end.isoformat(),
            "run_count": self.performance.run_count,
            "proposal_count": self.performance.proposal_count,
            "approved_count": self.performance.approved_count,
            "rejected_count": self.performance.rejected_count,
        }


def review_period(when: datetime, *, days: int = REVIEW_PERIOD_DAYS) -> tuple[datetime, datetime]:
    """Return the completed span the weekend review covers, in UTC.

    The period ends at the most recent Saturday midnight Eastern at or before ``when``, so a
    review run on the weekend covers the trading week that just finished rather than a partial one.
    """
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("review time must be timezone-aware")
    if days <= 0:
        raise ValueError("review period must span at least one day")
    eastern = when.astimezone(SCHEDULE_ZONE)
    midnight = eastern.replace(hour=0, minute=0, second=0, microsecond=0)
    # Monday is 0, so Saturday is 5.
    end = midnight - timedelta(days=(eastern.weekday() - 5) % 7)
    return (end - timedelta(days=days)).astimezone(UTC), end.astimezone(UTC)


def weekly_run_key(period_end: datetime) -> str:
    return f"weekly:{period_end.astimezone(SCHEDULE_ZONE).date().isoformat()}:{SCHEDULE_ZONE.key}"


def weekly_test_run_key(period_end: datetime, nonce: UUID) -> str:
    """Return an explicitly non-production key for an additional audited review."""
    date = period_end.astimezone(SCHEDULE_ZONE).date().isoformat()
    return f"weekly-test:{date}:{nonce}"


def weekly_run(
    session: Session,
    raw_root: Path,
    config: AgentConfig,
    *,
    prompt: str,
    strategy: str,
    portfolio_policy: str,
    provider: StructuredReasoningProvider,
    as_of: datetime | None = None,
    period_days: int = REVIEW_PERIOD_DAYS,
    agents_config_bytes: bytes | None = None,
    test_rerun: bool = False,
) -> WeeklyReviewResult:
    """Review one completed period and record a strategy proposal for human review."""
    role, _profile = resolve_role(config, "weekly_strategist")
    if not role.permissions.can_mutate_knowledge:
        raise ValueError("the weekly strategist must be permitted to propose knowledge changes")
    when = as_of or datetime.now(UTC)
    period_start, period_end = review_period(when, days=period_days)
    key = (
        weekly_test_run_key(period_end, uuid4())
        if test_rerun
        else weekly_run_key(period_end)
    )
    config_material = strategy.encode() + b"\0" + portfolio_policy.encode()
    if agents_config_bytes is not None:
        config_material += b"\0" + agents_config_bytes
    run = claim_run(session, key, when, hashlib.sha256(config_material).hexdigest())
    if run is None:
        raise RuntimeError("weekly review window already claimed; refusing duplicate")

    directory = raw_root / "paper" / "runs" / run.id
    try:
        directory.mkdir(parents=True, exist_ok=False)
        resolved = directory.resolve()
        if not resolved.is_relative_to(raw_root.resolve()):
            raise RuntimeError("run artifact directory escaped the configured raw-data root")
        run.raw_artifact_path = str(resolved)
        session.commit()
        audit("run_started", component="weekly_runner", run_id=run.id, run_key=key)
        if test_rerun:
            event(
                session,
                run.id,
                "TEST_RERUN",
                "Explicit non-destructive weekly review rerun",
                metadata={"normal_weekly_key": weekly_run_key(period_end)},
            )

        (directory / "strategy.md").write_text(strategy, encoding="utf-8")
        (directory / "portfolio_policy.md").write_text(portfolio_policy, encoding="utf-8")
        if agents_config_bytes is not None:
            (directory / "agents_config.yaml").write_bytes(agents_config_bytes)

        strategy_version = record_strategy_version(
            session,
            document=strategy,
            as_of=when,
            markdown_path="knowledge/strategy.md",
        )
        attribute_run_to_strategy(
            session,
            run_id=run.id,
            strategy_id=strategy_version.strategy_id,
        )
        event(
            session,
            run.id,
            "RECORD_STRATEGY_VERSION",
            metadata={
                "strategy_id": strategy_version.strategy_id,
                "name": strategy_version.name,
                "content_hash": strategy_version.content_hash,
            },
        )

        context = assemble_weekly_context(
            session,
            run_id=run.id,
            as_of=when,
            period_start=period_start,
            period_end=period_end,
            strategy=strategy,
            strategy_version=strategy_version,
            portfolio_policy=portfolio_policy,
            role=role,
        )
        _write_json(directory / "weekly_performance.json", context.performance.model_dump(
            mode="json"
        ))
        event(
            session,
            run.id,
            "ASSEMBLE_WEEKLY_REVIEW_CONTEXT",
            metadata={
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "run_count": context.performance.run_count,
                "proposal_count": context.performance.proposal_count,
                "thesis_outcome_count": len(context.performance.thesis_outcomes),
                "has_sample": context.performance.has_sample(),
            },
        )
        if not context.performance.has_sample():
            event(
                session,
                run.id,
                "WEEKLY_REVIEW_PERIOD_IS_EMPTY",
                "The period contains no decisions; a change cannot be proposed from it",
            )

        recorded: list[str] = []

        def persist_review(
            recommendation: StrategyRecommendation,
            _invocation_id: str,
        ) -> None:
            recorded.extend(
                record_strategy_review(
                    session,
                    run_id=run.id,
                    strategy_version=strategy_version,
                    recommendation=recommendation,
                    as_of=when,
                )
            )

        invocation = invoke_role(
            session,
            config,
            run_id=run.id,
            run_directory=directory,
            workflow=WEEKLY_STRATEGIST,
            prompt=prompt,
            context=context,
            output_model=StrategyRecommendation,
            admitted_evidence_ids=(),
            provider=provider,
            validate=lambda item: validate_strategy_recommendation(item, context),
            on_output=persist_review,
        )
        event(
            session,
            run.id,
            "RUN_WEEKLY_STRATEGIST",
            metadata={
                "invocation_id": invocation.invocation_id,
                "status": invocation.output.status,
                "proposed_change_count": len(invocation.output.proposed_changes),
                "knowledge_change_ids": recorded,
            },
        )

        report = _report(context, invocation.output, tuple(recorded), test_rerun=test_rerun)
        (directory / "weekly_report.md").write_text(report, encoding="utf-8")
        record_run_report(
            session,
            run_id=run.id,
            report_path="weekly_report.md",
            content=report,
            summary=_summary_line(invocation.output),
        )
        event(session, run.id, "WRITE_WEEKLY_REPORT")

        manifest = {
            path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*")
            if path.is_file()
        }
        _write_json(directory / "manifest.json", manifest)

        run.status = "COMPLETED"
        run.completed_at = datetime.now(UTC)
        session.commit()
        audit("run_completed", component="weekly_runner", run_id=run.id)
        return WeeklyReviewResult(
            run_id=run.id,
            invocation_id=invocation.invocation_id,
            recommendation=invocation.output,
            performance=context.performance,
            strategy_version=strategy_version,
            knowledge_change_ids=tuple(recorded),
        )
    except Exception as exc:
        session.rollback()
        run.status = "FAILED"
        run.error_summary = str(exc)
        run.completed_at = datetime.now(UTC)
        session.commit()
        event(session, run.id, "FAILED", str(exc))
        audit(
            "run_failed",
            component="weekly_runner",
            run_id=run.id,
            level=40,
            error=str(exc),
        )
        raise


def configured_weekly_review(
    settings: Settings,
    session: Session,
    *,
    provider: StructuredReasoningProvider | None = None,
    as_of: datetime | None = None,
    test_rerun: bool = False,
) -> WeeklyReviewResult:
    """Run the review only when both the configuration and the environment enable the role."""
    config = load_agent_config(settings.trader_agents_config)
    if not settings.trader_strategist_enabled:
        raise RuntimeError("TRADER_STRATEGIST_ENABLED=true is required for a weekly review")
    prompt_path = resolved_prompt_path(
        settings.trader_agents_config, config.roles["weekly_strategist"]
    )
    return weekly_run(
        session,
        settings.trader_raw_data_dir,
        config,
        prompt=prompt_path.read_text(encoding="utf-8"),
        strategy=settings.trader_strategy_document.read_text(encoding="utf-8"),
        portfolio_policy=settings.trader_portfolio_policy.read_text(encoding="utf-8"),
        provider=provider or CodexCLIProvider(),
        as_of=as_of,
        agents_config_bytes=settings.trader_agents_config.read_bytes(),
        test_rerun=test_rerun,
    )


def _summary_line(recommendation: StrategyRecommendation) -> str:
    if recommendation.status == "NO_CHANGE":
        return f"NO_CHANGE — {recommendation.no_change_reason or 'no reason given'}"
    headings = ", ".join(change.section_heading for change in recommendation.proposed_changes)
    return f"PROPOSE_CHANGE — {headings}"


def _report(
    context: WeeklyAgentContext,
    recommendation: StrategyRecommendation,
    change_ids: tuple[str, ...],
    *,
    test_rerun: bool,
) -> str:
    performance = context.performance
    title = "Weekly Strategy Review (test rerun)" if test_rerun else "Weekly Strategy Review"
    lines = [
        f"# {title} — {performance.period_start.date()} to {performance.period_end.date()}",
        "",
        "## Strategy version reviewed",
        f"Name: {context.strategy_version.name}",
        f"Content hash: `{context.strategy_version.content_hash}`",
        "",
        "## Period record",
        f"Daily runs: {performance.run_count}",
        f"Runs ending in no action: {performance.no_action_run_count}",
        f"Proposals: {performance.proposal_count}",
        f"Risk approved: {performance.approved_count}",
        f"Risk rejected: {performance.rejected_count}",
        f"Orders submitted: {performance.submitted_order_count}",
        f"Orders filled: {performance.filled_order_count}",
        f"Theses opened: {performance.theses_opened}",
        f"Theses closed: {performance.theses_closed}",
        "",
        "## Equity",
        f"Starting equity: {_money(performance.starting_equity)}",
        f"Ending equity: {_money(performance.ending_equity)}",
        f"Change: {_money(performance.pnl)} ({_percent(performance.return_pct)})",
        f"Peak equity: {_money(performance.peak_equity)}",
        f"Maximum drawdown: {_percent(performance.max_drawdown_pct)}",
    ]
    if performance.rejection_codes:
        lines += ["", "## Why proposals were rejected"]
        lines += [f"- {item.code}: {item.count}" for item in performance.rejection_codes]
    if performance.thesis_outcomes:
        lines += ["", "## Thesis outcomes"]
        for outcome in performance.thesis_outcomes:
            realized = (
                "unrealized" if outcome.realized_pnl is None else _money(outcome.realized_pnl)
            )
            lines.append(
                f"- {outcome.symbol} ({outcome.status}): realized {realized}, "
                f"open qty {outcome.open_qty}, fills {outcome.fill_count} "
                f"[`{outcome.thesis_id}`]"
            )
    lines += [
        "",
        "## Review",
        f"Result: {recommendation.status}",
        "",
        f"{recommendation.diagnosis}",
        "",
        "### Process assessment",
        recommendation.process_assessment,
    ]
    if recommendation.status == "NO_CHANGE":
        lines += ["", "### No change", recommendation.no_change_reason or "no reason given"]
    for change, change_id in zip(recommendation.proposed_changes, change_ids, strict=False):
        lines += [
            "",
            f"### Proposed change — {change.section_heading}",
            f"Proposal id: `{change_id}` (pending human review; nothing has been applied)",
            "",
            "Current text:",
            "",
            "```",
            change.current_text,
            "```",
            "",
            "Proposed replacement:",
            "",
            "```",
            change.replacement_text,
            "```",
            "",
            f"Hypothesis: {change.hypothesis}",
            "",
            f"Disconfirming evidence: {change.disconfirming_evidence}",
            "",
            f"Expected effect: {change.expected_effect}",
            "",
            f"Evaluation plan: {change.evaluation_plan}",
            "",
            f"Revert criteria: {change.revert_criteria}",
        ]
        if change.failure_modes:
            lines += ["", "Failure modes:"]
            lines += [f"- {item}" for item in change.failure_modes]
    return "\n".join(lines) + "\n"


def _money(value: object) -> str:
    return "n/a" if value is None else f"${value}"


def _percent(value: object) -> str:
    return "n/a" if value is None else f"{value}%"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, default=str, indent=2, sort_keys=True), encoding="utf-8")
