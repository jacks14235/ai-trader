"""Audited no-action execution for a due scheduled market event."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.runner import verify_paper_broker_configuration
from trader.broker.base import Broker
from trader.execution.reconciliation import Reconciler
from trader.logging.audit import audit
from trader.persistence.models import Run, ScheduledRun
from trader.persistence.repositories import claim_run, event, snapshot


def event_run_key(scheduled_run_id: str) -> str:
    return f"scheduled-event:{scheduled_run_id}"


def event_run(
    session: Session,
    broker: Broker,
    scheduled_run: ScheduledRun,
    raw_root: Path,
    config_bytes: bytes,
    universe_config_bytes: bytes,
    dynamic_runs_config_bytes: bytes,
) -> str:
    """Execute a claimed paper event run without allowing any trade activity yet."""
    persisted = session.get(ScheduledRun, scheduled_run.id)
    if persisted is None or persisted.status != "RUNNING":
        raise RuntimeError("scheduled event run must hold a RUNNING scheduler lease")

    key = event_run_key(persisted.id)
    config_material = (
        config_bytes + b"\0" + universe_config_bytes + b"\0" + dynamic_runs_config_bytes
    )
    run = claim_run(
        session,
        key,
        persisted.scheduled_for,
        hashlib.sha256(config_material).hexdigest(),
    )
    if run is None:
        existing = session.scalar(select(Run).where(Run.run_key == key))
        if existing is not None and existing.status == "COMPLETED":
            persisted.run_id = existing.id
            session.commit()
            return existing.id
        status = existing.status if existing is not None else "UNKNOWN"
        raise RuntimeError(f"event run was already claimed with status {status}")

    persisted.run_id = run.id
    session.commit()
    directory = raw_root / "paper" / "runs" / run.id
    try:
        directory.mkdir(parents=True, exist_ok=False)
        audit(
            "event_run_started",
            component="event_runner",
            run_id=run.id,
            scheduled_run_id=persisted.id,
            event_type=persisted.event_type,
        )
        event(
            session,
            run.id,
            "VERIFY_SCHEDULE_TRIGGER",
            metadata={
                "scheduled_run_id": persisted.id,
                "market_event_id": persisted.market_event_id,
                "event_type": persisted.event_type,
            },
        )

        account = broker.get_account()
        event(session, run.id, "FETCH_ACCOUNT")
        broker_configuration_detail = verify_paper_broker_configuration(
            broker,
            account,
            run_id=run.id,
            component="event_runner",
        )
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

        _write_json(
            directory / "event_trigger.json",
            {
                "scheduled_run_id": persisted.id,
                "market_event_id": persisted.market_event_id,
                "event_type": persisted.event_type,
                "symbols": json.loads(persisted.symbols_json),
                "scheduled_for": _persisted_utc(persisted.scheduled_for),
                "expires_at": _persisted_utc(persisted.expires_at),
                "reason": persisted.reason,
                "payload": json.loads(persisted.payload_json),
            },
        )
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
        (directory / "universe_config.yaml").write_bytes(universe_config_bytes)
        (directory / "dynamic_runs_config.yaml").write_bytes(dynamic_runs_config_bytes)

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
            raise RuntimeError("open order state must be reconciled before event activity")

        snapshot(session, account, positions, run.id, snapshot_type="EVENT")
        event(session, run.id, "TAKE_PORTFOLIO_SNAPSHOT")
        event(session, run.id, "CHECK_CIRCUIT_BREAKERS")
        event(session, run.id, "NO_EVENT_AGENT_CONFIGURED")

        report = (
            f"# Event Run — {persisted.event_type}\n\n"
            f"Symbols: {', '.join(json.loads(persisted.symbols_json))}\n\n"
            f"Reason: {persisted.reason}\n\n"
            "## Decisions\n"
            "NO_ACTION — event reasoning provider is not configured.\n\n"
            "This event run cannot bypass the normal market-hours, risk, or execution gates.\n"
        )
        (directory / "event_report.md").write_text(report, encoding="utf-8")
        event(session, run.id, "WRITE_EVENT_REPORT")

        manifest = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.iterdir()
        }
        _write_json(directory / "manifest.json", manifest)

        run.status = "COMPLETED"
        run.completed_at = datetime.now(UTC)
        session.commit()
        audit(
            "event_run_completed",
            component="event_runner",
            run_id=run.id,
            scheduled_run_id=persisted.id,
        )
        return run.id
    except Exception as exc:
        session.rollback()
        run.status = "FAILED"
        run.error_summary = str(exc)
        run.completed_at = datetime.now(UTC)
        session.commit()
        event(session, run.id, "FAILED", str(exc))
        audit(
            "event_run_failed",
            component="event_runner",
            run_id=run.id,
            scheduled_run_id=persisted.id,
            level=40,
            error=str(exc),
        )
        raise


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, default=str, indent=2, sort_keys=True), encoding="utf-8")


def _persisted_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
