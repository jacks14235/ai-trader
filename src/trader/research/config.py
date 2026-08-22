"""Fail-closed policy configuration for bounded shadow research."""

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.research.models import MAX_BATCH_BYTES, MAX_RAW_DOCUMENT_BYTES, ProviderName


class ResearchConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchSelectionConfig(ResearchConfigModel):
    max_fast_candidates: int = Field(ge=1, le=50)
    max_deep_symbols: int = Field(ge=8, le=12)
    max_questions_per_symbol: int = Field(ge=1, le=3)

    @field_validator("*", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("research selection caps must be integers")
        return value

    @model_validator(mode="after")
    def coherent_selection(self) -> "ResearchSelectionConfig":
        if self.max_deep_symbols > self.max_fast_candidates:
            raise ValueError("deep-symbol cap cannot exceed the fast-candidate cap")
        return self


class ResearchCollectionConfig(ResearchConfigModel):
    max_items_per_symbol: int = Field(ge=1, le=100)
    max_total_requests: int = Field(ge=1, le=200)
    max_total_items: int = Field(ge=1, le=1_000)
    max_response_bytes: int = Field(ge=1, le=MAX_RAW_DOCUMENT_BYTES)
    max_total_response_bytes: int = Field(ge=1, le=MAX_BATCH_BYTES)
    max_wall_clock_seconds: int = Field(ge=1, le=600)
    max_retries_per_request: int = Field(ge=0, le=3)

    @field_validator("*", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("research collection caps must be integers")
        return value

    @model_validator(mode="after")
    def coherent_collection(self) -> "ResearchCollectionConfig":
        if self.max_response_bytes > self.max_total_response_bytes:
            raise ValueError("per-response bytes cannot exceed the total response-byte cap")
        if self.max_items_per_symbol > self.max_total_items:
            raise ValueError("per-symbol item cap cannot exceed the total item cap")
        return self


class ResearchFreshnessConfig(ResearchConfigModel):
    market_context_hours: int = Field(ge=1, le=72)
    company_news_days: int = Field(ge=1, le=30)
    sec_filings_days: int = Field(ge=1, le=365)

    @field_validator("*", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("research freshness limits must be integers")
        return value


class PaidResearchConfig(ResearchConfigModel):
    enabled: Literal[False]
    max_per_request_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    max_per_run_usd: Decimal = Field(ge=0, allow_inf_nan=False)

    @field_validator("enabled", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("paid research enabled must be a boolean")
        return value

    @field_validator("max_per_request_usd", "max_per_run_usd")
    @classmethod
    def finite_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("paid research limits must be finite")
        return value

    @model_validator(mode="after")
    def disabled_means_zero_spend(self) -> "PaidResearchConfig":
        if self.max_per_request_usd != 0 or self.max_per_run_usd != 0:
            raise ValueError("initial shadow research must have zero paid-provider spend")
        return self


class ResearchConfig(ResearchConfigModel):
    """Complete human-owned policy for the initial research pipeline."""

    enabled: Literal[True]
    mode: Literal["shadow"]
    admitted_providers: tuple[ProviderName, ...]
    selection: ResearchSelectionConfig
    collection: ResearchCollectionConfig
    freshness: ResearchFreshnessConfig
    paid: PaidResearchConfig

    @field_validator("enabled", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("research enabled must be a boolean")
        return value

    @field_validator("admitted_providers")
    @classmethod
    def exact_initial_providers(
        cls, values: tuple[ProviderName, ...]
    ) -> tuple[ProviderName, ...]:
        if len(values) != len(set(values)):
            raise ValueError("admitted research providers cannot contain duplicates")
        if set(values) != {"alpaca", "sec"}:
            raise ValueError("initial shadow research admits exactly Alpaca and SEC")
        return values

    @model_validator(mode="after")
    def enough_run_capacity_for_plan(self) -> "ResearchConfig":
        additional_request_count = 0
        if self.selection.max_questions_per_symbol >= 2:
            additional_request_count += 1
        if self.selection.max_questions_per_symbol == 3:
            sec_attempts = self.collection.max_retries_per_request + 1
            additional_request_count += 2 * sec_attempts
        largest_plan = (2 * self.selection.max_fast_candidates) + (
            self.selection.max_deep_symbols * additional_request_count
        )
        setup_attempts = self.collection.max_retries_per_request + 1
        if self.collection.max_total_requests < largest_plan + setup_attempts:
            raise ValueError(
                "max_total_requests cannot hold the configured research plan and setup"
            )
        if self.collection.max_total_items < self.selection.max_fast_candidates:
            raise ValueError("max_total_items must allow at least one item per fast candidate")
        return self


# Policy is a human-owned config model; both names are supported at integration boundaries.
ResearchPolicy = ResearchConfig


def load_research_config(path: Path) -> ResearchConfig:
    """Load every required research-policy field or fail without defaults."""

    try:
        with path.open(encoding="utf-8") as stream:
            content = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load configuration {path}: {exc}") from exc
    return ResearchConfig.model_validate(content)


load_research_policy = load_research_config
