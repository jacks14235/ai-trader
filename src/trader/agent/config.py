"""Strict, human-owned configuration for every reasoning role."""

from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RoleName = Literal[
    "research_compactor",
    "daily_trader",
    "event_trader",
    "weekly_strategist",
]
ContextSource = Literal[
    "strategy_current",
    "portfolio_policy",
    "account_snapshot",
    "positions",
    "open_orders",
    "candidate_overview",
    "deep_research",
    "scheduled_event",
    "recent_decisions",
    "weekly_performance",
]
ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]

REQUIRED_ROLES: frozenset[str] = frozenset(
    {"research_compactor", "daily_trader", "event_trader", "weekly_strategist"}
)


class AgentConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelProfile(AgentConfigModel):
    provider: Literal["codex_cli"]
    model: str | None
    reasoning_effort: ReasoningEffort

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("model override must be a nonblank bounded string")
        return normalized


class RolePermissions(AgentConfigModel):
    filesystem: Literal["read-only"]
    web_search: Literal[False]
    can_submit_orders: Literal[False]
    can_mutate_knowledge: bool

    @field_validator("web_search", "can_submit_orders", "can_mutate_knowledge", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("role permission flags must be booleans")
        return value


class AgentRoleConfig(AgentConfigModel):
    enabled: bool
    profile: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=500)
    context_sources: tuple[ContextSource, ...]
    max_context_chars: int = Field(ge=10_000, le=500_000)
    max_document_chars: int = Field(ge=500, le=20_000)
    max_output_chars: int = Field(ge=1_000, le=100_000)
    timeout_seconds: int = Field(ge=30, le=1_800)
    permissions: RolePermissions

    @field_validator("enabled", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("role enabled must be a boolean")
        return value

    @field_validator(
        "max_context_chars",
        "max_document_chars",
        "max_output_chars",
        "timeout_seconds",
        mode="before",
    )
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("role limits must be integers")
        return value

    @field_validator("context_sources")
    @classmethod
    def unique_context_sources(
        cls, values: tuple[ContextSource, ...]
    ) -> tuple[ContextSource, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("context sources must be nonempty and unique")
        return values


class AgentConfig(AgentConfigModel):
    version: Literal[1]
    mode: Literal["paper_proposal"]
    automatic_daily_run: bool
    model_profiles: dict[str, ModelProfile]
    roles: dict[RoleName, AgentRoleConfig]

    @field_validator("automatic_daily_run", mode="before")
    @classmethod
    def actual_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("automatic_daily_run must be a boolean")
        return value

    @model_validator(mode="after")
    def coherent_roles(self) -> "AgentConfig":
        if set(self.roles) != REQUIRED_ROLES:
            raise ValueError("agents configuration must define every registered role exactly once")
        if not self.model_profiles:
            raise ValueError("at least one model profile is required")
        for role_name, role in self.roles.items():
            if role.profile not in self.model_profiles:
                raise ValueError(f"role {role_name} references an unknown model profile")
            if role_name != "weekly_strategist" and role.permissions.can_mutate_knowledge:
                raise ValueError(f"role {role_name} cannot mutate knowledge")
            if role_name == "daily_trader":
                required = {
                    "strategy_current",
                    "portfolio_policy",
                    "account_snapshot",
                    "positions",
                    "open_orders",
                    "candidate_overview",
                    "deep_research",
                    "recent_decisions",
                }
                if not required.issubset(role.context_sources):
                    raise ValueError("daily_trader is missing a required context source")
        if self.automatic_daily_run and not self.roles["daily_trader"].enabled:
            raise ValueError("automatic daily reasoning requires the daily_trader role")
        return self


def load_agent_config(path: Path, *, project_root: Path | None = None) -> AgentConfig:
    """Load strict role configuration and verify prompt paths stay inside the project."""
    try:
        with path.open(encoding="utf-8") as stream:
            content = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load configuration {path}: {exc}") from exc
    config = AgentConfig.model_validate(content)
    root = (project_root or path.parent.parent).resolve()
    for role_name, role in config.roles.items():
        prompt = (root / role.prompt).resolve()
        if not prompt.is_relative_to(root) or not prompt.is_file():
            raise ValueError(f"role {role_name} prompt is not a project file: {role.prompt}")
    return config


def resolved_prompt_path(config_path: Path, role: AgentRoleConfig) -> Path:
    """Resolve a previously validated role prompt relative to the project root."""
    return (config_path.parent.parent.resolve() / role.prompt).resolve()
