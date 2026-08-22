"""Strict inputs and reports for the event scheduler."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trader.risk.config import SYMBOL_PATTERN

EventType = Literal[
    "EARNINGS_RELEASE",
    "EARNINGS_CALL",
    "FDA_DECISION",
    "INVESTOR_DAY",
    "ECONOMIC_RELEASE",
]


class StrictSchedulingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketEventRequest(StrictSchedulingModel):
    event_type: EventType
    symbols: tuple[str, ...]
    scheduled_at: datetime
    source: str = Field(min_length=1, max_length=100)
    source_event_id: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: dict[str, object]
    announced_at: datetime
    raw: dict[str, object] | None = None

    @field_validator("symbols")
    @classmethod
    def valid_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(symbol.upper().strip() for symbol in values)
        if not normalized:
            raise ValueError("at least one symbol is required")
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols cannot contain duplicates")
        if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized):
            raise ValueError("invalid symbol")
        return normalized

    @field_validator("scheduled_at", "announced_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market-event timestamps must be timezone-aware")
        return value

    @field_validator("source", "source_event_id")
    @classmethod
    def stripped_nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source identifiers cannot be blank")
        return stripped

    @field_validator("evidence")
    @classmethod
    def evidence_is_present(cls, value: dict[str, object]) -> dict[str, object]:
        if not value:
            raise ValueError("source evidence is required")
        return value


class ScheduleRequest(StrictSchedulingModel):
    market_event_id: str = Field(min_length=1)
    scheduled_for: datetime
    reason: str = Field(min_length=1, max_length=1000)
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("scheduled_for")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled time must be timezone-aware")
        return value

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason cannot be blank")
        return stripped


class SchedulerTickReport(StrictSchedulingModel):
    recovered: int = 0
    expired: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    scheduled_run_ids: tuple[str, ...] = ()
