"""Deterministic registration and shadow scheduling for discovered events."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from trader.research.events import EventProvider

from .config import DiscoveryConfig
from .models import ScheduleRequest
from .service import Scheduler, SchedulingPolicyError


class DiscoveryRejection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_event_id: str
    reason: str


class DiscoverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    source_reference: str
    retrieval_mode: str
    retrieved_at: datetime
    source_updated_at: str | None
    window_start: datetime
    window_end: datetime
    source_content_hash: str
    candidate_count: int
    registered_event_ids: tuple[str, ...]
    rescheduled_event_ids: tuple[str, ...]
    cancelled_scheduled_run_ids: tuple[str, ...]
    scheduled_run_ids: tuple[str, ...]
    rejected: tuple[DiscoveryRejection, ...]
    skipped_outside_window: int
    skipped_outside_universe: int
    skipped_duplicates: int
    skipped_low_confidence: int


@dataclass(frozen=True)
class DiscoveryOutcome:
    summary: DiscoverySummary
    raw_payload: bytes


class EventDiscoveryService:
    """Turn provider facts into policy-checked jobs without any model discretion."""

    def __init__(
        self,
        provider: EventProvider,
        scheduler: Scheduler,
        config: DiscoveryConfig,
        allowed_symbols: frozenset[str],
    ) -> None:
        self.provider = provider
        self.scheduler = scheduler
        self.config = config
        self.allowed_symbols = allowed_symbols

    def discover_and_schedule(self, run_id: str, *, now: datetime) -> DiscoveryOutcome:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("discovery time must be timezone-aware")
        start = now.astimezone(UTC)
        end = start + timedelta(days=self.config.lookahead_days)
        batch = self.provider.discover(
            symbols=self.allowed_symbols,
            window_start=start,
            window_end=end,
            retrieved_at=start,
        )

        registered_event_ids: list[str] = []
        rescheduled_event_ids: list[str] = []
        cancelled_scheduled_run_ids: list[str] = []
        scheduled_run_ids: list[str] = []
        rejected: list[DiscoveryRejection] = []
        skipped_low_confidence = 0
        for candidate in batch.events:
            if candidate.confidence < self.config.minimum_confidence:
                skipped_low_confidence += 1
                continue
            reconciliation = self.scheduler.reconcile_market_event(
                candidate,
                observed_at=start,
                created_by_run_id=run_id,
            )
            market_event = reconciliation.event
            registered_event_ids.append(market_event.id)
            if reconciliation.rescheduled:
                rescheduled_event_ids.append(market_event.id)
                cancelled_scheduled_run_ids.extend(
                    reconciliation.cancelled_scheduled_run_ids
                )
            offset = self.config.followup_offsets_minutes[candidate.event_type]
            scheduled_for = candidate.scheduled_at.astimezone(UTC) + timedelta(minutes=offset)
            try:
                scheduled_run = self.scheduler.schedule(
                    ScheduleRequest(
                        market_event_id=market_event.id,
                        scheduled_for=scheduled_for,
                        reason=(
                            f"{candidate.event_type} follow-up for "
                            f"{', '.join(candidate.symbols)} from {candidate.source}"
                        ),
                        payload={
                            "source": candidate.source,
                            "source_event_id": candidate.source_event_id,
                            "confidence": candidate.confidence,
                            "shadow_mode": True,
                        },
                    ),
                    now=start,
                    created_by_run_id=run_id,
                )
            except SchedulingPolicyError as exc:
                rejected.append(
                    DiscoveryRejection(
                        source_event_id=candidate.source_event_id,
                        reason=str(exc),
                    )
                )
            else:
                scheduled_run_ids.append(scheduled_run.id)

        return DiscoveryOutcome(
            summary=DiscoverySummary(
                provider=batch.provider,
                source_reference=batch.source_reference,
                retrieval_mode=batch.retrieval_mode,
                retrieved_at=batch.retrieved_at,
                source_updated_at=batch.source_updated_at,
                window_start=start,
                window_end=end,
                source_content_hash=batch.content_hash,
                candidate_count=len(batch.events),
                registered_event_ids=tuple(registered_event_ids),
                rescheduled_event_ids=tuple(rescheduled_event_ids),
                cancelled_scheduled_run_ids=tuple(cancelled_scheduled_run_ids),
                scheduled_run_ids=tuple(scheduled_run_ids),
                rejected=tuple(rejected),
                skipped_outside_window=batch.skipped_outside_window,
                skipped_outside_universe=batch.skipped_outside_universe,
                skipped_duplicates=batch.skipped_duplicates,
                skipped_low_confidence=skipped_low_confidence,
            ),
            raw_payload=batch.raw_payload,
        )
