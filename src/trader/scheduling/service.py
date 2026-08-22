"""Database-backed scheduling with deterministic policy and lease-based claims."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trader.persistence.models import MarketEvent, Run, ScheduledRun, ScheduledRunEvent
from trader.persistence.repositories import PersistenceConflictError

from .config import DynamicRunsConfig
from .models import MarketEventRequest, ScheduleRequest, SchedulerTickReport

EASTERN = ZoneInfo("America/New_York")
COUNTED_STATUSES = frozenset({"PENDING", "CLAIMED", "RUNNING", "COMPLETED", "FAILED"})


class SchedulingPolicyError(ValueError):
    """Raised when a requested event or run violates deterministic policy."""


@dataclass(frozen=True)
class MarketEventReconciliation:
    """Result of reconciling one authoritative event observation."""

    event: MarketEvent
    rescheduled: bool
    cancelled_scheduled_run_ids: tuple[str, ...] = ()


class Scheduler:
    """Own durable event scheduling independently from any reasoning model."""

    def __init__(
        self,
        session: Session,
        config: DynamicRunsConfig,
        allowed_symbols: frozenset[str],
    ) -> None:
        self.session = session
        self.config = config
        self.allowed_symbols = allowed_symbols

    def register_market_event(
        self,
        request: MarketEventRequest,
        *,
        created_by_run_id: str | None = None,
    ) -> MarketEvent:
        """Persist one source-backed event, returning the row for identical retries."""
        self._require_enabled()
        self._validate_event_type(request.event_type)
        self._validate_symbols(request.symbols)
        scheduled_at = _utc(request.scheduled_at)
        announced_at = _utc(request.announced_at)
        if scheduled_at < announced_at:
            raise SchedulingPolicyError("market event cannot precede its announcement")
        if created_by_run_id is not None and self.session.get(Run, created_by_run_id) is None:
            raise SchedulingPolicyError("creating run does not exist")

        event_key = _event_key(request.source, request.source_event_id)
        symbols_json = _canonical_json(request.symbols)
        evidence_json = _canonical_json(request.evidence)
        raw_json = _canonical_json(request.raw) if request.raw is not None else None
        existing = self.session.scalar(
            select(MarketEvent).where(MarketEvent.event_key == event_key)
        )
        if existing is not None:
            _verify_same_event(
                existing,
                request,
                symbols_json,
                evidence_json,
                raw_json,
            )
            return existing

        record = MarketEvent(
            event_key=event_key,
            event_type=request.event_type,
            symbols_json=symbols_json,
            scheduled_at=scheduled_at,
            source=request.source,
            source_event_id=request.source_event_id,
            confidence=request.confidence,
            evidence_json=evidence_json,
            raw_json=raw_json,
            created_by_run_id=created_by_run_id,
            announced_at=announced_at,
        )
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            concurrent = self.session.scalar(
                select(MarketEvent).where(MarketEvent.event_key == event_key)
            )
            if concurrent is None:
                raise
            _verify_same_event(
                concurrent,
                request,
                symbols_json,
                evidence_json,
                raw_json,
            )
            return concurrent
        return record

    def reconcile_market_event(
        self,
        request: MarketEventRequest,
        *,
        observed_at: datetime,
        created_by_run_id: str | None = None,
    ) -> MarketEventReconciliation:
        """Reconcile a trusted source observation and safely replace a moved wake-up."""
        self._require_enabled()
        self._validate_event_type(request.event_type)
        self._validate_symbols(request.symbols)
        current = _utc(observed_at)
        scheduled_at = _utc(request.scheduled_at)
        announced_at = _utc(request.announced_at)
        if scheduled_at < announced_at:
            raise SchedulingPolicyError("market event cannot precede its announcement")
        if created_by_run_id is not None and self.session.get(Run, created_by_run_id) is None:
            raise SchedulingPolicyError("creating run does not exist")

        event_key = _event_key(request.source, request.source_event_id)
        existing = self.session.scalar(
            select(MarketEvent).where(MarketEvent.event_key == event_key)
        )
        if existing is None:
            event = self.register_market_event(
                request,
                created_by_run_id=created_by_run_id,
            )
            return MarketEventReconciliation(event=event, rescheduled=False)
        if existing.status != "ACTIVE":
            raise SchedulingPolicyError("cancelled market event cannot be reactivated by discovery")

        symbols_json = _canonical_json(request.symbols)
        if existing.event_type != request.event_type or existing.symbols_json != symbols_json:
            raise PersistenceConflictError(
                f"market event identity conflict for {existing.event_key}"
            )

        rescheduled = _utc(existing.scheduled_at) != scheduled_at
        cancelled_ids: list[str] = []
        if rescheduled:
            immutable_jobs = list(
                self.session.scalars(
                    select(ScheduledRun).where(
                        ScheduledRun.market_event_id == existing.id,
                        ScheduledRun.status.in_(
                            ("CLAIMED", "RUNNING", "COMPLETED", "FAILED", "EXPIRED")
                        ),
                    )
                )
            )
            if immutable_jobs:
                raise SchedulingPolicyError(
                    "cannot reschedule a market event after its worker was claimed"
                )
            pending_jobs = list(
                self.session.scalars(
                    select(ScheduledRun).where(
                        ScheduledRun.market_event_id == existing.id,
                        ScheduledRun.status == "PENDING",
                    )
                )
            )
            old_scheduled_at = _utc(existing.scheduled_at)
            for record in pending_jobs:
                record.status = "CANCELLED"
                record.completed_at = current
                record.updated_at = current
                self._record(
                    record,
                    "CANCELLED",
                    "CANCELLED",
                    "authoritative source rescheduled market event",
                    metadata={
                        "old_market_event_at": old_scheduled_at.isoformat(),
                        "new_market_event_at": scheduled_at.isoformat(),
                        "source": request.source,
                        "source_event_id": request.source_event_id,
                    },
                )
                cancelled_ids.append(record.id)

        evidence_json = _canonical_json(request.evidence)
        raw_json = _canonical_json(request.raw) if request.raw is not None else None
        changed = (
            rescheduled
            or existing.confidence != request.confidence
            or existing.evidence_json != evidence_json
            or existing.raw_json != raw_json
            or _utc(existing.announced_at) > announced_at
        )
        if changed:
            existing.scheduled_at = scheduled_at
            existing.confidence = request.confidence
            existing.evidence_json = evidence_json
            existing.raw_json = raw_json
            existing.announced_at = min(_utc(existing.announced_at), announced_at)
            existing.updated_at = current
            self.session.commit()
        return MarketEventReconciliation(
            event=existing,
            rescheduled=rescheduled,
            cancelled_scheduled_run_ids=tuple(cancelled_ids),
        )

    def schedule(
        self,
        request: ScheduleRequest,
        *,
        now: datetime | None = None,
        created_by_run_id: str | None = None,
    ) -> ScheduledRun:
        """Validate and persist a future run without granting the caller discretion."""
        self._require_enabled()
        current = _utc(now or datetime.now(UTC))
        event = self.session.get(MarketEvent, request.market_event_id)
        if event is None:
            raise SchedulingPolicyError("market event does not exist")
        if event.status != "ACTIVE":
            raise SchedulingPolicyError("market event is not active")
        if created_by_run_id is not None and self.session.get(Run, created_by_run_id) is None:
            raise SchedulingPolicyError("creating run does not exist")

        scheduled_for = _utc(request.scheduled_for)
        schedule_key = _schedule_key(event.id, scheduled_for)
        existing = self.session.scalar(
            select(ScheduledRun).where(ScheduledRun.schedule_key == schedule_key)
        )
        if existing is not None:
            _verify_same_schedule(existing, event, request)
            return existing

        event_time = _utc(event.scheduled_at)
        if scheduled_for <= current:
            raise SchedulingPolicyError("scheduled run must be in the future")
        if scheduled_for < event_time:
            raise SchedulingPolicyError("follow-up run cannot precede the market event")
        if scheduled_for > event_time + timedelta(
            minutes=self.config.max_event_followup_delay_minutes
        ):
            raise SchedulingPolicyError("follow-up run is too far after the market event")
        if scheduled_for > current + timedelta(days=self.config.max_schedule_horizon_days):
            raise SchedulingPolicyError("scheduled run exceeds the configured horizon")

        symbols = _load_symbols(event.symbols_json)
        self._validate_event_type(event.event_type)
        self._validate_symbols(symbols)
        self._validate_daily_limits(scheduled_for, symbols)
        self._validate_spacing(scheduled_for)

        expires_at = scheduled_for + timedelta(minutes=self.config.max_lateness_minutes)
        record = ScheduledRun(
            schedule_key=schedule_key,
            market_event_id=event.id,
            created_by_run_id=created_by_run_id,
            event_type=event.event_type,
            symbols_json=event.symbols_json,
            scheduled_for=scheduled_for,
            expires_at=expires_at,
            reason=request.reason,
            payload_json=_canonical_json(request.payload),
        )
        self.session.add(record)
        self.session.flush()
        self._record(record, "CREATED", "PENDING")
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            concurrent = self.session.scalar(
                select(ScheduledRun).where(ScheduledRun.schedule_key == schedule_key)
            )
            if concurrent is None:
                raise
            _verify_same_schedule(concurrent, event, request)
            return concurrent
        return record

    def cancel(self, scheduled_run_id: str, *, detail: str = "cancelled by operator") -> None:
        """Cancel only work that has not been claimed by a worker."""
        record = self._get_scheduled_run(scheduled_run_id)
        if record.status == "CANCELLED":
            return
        if record.status != "PENDING":
            raise SchedulingPolicyError("only pending scheduled runs may be cancelled")
        record.status = "CANCELLED"
        record.completed_at = datetime.now(UTC)
        record.updated_at = datetime.now(UTC)
        self._record(record, "CANCELLED", "CANCELLED", detail)
        self.session.commit()

    def cancel_market_event(
        self,
        market_event_id: str,
        *,
        detail: str = "market event cancelled by operator",
        now: datetime | None = None,
    ) -> int:
        """Cancel an event and all of its unclaimed future runs in one transaction."""
        current = _utc(now or datetime.now(UTC))
        market_event = self.session.get(MarketEvent, market_event_id)
        if market_event is None:
            raise SchedulingPolicyError(f"market event not found: {market_event_id}")
        if market_event.status == "CANCELLED":
            return 0
        active_jobs = list(
            self.session.scalars(
                select(ScheduledRun).where(
                    ScheduledRun.market_event_id == market_event_id,
                    ScheduledRun.status.in_(("CLAIMED", "RUNNING")),
                )
            )
        )
        if active_jobs:
            raise SchedulingPolicyError(
                "market event has an active worker lease and cannot be cancelled"
            )
        pending_jobs = list(
            self.session.scalars(
                select(ScheduledRun).where(
                    ScheduledRun.market_event_id == market_event_id,
                    ScheduledRun.status == "PENDING",
                )
            )
        )
        market_event.status = "CANCELLED"
        market_event.cancelled_at = current
        market_event.updated_at = current
        for record in pending_jobs:
            record.status = "CANCELLED"
            record.completed_at = current
            record.updated_at = current
            self._record(record, "CANCELLED", "CANCELLED", detail)
        self.session.commit()
        return len(pending_jobs)

    def list_scheduled_runs(self, *, status: str | None = None) -> list[ScheduledRun]:
        statement = select(ScheduledRun)
        if status is not None:
            statement = statement.where(ScheduledRun.status == status.upper())
        return list(self.session.scalars(statement.order_by(ScheduledRun.scheduled_for)))

    def tick(
        self,
        handler: Callable[[ScheduledRun], str],
        *,
        now: datetime | None = None,
    ) -> SchedulerTickReport:
        """Recover stale leases, claim due jobs, and execute each job at most once per claim."""
        if not self.config.enabled:
            return SchedulerTickReport()
        current = _utc(now or datetime.now(UTC))
        recovered = self._recover_expired_leases(current)
        expired = self._expire_overdue(current)
        claimed = self._claim_due(current, self.config.max_claims_per_tick)
        completed = 0
        failed = 0
        for record in claimed:
            token = record.lease_token
            if token is None:
                raise RuntimeError("claimed scheduled run is missing its lease token")
            self._mark_running(record.id, token, current)
            try:
                run_id = handler(record)
            except Exception as exc:
                self.session.rollback()
                self._mark_failed(record.id, token, str(exc), current)
                failed += 1
            else:
                self._mark_completed(record.id, token, run_id, current)
                completed += 1
        return SchedulerTickReport(
            recovered=recovered,
            expired=expired,
            claimed=len(claimed),
            completed=completed,
            failed=failed,
            scheduled_run_ids=tuple(record.id for record in claimed),
        )

    def run_due(
        self,
        scheduled_run_id: str,
        handler: Callable[[ScheduledRun], str],
        *,
        now: datetime | None = None,
    ) -> SchedulerTickReport:
        """Claim and execute one named due job, using the same lease rules as a tick."""
        self._require_enabled()
        current = _utc(now or datetime.now(UTC))
        self._recover_expired_leases(current)
        self._expire_overdue(current)
        record = self._claim_one(scheduled_run_id, current)
        if record is None:
            raise SchedulingPolicyError("scheduled run is not pending and due")
        token = record.lease_token
        if token is None:
            raise RuntimeError("claimed scheduled run is missing its lease token")
        self._mark_running(record.id, token, current)
        try:
            run_id = handler(record)
        except Exception as exc:
            self.session.rollback()
            self._mark_failed(record.id, token, str(exc), current)
            return SchedulerTickReport(
                claimed=1,
                failed=1,
                scheduled_run_ids=(record.id,),
            )
        self._mark_completed(record.id, token, run_id, current)
        return SchedulerTickReport(
            claimed=1,
            completed=1,
            scheduled_run_ids=(record.id,),
        )

    def _validate_daily_limits(self, scheduled_for: datetime, symbols: tuple[str, ...]) -> None:
        start, end = _eastern_day_bounds(scheduled_for)
        records = list(
            self.session.scalars(
                select(ScheduledRun).where(
                    ScheduledRun.scheduled_for >= start,
                    ScheduledRun.scheduled_for < end,
                    ScheduledRun.status.in_(COUNTED_STATUSES),
                )
            )
        )
        if len(records) >= self.config.max_extra_runs_per_day:
            raise SchedulingPolicyError("daily extra-run limit reached")
        for symbol in symbols:
            count = sum(symbol in _load_symbols(record.symbols_json) for record in records)
            if count >= self.config.max_extra_runs_per_symbol_per_day:
                raise SchedulingPolicyError(f"daily extra-run limit reached for {symbol}")

    def _validate_spacing(self, scheduled_for: datetime) -> None:
        spacing = timedelta(minutes=self.config.minimum_spacing_minutes)
        lower = scheduled_for - spacing
        upper = scheduled_for + spacing
        conflict = self.session.scalar(
            select(ScheduledRun.id)
            .where(
                ScheduledRun.status.in_(COUNTED_STATUSES),
                ScheduledRun.scheduled_for > lower,
                ScheduledRun.scheduled_for < upper,
            )
            .limit(1)
        )
        if conflict is not None:
            raise SchedulingPolicyError("scheduled run violates minimum spacing")

    def _validate_symbols(self, symbols: tuple[str, ...]) -> None:
        disallowed = set(symbols).difference(self.allowed_symbols)
        if disallowed:
            raise SchedulingPolicyError(
                f"symbols are outside the configured universe: {', '.join(sorted(disallowed))}"
            )

    def _validate_event_type(self, event_type: str) -> None:
        if event_type not in self.config.allowed_event_types:
            raise SchedulingPolicyError(f"event type is not allowed: {event_type}")

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise SchedulingPolicyError("dynamic runs are disabled")

    def _claim_due(self, now: datetime, limit: int) -> list[ScheduledRun]:
        candidate_ids = list(
            self.session.scalars(
                select(ScheduledRun.id)
                .where(
                    ScheduledRun.status == "PENDING",
                    ScheduledRun.scheduled_for <= now,
                    ScheduledRun.expires_at > now,
                )
                .order_by(ScheduledRun.scheduled_for, ScheduledRun.id)
                .limit(limit)
            )
        )
        claimed: list[ScheduledRun] = []
        for scheduled_run_id in candidate_ids:
            record = self._claim_one(scheduled_run_id, now)
            if record is not None:
                claimed.append(record)
        return claimed

    def _claim_one(self, scheduled_run_id: str, now: datetime) -> ScheduledRun | None:
        token = str(uuid4())
        result = self.session.execute(
            update(ScheduledRun)
            .where(
                ScheduledRun.id == scheduled_run_id,
                ScheduledRun.status == "PENDING",
                ScheduledRun.scheduled_for <= now,
                ScheduledRun.expires_at > now,
            )
            .values(
                status="CLAIMED",
                claimed_at=now,
                lease_expires_at=now + timedelta(minutes=self.config.lease_minutes),
                lease_token=token,
                attempt_count=ScheduledRun.attempt_count + 1,
                updated_at=now,
            )
        )
        rowcount = result.rowcount  # type: ignore[attr-defined]
        if rowcount != 1:
            self.session.rollback()
            return None
        record = self._get_scheduled_run(scheduled_run_id)
        self.session.refresh(record)
        self._record(record, "CLAIMED", "CLAIMED")
        self.session.commit()
        return record

    def _mark_running(self, scheduled_run_id: str, token: str, now: datetime) -> None:
        record = self._get_leased_run(scheduled_run_id, token, "CLAIMED")
        record.status = "RUNNING"
        record.started_at = now
        record.updated_at = now
        self._record(record, "STARTED", "RUNNING")
        self.session.commit()

    def _mark_completed(
        self,
        scheduled_run_id: str,
        token: str,
        run_id: str,
        now: datetime,
    ) -> None:
        record = self._get_leased_run(scheduled_run_id, token, "RUNNING")
        if record.run_id is not None and record.run_id != run_id:
            raise PersistenceConflictError(
                f"scheduled run {scheduled_run_id} was linked to a different run"
            )
        record.run_id = run_id
        record.status = "COMPLETED"
        record.completed_at = now
        record.updated_at = now
        record.lease_expires_at = None
        record.lease_token = None
        self._record(record, "COMPLETED", "COMPLETED", metadata={"run_id": run_id})
        self.session.commit()

    def _mark_failed(
        self,
        scheduled_run_id: str,
        token: str,
        error: str,
        now: datetime,
    ) -> None:
        record = self._get_leased_run(scheduled_run_id, token, "RUNNING")
        record.status = "FAILED"
        record.last_error = error
        record.completed_at = now
        record.updated_at = now
        record.lease_expires_at = None
        record.lease_token = None
        self._record(record, "FAILED", "FAILED", error)
        self.session.commit()

    def _expire_overdue(self, now: datetime) -> int:
        records = list(
            self.session.scalars(
                select(ScheduledRun).where(
                    ScheduledRun.status == "PENDING",
                    ScheduledRun.expires_at <= now,
                )
            )
        )
        for record in records:
            record.status = "EXPIRED"
            record.completed_at = now
            record.updated_at = now
            self._record(record, "EXPIRED", "EXPIRED", "run was not claimed before expiry")
        if records:
            self.session.commit()
        return len(records)

    def _recover_expired_leases(self, now: datetime) -> int:
        records = list(
            self.session.scalars(
                select(ScheduledRun).where(
                    ScheduledRun.status.in_(("CLAIMED", "RUNNING")),
                    ScheduledRun.lease_expires_at <= now,
                )
            )
        )
        for record in records:
            linked_run = self.session.get(Run, record.run_id) if record.run_id else None
            if linked_run is not None and linked_run.status in {"COMPLETED", "FAILED"}:
                record.status = linked_run.status
                record.completed_at = linked_run.completed_at or now
                record.last_error = linked_run.error_summary
                event_type = "RECOVERED_COMPLETION"
            elif linked_run is not None:
                linked_run.status = "FAILED"
                linked_run.completed_at = now
                linked_run.error_summary = "event-run worker lease expired"
                record.status = "FAILED"
                record.completed_at = now
                record.last_error = linked_run.error_summary
                event_type = "LEASE_EXPIRED"
            elif record.attempt_count >= self.config.max_attempts:
                record.status = "FAILED"
                record.completed_at = now
                record.last_error = "worker lease expired too many times"
                event_type = "LEASE_EXPIRED"
            else:
                record.status = "PENDING"
                record.claimed_at = None
                record.started_at = None
                event_type = "LEASE_RECOVERED"
            record.lease_expires_at = None
            record.lease_token = None
            record.updated_at = now
            self._record(record, event_type, record.status, record.last_error)
        if records:
            self.session.commit()
        return len(records)

    def _get_scheduled_run(self, scheduled_run_id: str) -> ScheduledRun:
        record = self.session.get(ScheduledRun, scheduled_run_id)
        if record is None:
            raise SchedulingPolicyError(f"scheduled run not found: {scheduled_run_id}")
        return record

    def _get_leased_run(self, scheduled_run_id: str, token: str, status: str) -> ScheduledRun:
        record = self._get_scheduled_run(scheduled_run_id)
        if record.status != status or record.lease_token != token:
            raise SchedulingPolicyError("scheduled-run lease is no longer owned by this worker")
        return record

    def _record(
        self,
        record: ScheduledRun,
        event_type: str,
        status: str,
        detail: str | None = None,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.session.add(
            ScheduledRunEvent(
                scheduled_run_id=record.id,
                event_type=event_type,
                status=status,
                detail=detail,
                metadata_json=_canonical_json(metadata) if metadata is not None else None,
            )
        )


def _event_key(source: str, source_event_id: str) -> str:
    material = f"{source.strip()}\0{source_event_id.strip()}".encode()
    return f"market-event:{hashlib.sha256(material).hexdigest()}"


def _schedule_key(market_event_id: str, scheduled_for: datetime) -> str:
    return f"event:{market_event_id}:{_utc(scheduled_for).isoformat()}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _load_symbols(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise PersistenceConflictError("persisted symbols must be a JSON list")
    if any(not isinstance(symbol, str) for symbol in parsed):
        raise PersistenceConflictError("persisted symbols must contain only strings")
    return tuple(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        # SQLite drops timezone metadata even for timezone-aware columns. Persisted values are UTC.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _eastern_day_bounds(value: datetime) -> tuple[datetime, datetime]:
    local_date = _utc(value).astimezone(EASTERN).date()
    start = datetime.combine(local_date, time.min, EASTERN).astimezone(UTC)
    end = datetime.combine(local_date + timedelta(days=1), time.min, EASTERN).astimezone(UTC)
    return start, end


def _verify_same_event(
    existing: MarketEvent,
    request: MarketEventRequest,
    symbols_json: str,
    evidence_json: str,
    raw_json: str | None,
) -> None:
    if not (
        existing.event_type == request.event_type
        and existing.symbols_json == symbols_json
        and _utc(existing.scheduled_at) == _utc(request.scheduled_at)
        and existing.source == request.source
        and existing.source_event_id == request.source_event_id
        and existing.confidence == request.confidence
        and existing.evidence_json == evidence_json
        and existing.raw_json == raw_json
        and _utc(existing.announced_at) == _utc(request.announced_at)
    ):
        raise PersistenceConflictError(
            f"market event idempotency conflict for {existing.event_key}"
        )


def _verify_same_schedule(
    existing: ScheduledRun,
    event: MarketEvent,
    request: ScheduleRequest,
) -> None:
    if not (
        existing.market_event_id == event.id
        and existing.event_type == event.event_type
        and existing.symbols_json == event.symbols_json
        and _utc(existing.scheduled_for) == _utc(request.scheduled_for)
        and existing.reason == request.reason
        and existing.payload_json == _canonical_json(request.payload)
    ):
        raise PersistenceConflictError(
            f"scheduled run idempotency conflict for {existing.schedule_key}"
        )
