"""Human-owned, bounded processes for simulated research books.

This module describes composition only. It neither enables roles nor supplies execution access.
The executor must still check role enablement and verify its actual context projection.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.agent.config import AgentConfig, ContextSource, load_unique_yaml

MAX_PROFILE_STEPS = 5
MAX_PROFILES = 32
ProfileName = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
]
StepName = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
]

RESEARCH_PROFILE_SOURCES: frozenset[ContextSource] = frozenset(
    {"candidate_overview", "deep_research", "research_packets"}
)
DAILY_PROFILE_SOURCES: frozenset[ContextSource] = frozenset(
    {
        "strategy_current",
        "portfolio_policy",
        "account_snapshot",
        "positions",
        "open_orders",
        "candidate_overview",
        "deep_research",
        "recent_decisions",
        "open_theses",
        "research_packets",
    }
)


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class ProfileStep(CatalogModel):
    role: Literal["research_compactor", "daily_trader"]
    step: StepName
    output: Literal["research_packet", "daily_decision"]
    consumes: tuple[StepName, ...] = Field(default=(), max_length=MAX_PROFILE_STEPS)
    prompt: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def coherent_step(self) -> "ProfileStep":
        expected = "research_compactor" if self.output == "research_packet" else "daily_trader"
        if self.role != expected:
            raise ValueError(f"{self.output} must use {expected}")
        if len(set(self.consumes)) != len(self.consumes):
            raise ValueError("consumes must be unique")
        return self


class ProcessProfile(CatalogModel):
    description: str = Field(min_length=1, max_length=1_000)
    steps: tuple[ProfileStep, ...] = Field(min_length=1, max_length=MAX_PROFILE_STEPS)

    @field_validator("description")
    @classmethod
    def nonblank_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("profile description must not be blank")
        return value

    @model_validator(mode="after")
    def coherent_sequence(self) -> "ProcessProfile":
        if self.steps[-1].output != "daily_decision" or any(
            step.output == "daily_decision" for step in self.steps[:-1]
        ):
            raise ValueError("exactly one daily_decision step is required, and it must be last")
        prior: set[str] = set()
        for step in self.steps:
            if step.step in prior:
                raise ValueError(f"duplicate profile step: {step.step}")
            if not set(step.consumes).issubset(prior):
                raise ValueError(f"step {step.step} may consume prior steps only")
            prior.add(step.step)
        return self


class PipelineCatalog(CatalogModel):
    version: Literal[1] = 1
    default_profile: ProfileName
    profiles: dict[ProfileName, ProcessProfile] = Field(min_length=1, max_length=MAX_PROFILES)

    @field_validator("version", mode="before")
    @classmethod
    def actual_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("catalog version must be an integer")
        return value

    @model_validator(mode="after")
    def known_default(self) -> "PipelineCatalog":
        if self.default_profile not in self.profiles:
            raise ValueError("default_profile must name a defined profile")
        return self


def load_pipeline_catalog(
    path: Path, config: AgentConfig, *, project_root: Path | None = None
) -> PipelineCatalog:
    """Validate composition, context permissions and every effective prompt before use.

    Prompt overrides and configured fallbacks must resolve to files beneath project/prompts.
    A symlink is allowed only when its final target is still in that directory. Runtime prompt
    readers must repeat containment checks because files may change after this validation.
    """
    catalog = PipelineCatalog.model_validate(load_unique_yaml(path))
    root = (project_root or path.parent.parent).resolve()
    for name, profile in catalog.profiles.items():
        for step in profile.steps:
            role = config.roles[step.role]
            supported = (
                RESEARCH_PROFILE_SOURCES
                if step.output == "research_packet"
                else DAILY_PROFILE_SOURCES
            )
            unsupported = set(role.context_sources) - supported
            if unsupported:
                raise ValueError(
                    f"profile {name} step {step.step} cannot supply declared sources: "
                    + ", ".join(sorted(unsupported))
                )
            prompt = step.prompt if step.prompt is not None else role.prompt
            try:
                resolved = (root / prompt).resolve()
                valid = (
                    not Path(prompt).is_absolute()
                    and resolved.is_relative_to(root / "prompts")
                    and resolved.is_file()
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise ValueError(f"invalid prompt path for {name}/{step.step}: {prompt}") from exc
            if not valid:
                raise ValueError(
                    f"profile {name} step {step.step} prompt must be a file inside "
                    f"project/prompts: {prompt}"
                )
    return catalog
