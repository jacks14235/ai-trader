"""Fail-closed configuration for durable dynamic paper runs."""

from pathlib import Path
from typing import Annotated, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import EventType


class FileDiscoverySourceConfig(BaseModel):
    """Strict local-file source retained for fixtures and manual shadow tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["file"]
    feed_path: Path

    @field_validator("feed_path")
    @classmethod
    def nonempty_feed_path(cls, value: Path) -> Path:
        if not str(value).strip():
            raise ValueError("feed_path cannot be empty")
        return value


class BeaDiscoverySourceConfig(BaseModel):
    """Human-owned safety limits for the fixed official BEA calendar endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["bea"]
    cache_path: Path
    included_release_names: tuple[str, ...]
    timeout_seconds: int
    max_cache_age_minutes: int
    max_fetch_attempts: int

    @field_validator(
        "timeout_seconds",
        "max_cache_age_minutes",
        "max_fetch_attempts",
        mode="before",
    )
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("BEA source limits must be integers")
        return value

    @field_validator("cache_path")
    @classmethod
    def nonempty_cache_path(cls, value: Path) -> Path:
        if not str(value).strip():
            raise ValueError("cache_path cannot be empty")
        return value

    @field_validator("included_release_names")
    @classmethod
    def valid_release_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if not normalized or any(not value for value in normalized):
            raise ValueError("at least one nonblank BEA release name is required")
        if len(normalized) != len(set(normalized)):
            raise ValueError("BEA release names cannot contain duplicates")
        if len(normalized) > 20:
            raise ValueError("at most 20 BEA release names may be configured")
        return normalized

    @model_validator(mode="after")
    def conservative_bounds(self) -> "BeaDiscoverySourceConfig":
        if not 1 <= self.timeout_seconds <= 30:
            raise ValueError("BEA timeout_seconds must be between 1 and 30")
        if not 5 <= self.max_cache_age_minutes <= 1440:
            raise ValueError("BEA max_cache_age_minutes must be between 5 and 1440")
        if not 1 <= self.max_fetch_attempts <= 3:
            raise ValueError("BEA max_fetch_attempts must be between 1 and 3")
        return self


DiscoverySourceConfig = Annotated[
    FileDiscoverySourceConfig | BeaDiscoverySourceConfig,
    Field(discriminator="provider"),
]


class DiscoveryConfig(BaseModel):
    """Vendor-neutral discovery input and deterministic follow-up timing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    source: DiscoverySourceConfig
    lookahead_days: int
    minimum_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    followup_offsets_minutes: dict[EventType, int]

    @field_validator("enabled", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("discovery enabled must be a boolean")
        return value

    @field_validator("lookahead_days", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("lookahead_days must be an integer")
        return value

    @field_validator("followup_offsets_minutes", mode="before")
    @classmethod
    def actual_offset_integers(cls, value: object) -> object:
        if isinstance(value, dict) and any(type(item) is not int for item in value.values()):
            raise ValueError("follow-up offsets must be integers")
        return value

    @model_validator(mode="after")
    def conservative_bounds(self) -> "DiscoveryConfig":
        if not 1 <= self.lookahead_days <= 7:
            raise ValueError("lookahead_days must be between 1 and 7")
        if any(not 5 <= offset <= 360 for offset in self.followup_offsets_minutes.values()):
            raise ValueError("follow-up offsets must be between 5 and 360 minutes")
        return self


class DynamicRunsConfig(BaseModel):
    """Human-owned limits that an agent cannot relax at runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    max_extra_runs_per_day: int
    max_extra_runs_per_symbol_per_day: int
    minimum_spacing_minutes: int
    max_schedule_horizon_days: int
    max_event_followup_delay_minutes: int
    max_lateness_minutes: int
    lease_minutes: int
    max_attempts: int
    max_claims_per_tick: int
    allowed_event_types: tuple[EventType, ...]
    discovery: DiscoveryConfig

    @field_validator("enabled", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("enabled must be a boolean")
        return value

    @field_validator(
        "max_extra_runs_per_day",
        "max_extra_runs_per_symbol_per_day",
        "minimum_spacing_minutes",
        "max_schedule_horizon_days",
        "max_event_followup_delay_minutes",
        "max_lateness_minutes",
        "lease_minutes",
        "max_attempts",
        "max_claims_per_tick",
        mode="before",
    )
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("dynamic-run limits must be integers")
        return value

    @field_validator("allowed_event_types")
    @classmethod
    def unique_event_types(cls, values: tuple[EventType, ...]) -> tuple[EventType, ...]:
        if not values:
            raise ValueError("at least one event type must be allowed")
        if len(values) != len(set(values)):
            raise ValueError("allowed event types cannot contain duplicates")
        return values

    @model_validator(mode="after")
    def conservative_bounds(self) -> "DynamicRunsConfig":
        if not 1 <= self.max_extra_runs_per_day <= 3:
            raise ValueError("max_extra_runs_per_day must be between 1 and 3")
        if not 1 <= self.max_extra_runs_per_symbol_per_day <= 2:
            raise ValueError("max_extra_runs_per_symbol_per_day must be between 1 and 2")
        if self.max_extra_runs_per_symbol_per_day > self.max_extra_runs_per_day:
            raise ValueError("per-symbol run limit cannot exceed the daily run limit")
        if self.minimum_spacing_minutes < 30:
            raise ValueError("minimum_spacing_minutes cannot be less than 30")
        if not 1 <= self.max_schedule_horizon_days <= 30:
            raise ValueError("max_schedule_horizon_days must be between 1 and 30")
        if not 5 <= self.max_event_followup_delay_minutes <= 360:
            raise ValueError("max_event_followup_delay_minutes must be between 5 and 360")
        if not 5 <= self.max_lateness_minutes <= 60:
            raise ValueError("max_lateness_minutes must be between 5 and 60")
        if not 1 <= self.lease_minutes <= 30:
            raise ValueError("lease_minutes must be between 1 and 30")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if not 1 <= self.max_claims_per_tick <= 3:
            raise ValueError("max_claims_per_tick must be between 1 and 3")
        missing_offsets = set(self.allowed_event_types).difference(
            self.discovery.followup_offsets_minutes
        )
        if self.discovery.enabled and missing_offsets:
            raise ValueError(
                "missing discovery follow-up offsets for: "
                + ", ".join(sorted(missing_offsets))
            )
        return self


def load_dynamic_runs_config(path: Path) -> DynamicRunsConfig:
    """Load the complete dynamic-runs file without supplying unsafe defaults."""
    try:
        with path.open(encoding="utf-8") as stream:
            content = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load configuration {path}: {exc}") from exc
    return DynamicRunsConfig.model_validate(content)
