import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.event_runner import event_run
from trader.broker.base import Broker
from trader.broker.models import (
    Account,
    BrokerFill,
    BrokerOrder,
    OrderQueryStatus,
    Position,
)
from trader.persistence.db import create_session_factory
from trader.persistence.models import Run, ScheduledRun, ScheduledRunEvent
from trader.persistence.repositories import (
    PersistenceConflictError,
    claim_run,
    get_run_audit_trail,
)
from trader.scheduling.config import DynamicRunsConfig
from trader.scheduling.models import EventType, MarketEventRequest, ScheduleRequest
from trader.scheduling.service import Scheduler, SchedulingPolicyError

BASE_TIME = datetime(2026, 8, 20, 12, tzinfo=UTC)


class EmptyPaperBroker:
    @property
    def is_paper(self) -> bool:
        return True

    @property
    def paper_options_level_is_provider_managed(self) -> bool:
        return False

    def get_account(self) -> Account:
        return Account(equity="2000", cash="2000", buying_power="2000")

    def get_positions(self) -> list[Position]:
        return []

    def get_open_orders(self) -> list[BrokerOrder]:
        return []

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]:
        del status, after, until, limit
        return []

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        del client_order_id
        return None

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]:
        del after, until
        return []


def test_dynamic_run_config_rejects_relaxed_or_unknown_limits() -> None:
    content = _config_dict()
    content["minimum_spacing_minutes"] = 1
    with pytest.raises(ValidationError, match="cannot be less than 30"):
        DynamicRunsConfig.model_validate(content)

    content = _config_dict()
    content["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        DynamicRunsConfig.model_validate(content)


def test_market_event_requires_allowed_symbols_and_is_idempotent(tmp_path: Path) -> None:
    scheduler = _scheduler(_session(tmp_path))
    request = _event_request()

    first = scheduler.register_market_event(request)
    retried = scheduler.register_market_event(request)

    assert first.id == retried.id
    with pytest.raises(SchedulingPolicyError, match="outside the configured universe"):
        scheduler.register_market_event(
            _event_request(source_event_id="outside", symbols=("AAPL",))
        )
    with pytest.raises(PersistenceConflictError, match="idempotency conflict"):
        scheduler.register_market_event(request.model_copy(update={"confidence": 0.5}))


def test_schedule_is_idempotent_and_enforces_spacing_and_symbol_caps(tmp_path: Path) -> None:
    scheduler = _scheduler(_session(tmp_path))
    first_event = scheduler.register_market_event(_event_request())
    first_request = _schedule_request(first_event.id)

    first = scheduler.schedule(first_request, now=BASE_TIME)
    retried = scheduler.schedule(first_request, now=BASE_TIME)

    assert first.id == retried.id
    close_event = scheduler.register_market_event(
        _event_request(
            source_event_id="close",
            scheduled_at=BASE_TIME + timedelta(hours=2, minutes=20),
        )
    )
    with pytest.raises(SchedulingPolicyError, match="minimum spacing"):
        scheduler.schedule(
            _schedule_request(
                close_event.id,
                scheduled_for=BASE_TIME + timedelta(hours=2, minutes=25),
            ),
            now=BASE_TIME,
        )

    second_event = scheduler.register_market_event(
        _event_request(
            source_event_id="second",
            scheduled_at=BASE_TIME + timedelta(hours=3),
        )
    )
    scheduler.schedule(
        _schedule_request(
            second_event.id,
            scheduled_for=BASE_TIME + timedelta(hours=3, minutes=5),
        ),
        now=BASE_TIME,
    )
    third_event = scheduler.register_market_event(
        _event_request(
            source_event_id="third",
            scheduled_at=BASE_TIME + timedelta(hours=4),
        )
    )
    with pytest.raises(SchedulingPolicyError, match="limit reached for SPY"):
        scheduler.schedule(
            _schedule_request(
                third_event.id,
                scheduled_for=BASE_TIME + timedelta(hours=4, minutes=5),
            ),
            now=BASE_TIME,
        )


def test_scheduler_tick_claims_once_and_records_lifecycle(tmp_path: Path) -> None:
    session = _session(tmp_path)
    scheduler = _scheduler(session)
    scheduled = _create_schedule(scheduler)
    handled: list[str] = []

    def handler(record: ScheduledRun) -> str:
        handled.append(record.id)
        run = claim_run(
            session,
            f"test-event:{record.id}",
            record.scheduled_for,
            "config-hash",
        )
        assert run is not None
        run.status = "COMPLETED"
        run.completed_at = BASE_TIME + timedelta(hours=2, minutes=6)
        session.commit()
        return run.id

    due = BASE_TIME + timedelta(hours=2, minutes=6)
    first = scheduler.tick(handler, now=due)
    second = scheduler.tick(handler, now=due)

    assert first.claimed == first.completed == 1
    assert second.claimed == 0
    assert handled == [scheduled.id]
    persisted = session.get(ScheduledRun, scheduled.id)
    assert persisted is not None
    assert persisted.status == "COMPLETED"
    assert persisted.attempt_count == 1
    lifecycle = list(
        session.scalars(
            select(ScheduledRunEvent.event_type)
            .where(ScheduledRunEvent.scheduled_run_id == scheduled.id)
            .order_by(ScheduledRunEvent.occurred_at, ScheduledRunEvent.id)
        )
    )
    assert lifecycle == ["CREATED", "CLAIMED", "STARTED", "COMPLETED"]


def test_scheduler_expires_late_jobs_without_running_handler(tmp_path: Path) -> None:
    session = _session(tmp_path)
    scheduler = _scheduler(session)
    scheduled = _create_schedule(scheduler)

    report = scheduler.tick(
        lambda _record: pytest.fail("expired job must not run"),
        now=BASE_TIME + timedelta(hours=3),
    )

    assert report.expired == 1
    assert report.claimed == 0
    assert session.get(ScheduledRun, scheduled.id).status == "EXPIRED"  # type: ignore[union-attr]


def test_market_event_cancellation_cascades_to_pending_run(tmp_path: Path) -> None:
    session = _session(tmp_path)
    scheduler = _scheduler(session)
    scheduled = _create_schedule(scheduler)

    cancelled = scheduler.cancel_market_event(
        scheduled.market_event_id,
        now=BASE_TIME + timedelta(minutes=1),
    )

    assert cancelled == 1
    persisted = session.get(ScheduledRun, scheduled.id)
    assert persisted is not None
    assert persisted.status == "CANCELLED"
    report = scheduler.tick(
        lambda _record: pytest.fail("cancelled job must not run"),
        now=BASE_TIME + timedelta(hours=2, minutes=6),
    )
    assert report.claimed == 0


def test_scheduler_records_handler_failure_and_does_not_retry(tmp_path: Path) -> None:
    session = _session(tmp_path)
    scheduler = _scheduler(session)
    scheduled = _create_schedule(scheduler)

    def failing_handler(_record: ScheduledRun) -> str:
        raise RuntimeError("provider unavailable")

    due = BASE_TIME + timedelta(hours=2, minutes=6)
    report = scheduler.tick(failing_handler, now=due)
    retry = scheduler.tick(failing_handler, now=due)

    assert report.failed == 1
    assert retry.claimed == 0
    persisted = session.get(ScheduledRun, scheduled.id)
    assert persisted is not None
    assert persisted.status == "FAILED"
    assert persisted.last_error == "provider unavailable"


def test_event_run_is_audited_no_action_job(tmp_path: Path) -> None:
    session = _session(tmp_path)
    scheduler = _scheduler(session)
    scheduled = _create_schedule(scheduler)
    raw_root = tmp_path / "raw"

    report = scheduler.tick(
        lambda record: event_run(
            session,
            cast(Broker, EmptyPaperBroker()),
            record,
            raw_root,
            b"mode: paper\n",
            b"allowed_symbols: [SPY]\nbenchmark_symbols: [SPY]\n",
            b"enabled: true\n",
        ),
        now=BASE_TIME + timedelta(hours=2, minutes=6),
    )

    assert report.completed == 1
    persisted = session.get(ScheduledRun, scheduled.id)
    assert persisted is not None
    assert persisted.status == "COMPLETED"
    assert persisted.run_id is not None
    run = session.get(Run, persisted.run_id)
    assert run is not None
    assert run.status == "COMPLETED"
    trail = get_run_audit_trail(session, run.id)
    assert trail.scheduled_run is not None
    assert trail.scheduled_run.id == scheduled.id
    assert trail.market_event is not None
    assert trail.market_event.id == scheduled.market_event_id
    assert [item.event_type for item in trail.scheduled_run_events] == [
        "CREATED",
        "CLAIMED",
        "STARTED",
        "COMPLETED",
    ]
    directory = raw_root / "paper" / "runs" / run.id
    assert "NO_ACTION" in (directory / "event_report.md").read_text()
    trigger = json.loads((directory / "event_trigger.json").read_text())
    assert trigger["scheduled_run_id"] == scheduled.id
    assert trigger["scheduled_for"].endswith("+00:00")
    assert set(json.loads((directory / "manifest.json").read_text())) == {
        "account_before.json",
        "dynamic_runs_config.yaml",
        "event_report.md",
        "event_trigger.json",
        "open_orders_before.json",
        "positions_before.json",
        "reconciliation_before.json",
        "risk_config.yaml",
        "universe_config.yaml",
    }


def _session(tmp_path: Path) -> Session:
    return create_session_factory(f"sqlite:///{tmp_path}/scheduler.sqlite")()


def _scheduler(session: Session) -> Scheduler:
    return Scheduler(session, DynamicRunsConfig.model_validate(_config_dict()), frozenset({"SPY"}))


def _config_dict() -> dict[str, object]:
    return {
        "enabled": True,
        "max_extra_runs_per_day": 3,
        "max_extra_runs_per_symbol_per_day": 2,
        "minimum_spacing_minutes": 30,
        "max_schedule_horizon_days": 30,
        "max_event_followup_delay_minutes": 360,
        "max_lateness_minutes": 30,
        "lease_minutes": 10,
        "max_attempts": 2,
        "max_claims_per_tick": 3,
        "allowed_event_types": [
            "EARNINGS_RELEASE",
            "EARNINGS_CALL",
            "FDA_DECISION",
            "INVESTOR_DAY",
            "ECONOMIC_RELEASE",
        ],
        "discovery": {
            "enabled": True,
            "source": {
                "provider": "file",
                "feed_path": "config/event_feed.json",
            },
            "lookahead_days": 2,
            "minimum_confidence": 0.8,
            "followup_offsets_minutes": {
                "EARNINGS_RELEASE": 10,
                "EARNINGS_CALL": 15,
                "FDA_DECISION": 10,
                "INVESTOR_DAY": 15,
                "ECONOMIC_RELEASE": 10,
            },
        },
    }


def _event_request(
    *,
    source_event_id: str = "earnings-1",
    symbols: tuple[str, ...] = ("SPY",),
    scheduled_at: datetime = BASE_TIME + timedelta(hours=2),
    event_type: EventType = "EARNINGS_RELEASE",
) -> MarketEventRequest:
    return MarketEventRequest(
        event_type=event_type,
        symbols=symbols,
        scheduled_at=scheduled_at,
        source="test-calendar",
        source_event_id=source_event_id,
        confidence=0.95,
        evidence={"url": "https://example.test/event"},
        announced_at=BASE_TIME - timedelta(hours=1),
    )


def _schedule_request(
    market_event_id: str,
    *,
    scheduled_for: datetime = BASE_TIME + timedelta(hours=2, minutes=5),
) -> ScheduleRequest:
    return ScheduleRequest(
        market_event_id=market_event_id,
        scheduled_for=scheduled_for,
        reason="Review the verified release after publication",
    )


def _create_schedule(scheduler: Scheduler) -> ScheduledRun:
    event = scheduler.register_market_event(_event_request())
    return scheduler.schedule(_schedule_request(event.id), now=BASE_TIME)
