"""Explicit, idempotent paper daily-run orchestration."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from trader.agent.runtime import DailyReasoningPipeline
from trader.broker.base import Broker
from trader.broker.models import Account
from trader.execution.reconciliation import Reconciler
from trader.logging.audit import audit
from trader.persistence.repositories import claim_run, event, snapshot
from trader.research.service import ResearchPipeline
from trader.risk.runtime import DailyRiskExecutionPipeline
from trader.scheduling.discovery import EventDiscoveryService
from trader.universe.scanner import CandidateScanner

SCHEDULE_TIME = "09:30"
SCHEDULE_ZONE = ZoneInfo("America/New_York")
ALPACA_PAPER_FORCED_OPTIONS_LEVEL = 3


def run_key(when: datetime) -> str:
    """Return the unique daily schedule key for the Eastern trading date."""
    if when.tzinfo is None:
        raise ValueError("scheduled time must be timezone-aware")
    trading_date = when.astimezone(SCHEDULE_ZONE).date()
    return f"daily:{trading_date.isoformat()}:{SCHEDULE_TIME}:{SCHEDULE_ZONE.key}"


def paper_test_run_key(when: datetime, nonce: UUID) -> str:
    """Return an explicitly non-production key for an additional audited paper run."""
    if when.tzinfo is None:
        raise ValueError("scheduled time must be timezone-aware")
    trading_date = when.astimezone(SCHEDULE_ZONE).date()
    return f"daily-test:{trading_date.isoformat()}:{nonce}"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, default=str, indent=2, sort_keys=True), encoding="utf-8")


def verify_paper_broker_configuration(
    broker: Broker,
    account: Account,
    *,
    run_id: str,
    component: str,
) -> str | None:
    """Fail closed on broker capabilities, returning any audited paper exception detail."""
    paper_options_exception = (
        broker.is_paper
        and broker.paper_options_level_is_provider_managed
        and account.options_level == ALPACA_PAPER_FORCED_OPTIONS_LEVEL
    )
    if (
        account.trading_blocked
        or account.shorting_enabled
        or account.multiplier > 1
        or (account.options_level > 0 and not paper_options_exception)
    ):
        raise RuntimeError("broker account configuration is less restrictive than required")
    if not paper_options_exception:
        return None
    detail = (
        "Alpaca paper account reports provider-managed options level 3; "
        "local equity-only asset controls remain mandatory"
    )
    audit(
        "paper_options_level_provider_managed",
        component=component,
        run_id=run_id,
        level=logging.WARNING,
        reported_options_level=account.options_level,
        required_live_options_level=0,
    )
    return detail


def daily_run(
    session: Session,
    broker: Broker,
    raw_root: Path,
    config_bytes: bytes,
    scheduled_for: datetime | None = None,
    universe_config_bytes: bytes | None = None,
    dynamic_runs_config_bytes: bytes | None = None,
    research_config_bytes: bytes | None = None,
    agents_config_bytes: bytes | None = None,
    strategy_bytes: bytes | None = None,
    portfolio_policy_bytes: bytes | None = None,
    event_discovery: EventDiscoveryService | None = None,
    candidate_scanner: CandidateScanner | None = None,
    research_pipeline: ResearchPipeline | None = None,
    reasoning_pipeline: DailyReasoningPipeline | None = None,
    risk_execution_pipeline: DailyRiskExecutionPipeline | None = None,
    research_event_symbols: tuple[str, ...] = (),
    test_rerun: bool = False,
) -> str:
    """Run the reconstructable daily paper pipeline with optional model and execution stages."""
    when = scheduled_for or datetime.now(UTC)
    key = paper_test_run_key(when, uuid4()) if test_rerun else run_key(when)
    config_material = (
        config_bytes
        if universe_config_bytes is None
        else config_bytes + b"\0" + universe_config_bytes
    )
    if dynamic_runs_config_bytes is not None:
        config_material += b"\0" + dynamic_runs_config_bytes
    if research_config_bytes is not None:
        config_material += b"\0" + research_config_bytes
    if agents_config_bytes is not None:
        config_material += b"\0" + agents_config_bytes
    if strategy_bytes is not None:
        config_material += b"\0" + strategy_bytes
    if portfolio_policy_bytes is not None:
        config_material += b"\0" + portfolio_policy_bytes
    run = claim_run(session, key, when, hashlib.sha256(config_material).hexdigest())
    if run is None:
        raise RuntimeError("run window already claimed; refusing duplicate")

    directory = raw_root / "paper" / "runs" / run.id
    try:
        directory.mkdir(parents=True, exist_ok=False)
        resolved_directory = directory.resolve()
        if not resolved_directory.is_relative_to(raw_root.resolve()):
            raise RuntimeError("run artifact directory escaped the configured raw-data root")
        run.raw_artifact_path = str(resolved_directory)
        session.commit()
        audit("run_started", component="daily_runner", run_id=run.id, run_key=key)

        if test_rerun:
            event(
                session,
                run.id,
                "TEST_RERUN",
                "Explicit non-destructive paper test rerun",
                metadata={"normal_daily_key": run_key(when)},
            )

        event(session, run.id, "VERIFY_DATABASE")
        account = broker.get_account()
        event(session, run.id, "FETCH_ACCOUNT")

        broker_configuration_detail = verify_paper_broker_configuration(
            broker,
            account,
            run_id=run.id,
            component="daily_runner",
        )
        paper_options_exception = broker_configuration_detail is not None
        event(
            session,
            run.id,
            "VERIFY_BROKER_CONFIGURATION",
            broker_configuration_detail,
        )

        positions = broker.get_positions()
        event(session, run.id, "FETCH_POSITIONS")
        orders = broker.get_open_orders()
        event(session, run.id, "FETCH_OPEN_ORDERS")

        (directory / "account_before.json").write_text(
            account.model_dump_json(indent=2), encoding="utf-8"
        )
        _write_json(
            directory / "positions_before.json",
            [position.model_dump(mode="json") for position in positions],
        )
        _write_json(
            directory / "open_orders_before.json",
            [order.model_dump(mode="json") for order in orders],
        )
        (directory / "risk_config.yaml").write_bytes(config_bytes)
        if universe_config_bytes is not None:
            (directory / "universe_config.yaml").write_bytes(universe_config_bytes)
        if dynamic_runs_config_bytes is not None:
            (directory / "dynamic_runs_config.yaml").write_bytes(dynamic_runs_config_bytes)
        if research_config_bytes is not None:
            (directory / "research_config.yaml").write_bytes(research_config_bytes)
        if agents_config_bytes is not None:
            (directory / "agents_config.yaml").write_bytes(agents_config_bytes)
        if strategy_bytes is not None:
            (directory / "strategy.md").write_bytes(strategy_bytes)
        if portfolio_policy_bytes is not None:
            (directory / "portfolio_policy.md").write_bytes(portfolio_policy_bytes)

        reconciliation = Reconciler(broker, session).reconcile()
        _write_json(
            directory / "reconciliation_before.json",
            reconciliation.model_dump(mode="json"),
        )
        event(session, run.id, "RECONCILE_PREVIOUS_ACTIVITY")
        if reconciliation.issues:
            issue_codes = ", ".join(issue.code for issue in reconciliation.issues)
            raise RuntimeError(f"broker reconciliation reported issues: {issue_codes}")
        if orders:
            raise RuntimeError("open order state must be reconciled before new activity")

        snapshot(session, account, positions, run.id)
        event(session, run.id, "TAKE_PORTFOLIO_SNAPSHOT")

        event(session, run.id, "CHECK_CIRCUIT_BREAKERS")

        candidate_scan = None
        if candidate_scanner is not None:
            candidate_scan = candidate_scanner.scan(
                as_of=when,
                portfolio_symbols=tuple(position.symbol for position in positions),
            )
            _write_json(
                directory / "eligible_assets.json",
                [asset.model_dump(mode="json") for asset in candidate_scan.eligible_assets],
            )
            _write_json(directory / "candidate_scan.json", candidate_scan.summary())
            event(
                session,
                run.id,
                "SCAN_CANDIDATE_UNIVERSE",
                metadata={
                    "eligible_asset_count": candidate_scan.eligible_asset_count,
                    "candidate_count": candidate_scan.candidate_count,
                    "asset_content_hash": candidate_scan.asset_content_hash,
                    "skipped_screener_symbols": candidate_scan.skipped_screener_symbols,
                },
            )
        else:
            event(session, run.id, "NO_CANDIDATE_SCANNER_CONFIGURED")

        discovery_summary = None
        if event_discovery is not None:
            discovery = event_discovery.discover_and_schedule(run.id, now=when)
            discovery_summary = discovery.summary
            (directory / "event_feed.json").write_bytes(discovery.raw_payload)
            _write_json(
                directory / "event_discovery.json",
                discovery.summary.model_dump(mode="json"),
            )
            event(
                session,
                run.id,
                "DISCOVER_MARKET_EVENTS",
                metadata={
                    "candidate_count": discovery.summary.candidate_count,
                    "registered_event_ids": discovery.summary.registered_event_ids,
                    "scheduled_run_ids": discovery.summary.scheduled_run_ids,
                    "rejected_count": len(discovery.summary.rejected),
                    "source_content_hash": discovery.summary.source_content_hash,
                },
            )
        else:
            event(session, run.id, "NO_EVENT_DISCOVERY_CONFIGURED")

        research_result = None
        if research_pipeline is not None:
            if candidate_scan is None:
                raise RuntimeError("research pipeline requires a candidate-universe scan")
            research_result = research_pipeline.run(
                run_id=run.id,
                run_directory=directory,
                scan=candidate_scan,
                portfolio_symbols=tuple(position.symbol for position in positions),
                event_symbols=research_event_symbols,
            )
            _write_json(
                directory / "research_plan.json",
                research_result.plan.model_dump(mode="json"),
            )
            _write_json(directory / "research_summary.json", research_result.summary())
            event(
                session,
                run.id,
                "COLLECT_SHADOW_RESEARCH",
                metadata={
                    "question_count": len(research_result.plan.questions),
                    "deep_symbol_count": len(research_result.plan.deep_symbols),
                    "http_request_count": research_result.total_request_count,
                    "unique_document_count": research_result.unique_document_count,
                    "persisted_research_ids": research_result.persisted_research_ids,
                },
            )
        else:
            event(session, run.id, "NO_RESEARCH_PIPELINE_CONFIGURED")

        reasoning_result = None
        if reasoning_pipeline is not None:
            if candidate_scan is None or research_result is None:
                raise RuntimeError(
                    "daily reasoning requires candidate scanning and persisted research"
                )
            reasoning_result = reasoning_pipeline.run(
                run_id=run.id,
                run_directory=directory,
                as_of=when,
                account=account,
                positions=tuple(positions),
                open_orders=tuple(orders),
                scan=candidate_scan,
                research=research_result,
            )
            _write_json(directory / "agent_summary.json", reasoning_result.summary())
            event(
                session,
                run.id,
                "RUN_DAILY_TRADER_PROPOSAL",
                metadata=reasoning_result.summary(),
            )
        else:
            event(session, run.id, "AGENT_REASONING_DISABLED")

        risk_result = None
        if risk_execution_pipeline is not None and reasoning_result is not None:
            if candidate_scan is None:
                raise RuntimeError("risk execution requires a candidate-universe scan")
            allowed_symbols = frozenset(
                {candidate.symbol for candidate in candidate_scan.candidates}
                | {position.symbol for position in positions}
            )
            risk_result = risk_execution_pipeline.run(
                run_id=run.id,
                run_directory=directory,
                account=account,
                positions=tuple(positions),
                open_orders=tuple(orders),
                proposals=reasoning_result.decision.proposals,
                allowed_symbols=allowed_symbols,
                allow_execution=not test_rerun,
            )
            event(
                session,
                run.id,
                "EVALUATE_AND_EXECUTE_PAPER_PROPOSALS",
                metadata=risk_result.summary(),
            )
            if test_rerun and any(item.approved for item in risk_result.decisions):
                event(
                    session,
                    run.id,
                    "TEST_RERUN_EXECUTION_BLOCKED",
                    "Test reruns may evaluate proposals but can never submit orders",
                )
        elif reasoning_result is not None:
            event(session, run.id, "RISK_EXECUTION_PIPELINE_DISABLED")
        else:
            event(session, run.id, "RISK_EXECUTION_SKIPPED_NO_REASONING")

        trading_date = when.astimezone(SCHEDULE_ZONE).date()
        decision_text = "NO_ACTION — agent reasoning is disabled."
        if reasoning_result is not None:
            if reasoning_result.decision.status == "NO_ACTION":
                decision_text = (
                    "NO_ACTION — " + (reasoning_result.decision.no_action_reason or "no reason")
                )
            else:
                decision_text = "\n".join(
                    f"- PROPOSED {proposal.action} {proposal.symbol}: {proposal.rationale}"
                    for proposal in reasoning_result.decision.proposals
                )
        report_title = "Daily Test Rerun" if test_rerun else "Daily Run"
        report = (
            f"# {report_title} — {trading_date}\n\n"
            "## Portfolio\n"
            f"Equity: ${account.equity}\n"
            f"Cash: ${account.cash}\n\n"
            "## Decisions\n"
            f"{decision_text}\n"
        )
        if discovery_summary is not None:
            report += (
                "\n## Event discovery (shadow mode)\n"
                f"Candidates: {discovery_summary.candidate_count}\n"
                f"Registered events: {len(discovery_summary.registered_event_ids)}\n"
                f"Scheduled follow-ups: {len(discovery_summary.scheduled_run_ids)}\n"
                f"Rejected follow-ups: {len(discovery_summary.rejected)}\n"
            )
        if candidate_scan is not None:
            report += (
                "\n## Candidate universe (shadow mode)\n"
                f"Eligible active/tradable U.S. equities: {candidate_scan.eligible_asset_count}\n"
                f"Bounded research candidates: {candidate_scan.candidate_count}\n"
                f"Asset snapshot hash: `{candidate_scan.asset_content_hash}`\n"
            )
        if research_result is not None:
            report += (
                "\n## Research collection (shadow mode)\n"
                f"Deep-research symbols: {len(research_result.plan.deep_symbols)}\n"
                f"Research questions: {len(research_result.plan.questions)}\n"
                f"HTTP requests: {research_result.total_request_count}\n"
                f"Unique retained documents: {research_result.unique_document_count}\n"
                f"Persisted evidence items: {len(research_result.persisted_research_ids)}\n"
            )
        if reasoning_result is not None:
            report += (
                "\n## Daily trader\n"
                f"Invocation: `{reasoning_result.invocation_id}`\n"
                f"Result: {reasoning_result.decision.status}\n"
                f"Proposals: "
                f"{len(reasoning_result.decision.proposals)}\n"
            )
        if risk_result is not None:
            report += (
                "\n## Deterministic risk and paper execution\n"
                f"Approved proposals: "
                f"{sum(item.approved for item in risk_result.decisions)}\n"
                f"Rejected proposals: "
                f"{sum(not item.approved for item in risk_result.decisions)}\n"
                f"Paper orders submitted: {len(risk_result.submitted_orders)}\n"
                f"Execution enabled: {risk_result.execution_enabled}\n"
            )
            for decision in risk_result.decisions:
                outcome = "APPROVED" if decision.approved else "REJECTED"
                codes = ", ".join(decision.rejection_codes) or "none"
                report += f"- {decision.proposal_id}: {outcome} ({codes})\n"
        elif reasoning_result is not None:
            report += "Broker execution was not configured.\n"
        if paper_options_exception:
            report += (
                "\n## Broker safety exception\n"
                "Alpaca forced this paper account to options level 3. Options remain disabled "
                "by the local equity-only risk policy.\n"
            )
        (directory / "daily_report.md").write_text(report, encoding="utf-8")
        event(session, run.id, "WRITE_DAILY_REPORT")

        manifest = {
            path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*")
            if path.is_file()
        }
        _write_json(directory / "manifest.json", manifest)

        run.status = "COMPLETED"
        run.completed_at = datetime.now(UTC)
        session.commit()
        audit("run_completed", component="daily_runner", run_id=run.id)
        return run.id
    except Exception as exc:
        session.rollback()
        run.status = "FAILED"
        run.error_summary = str(exc)
        run.completed_at = datetime.now(UTC)
        session.commit()
        event(session, run.id, "FAILED", str(exc))
        audit(
            "run_failed",
            component="daily_runner",
            run_id=run.id,
            level=40,
            error=str(exc),
        )
        raise
