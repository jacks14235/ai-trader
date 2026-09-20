"""Fail-closed contracts for simulated-book composition and research memos."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from trader.agent.catalog import (
    MAX_PROFILE_STEPS,
    PipelineCatalog,
    ProcessProfile,
    ProfileStep,
    load_pipeline_catalog,
)
from trader.agent.config import AgentConfig, load_agent_config
from trader.agent.invocation import WorkflowStep, canonical_json
from trader.agent.packets import (
    CitedClaim,
    NamedPacket,
    ResearchPacket,
    SymbolPacket,
    packet_content_hash,
    validate_research_packet,
)
from trader.agent.prompts import MAX_OPERATING_NOTE_CHARS, compose_prompt

PROJECT_ROOT = Path(__file__).parents[2]
EVIDENCE_ID = "a" * 64
OTHER_ID = "b" * 64


@pytest.fixture
def config() -> AgentConfig:
    return load_agent_config(PROJECT_ROOT / "config/agents.yaml")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts").mkdir()
    for prompt in (PROJECT_ROOT / "prompts").glob("*.md"):
        (tmp_path / "prompts" / prompt.name).write_bytes(prompt.read_bytes())
    for name in ("agents.yaml", "pipelines.yaml"):
        (tmp_path / "config" / name).write_bytes((PROJECT_ROOT / "config" / name).read_bytes())
    return tmp_path


def _step(name: str = "decide", *, packet: bool = False, **kwargs: Any) -> dict[str, Any]:
    return {
        "step": name,
        "role": "research_compactor" if packet else "daily_trader",
        "output": "research_packet" if packet else "daily_decision",
        **kwargs,
    }


def _catalog(steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "default_profile": "single_pass",
        "profiles": {
            "single_pass": {"description": "One bounded process.", "steps": steps or [_step()]}
        },
    }


def _write_catalog(project: Path, content: dict[str, Any]) -> Path:
    path = project / "config/pipelines.yaml"
    path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return path


def _claim(claim_id: str = "aapl_fact", **kwargs: Any) -> CitedClaim:
    return CitedClaim.model_validate(
        {
            "claim_id": claim_id,
            "text": "Source reports revenue of €20m.",
            "evidence_ids": [EVIDENCE_ID],
            **kwargs,
        }
    )


def _packet() -> ResearchPacket:
    return ResearchPacket(
        symbols=(SymbolPacket(symbol="AAPL", facts=(_claim(),)),),
        limitations=("Only retained excerpts were reviewed.",),
    )


def test_shipped_catalog_is_bounded_and_daily_configuration_is_unchanged(
    config: AgentConfig,
) -> None:
    catalog = load_pipeline_catalog(PROJECT_ROOT / "config/pipelines.yaml", config)
    assert catalog.version == 1
    assert catalog.default_profile == "single_pass"
    assert len(catalog.profiles["single_pass"].steps) == 1
    rich = catalog.profiles["research_then_adversary"]
    assert [step.step for step in rich.steps] == ["packet", "adversary", "decide"]
    assert rich.steps[-1].consumes == ("packet", "adversary")
    for step in rich.steps:
        WorkflowStep(role=step.role, step=step.step)
    assert "research_packets" in config.roles["research_compactor"].context_sources
    assert "research_packets" not in config.roles["daily_trader"].context_sources


@pytest.mark.parametrize("name", ["", "Upper", "foo__bar", "foo_", "../foo", "x" * 65])
def test_profile_and_step_identifiers_are_bounded(name: str) -> None:
    with pytest.raises(ValueError):
        ProfileStep.model_validate(_step(name))
    content = _catalog()
    content["profiles"][name] = content["profiles"].pop("single_pass")
    content["default_profile"] = name
    with pytest.raises(ValueError):
        PipelineCatalog.model_validate(content)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_versions_require_exact_supported_integer(version: object) -> None:
    with pytest.raises(ValueError):
        PipelineCatalog.model_validate({**_catalog(), "version": version})
    with pytest.raises(ValueError):
        ResearchPacket.model_validate({"schema_version": version})


@pytest.mark.parametrize(
    "change",
    [
        {"role": "event_trader"},
        {"role": "weekly_strategist"},
        {"role": "research_compactor"},
        {"output": "research_packet"},
        {"output": "trade"},
        {"can_submit_orders": True},
        {"consumes": ["packet", "packet"]},
    ],
)
def test_profile_step_rejects_wrong_roles_outputs_and_extra_authority(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        ProfileStep.model_validate(_step(**change))


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [_step("packet", packet=True)],
        [_step(), _step("packet", packet=True)],
        [_step(), _step("another_decision")],
        [_step("packet", packet=True), _step("packet")],
        [_step(consumes=["missing"])],
        [_step(consumes=["decide"])],
        [_step("packet", packet=True, consumes=["decide"]), _step()],
        [*[_step(f"packet_{n}", packet=True) for n in range(MAX_PROFILE_STEPS)], _step()],
    ],
)
def test_sequence_rejects_invalid_dependencies_terminals_and_cost(
    steps: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError):
        ProcessProfile.model_validate({"description": "Test", "steps": steps})


def test_five_steps_are_allowed_and_default_must_exist() -> None:
    steps = [_step(f"packet_{n}", packet=True) for n in range(MAX_PROFILE_STEPS - 1)]
    steps.append(_step(consumes=[step["step"] for step in steps]))
    assert len(PipelineCatalog.model_validate(_catalog(steps)).profiles["single_pass"].steps) == 5
    with pytest.raises(ValueError, match="default_profile"):
        PipelineCatalog.model_validate({**_catalog(), "default_profile": "missing"})
    with pytest.raises(ValueError):
        PipelineCatalog.model_validate({**_catalog(), "profiles": {}})
    with pytest.raises(ValueError):
        PipelineCatalog.model_validate({**_catalog(), "allow_live": True})


@pytest.mark.parametrize(
    ("role", "source"),
    [("research_compactor", "account_snapshot"), ("daily_trader", "weekly_performance")],
)
def test_catalog_rejects_sources_its_projectors_cannot_supply(
    config: AgentConfig, role: str, source: str
) -> None:
    content = config.model_dump()
    content["roles"][role]["context_sources"] += (source,)
    invalid = AgentConfig.model_validate(content)
    with pytest.raises(ValueError, match="cannot supply declared sources"):
        load_pipeline_catalog(PROJECT_ROOT / "config/pipelines.yaml", invalid)


@pytest.mark.parametrize("prompt", ["../outside.md", "README.md", "prompts/missing.md", "prompts"])
def test_prompts_must_be_files_under_project_prompts(
    project: Path, config: AgentConfig, prompt: str
) -> None:
    (project / "README.md").write_text("Readable, but not a prompt.")
    path = _write_catalog(project, _catalog([_step(prompt=prompt)]))
    with pytest.raises(ValueError, match="inside project/prompts"):
        load_pipeline_catalog(path, config)


def test_absolute_override_is_rejected_even_inside_project(
    project: Path, config: AgentConfig
) -> None:
    path = _write_catalog(
        project, _catalog([_step(prompt=str(project / "prompts/daily_trader.md"))])
    )
    with pytest.raises(ValueError, match="inside project/prompts"):
        load_pipeline_catalog(path, config)


def test_effective_role_prompt_fallback_is_checked(project: Path, config: AgentConfig) -> None:
    catalog = yaml.safe_load((project / "config/pipelines.yaml").read_text())
    catalog["profiles"]["single_pass"]["steps"][0].pop("prompt", None)
    (project / "config/pipelines.yaml").write_text(yaml.safe_dump(catalog))
    content = config.model_dump()
    content["roles"]["daily_trader"]["prompt"] = "missing.md"
    with pytest.raises(ValueError, match="inside project/prompts"):
        load_pipeline_catalog(
            project / "config/pipelines.yaml", AgentConfig.model_validate(content)
        )


@pytest.mark.parametrize("outside_project", [False, True])
def test_symlink_escape_is_rejected(
    project: Path, config: AgentConfig, outside_project: bool
) -> None:
    target = (project.parent if outside_project else project) / "outside_prompt.md"
    target.write_text("Escaped prompt.")
    (project / "prompts/escape.md").symlink_to(target)
    path = _write_catalog(project, _catalog([_step(prompt="prompts/escape.md")]))
    with pytest.raises(ValueError, match="inside project/prompts"):
        load_pipeline_catalog(path, config)


def test_in_directory_symlink_and_explicit_project_root_work(
    project: Path, config: AgentConfig
) -> None:
    (project / "prompts/alias.md").symlink_to(project / "prompts/daily_trader.md")
    path = project / "elsewhere.yaml"
    path.write_text(yaml.safe_dump(_catalog([_step(prompt="prompts/alias.md")])))
    assert (
        load_pipeline_catalog(path, config, project_root=project).default_profile == "single_pass"
    )


@pytest.mark.parametrize(
    "duplicate",
    [
        "version: 1\nversion: 1\n",
        "profiles:\n  desk: {}\n  desk: {}\n",
        "profiles:\n  desk:\n    steps:\n      - role: daily_trader\n        role: daily_trader\n",
        "base: &base {step: packet}\ncopy: {<<: *base, step: decide}\n",
    ],
)
def test_duplicate_yaml_keys_fail_closed_at_any_depth(
    project: Path, config: AgentConfig, duplicate: str
) -> None:
    path = project / "config/pipelines.yaml"
    path.write_text(duplicate)
    with pytest.raises(ValueError, match="duplicate YAML mapping key"):
        load_pipeline_catalog(path, config)
    with pytest.raises(ValueError, match="duplicate YAML mapping key"):
        load_agent_config(path)


def test_yaml_is_safe_and_load_errors_fail_closed(project: Path, config: AgentConfig) -> None:
    path = project / "config/pipelines.yaml"
    path.write_text("!!python/object/apply:builtins.print ['must not execute']")
    with pytest.raises(ValueError, match="cannot load configuration"):
        load_pipeline_catalog(path, config)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="cannot load configuration"):
        load_pipeline_catalog(path, config)
    with pytest.raises(ValueError, match="cannot load configuration"):
        load_pipeline_catalog(project / "missing.yaml", config)


def test_packet_roundtrip_preserves_unicode_and_canonical_hash() -> None:
    packet = _packet()
    expected_hash = hashlib.sha256(canonical_json(packet).encode()).hexdigest()
    assert packet_content_hash(packet) == expected_hash
    envelope = NamedPacket(
        step="packet", invocation_id="producer_1", content_hash=expected_hash, packet=packet
    )
    assert NamedPacket.model_validate_json(envelope.model_dump_json()) == envelope
    assert validate_research_packet(packet, [EVIDENCE_ID], ["AAPL"]) == packet


def test_thin_packets_do_not_manufacture_claims_or_dissent() -> None:
    packet = ResearchPacket(
        symbols=(SymbolPacket(symbol="AAPL", unknowns=("No retained filing excerpt.",)),)
    )
    assert validate_research_packet(packet, [], ["AAPL"]) == packet
    assert validate_research_packet(ResearchPacket(), [], []) == ResearchPacket()


@pytest.mark.parametrize(
    "field", ["facts", "source_claims", "interpretations", "contradictions", "dissent"]
)
def test_each_claim_category_requires_admitted_evidence(field: str) -> None:
    packet = ResearchPacket.model_validate(
        {"symbols": [{"symbol": "AAPL", field: [_claim().model_dump()]}]}
    )
    with pytest.raises(ValueError, match="unadmitted evidence"):
        validate_research_packet(packet, [OTHER_ID], ["AAPL"])
    with pytest.raises(ValueError, match="unsupported symbol"):
        validate_research_packet(packet, [EVIDENCE_ID], ["MSFT"])


@pytest.mark.parametrize(
    "ids", [[], [EVIDENCE_ID, EVIDENCE_ID], ["a" * 63], ["A" * 64], ["z" * 64]]
)
def test_evidence_ids_are_nonempty_unique_and_exact_hex(ids: list[str]) -> None:
    with pytest.raises(ValueError):
        _claim(evidence_ids=ids)


def test_symbol_and_claim_identity_must_be_unique_across_packet() -> None:
    item = _packet().symbols[0]
    with pytest.raises(ValueError, match="symbols must be unique"):
        ResearchPacket(symbols=(item, item))
    other = SymbolPacket(symbol="MSFT", dissent=(_claim(),))
    with pytest.raises(ValueError, match="claim_ids must be unique"):
        ResearchPacket(symbols=(item, other))
    with pytest.raises(ValueError, match="claim_ids must be unique"):
        ResearchPacket(
            symbols=(SymbolPacket(symbol="AAPL", facts=(_claim(),), dissent=(_claim(),)),)
        )


def test_packet_material_challenges_fit_the_terminal_disposition_budget() -> None:
    with pytest.raises(ValueError, match="contradiction/dissent claims"):
        ResearchPacket(
            symbols=(
                SymbolPacket(
                    symbol="AAPL",
                    contradictions=(_claim("one_more_challenge"),),
                    dissent=tuple(_claim(f"dissent_{index}") for index in range(10)),
                ),
            )
        )


@pytest.mark.parametrize("claim_id", ["Bad", "x__y", "x_", "", "x" * 65, "../x"])
def test_claim_ids_are_bounded_local_identifiers(claim_id: str) -> None:
    with pytest.raises(ValueError):
        _claim(claim_id)


@pytest.mark.parametrize("text", [" ", "x" * 2_001, "\x00bad", "bad\x7f", "bad\u202e", "\ud800"])
def test_claim_text_is_bounded_nonblank_and_control_safe(text: str) -> None:
    with pytest.raises(ValueError):
        _claim(text=text)


def test_collection_and_note_lengths_are_bounded() -> None:
    with pytest.raises(ValueError):
        SymbolPacket(symbol="AAPL", facts=tuple(_claim(f"fact_{i}") for i in range(11)))
    with pytest.raises(ValueError):
        SymbolPacket(symbol="AAPL", unknowns=("x" * 1_001,))
    with pytest.raises(ValueError):
        SymbolPacket(symbol="AAPL", unknowns=("unknown",) * 21)
    with pytest.raises(ValueError):
        ResearchPacket(limitations=("unknown",) * 21)
    with pytest.raises(ValueError):
        ResearchPacket(symbols=tuple(SymbolPacket(symbol=f"S{i}") for i in range(51)))
    with pytest.raises(ValueError):
        _claim(evidence_ids=tuple(f"{i:064x}" for i in range(21)))


@pytest.mark.parametrize("symbol", ["aapl", " AAPL", "A" * 16, "AAPL/USDT", "", "1AAPL"])
def test_symbol_format_fails_closed(symbol: str) -> None:
    with pytest.raises(ValueError):
        SymbolPacket(symbol=symbol)


def test_nested_extra_fields_and_mutation_are_rejected() -> None:
    with pytest.raises(ValueError):
        _claim(target_position_pct=10)
    with pytest.raises(ValueError):
        SymbolPacket.model_validate({"symbol": "AAPL", "action": "BUY"})
    with pytest.raises(ValueError):
        ResearchPacket.model_validate({"trade_proposals": []})
    with pytest.raises(ValidationError, match="frozen"):
        _claim().text = "Changed"
    with pytest.raises(ValidationError, match="frozen"):
        _packet().limitations = ()


def test_tampered_packet_hash_is_rejected() -> None:
    packet = _packet()
    envelope = NamedPacket(
        step="packet",
        invocation_id="producer_1",
        content_hash=packet_content_hash(packet),
        packet=packet,
    ).model_dump()
    envelope["packet"]["symbols"][0]["facts"][0]["text"] = "Altered claim."
    with pytest.raises(ValueError, match="content hash mismatch"):
        NamedPacket.model_validate(envelope)


def test_model_copy_cannot_bypass_boundary_revalidation() -> None:
    claim = _claim().model_copy(update={"evidence_ids": ()})
    item = SymbolPacket(symbol="AAPL").model_copy(update={"facts": (claim,)})
    packet = ResearchPacket().model_copy(update={"symbols": (item,)})
    with pytest.raises(ValueError):
        validate_research_packet(packet, [EVIDENCE_ID], ["AAPL"])
    with pytest.raises(ValueError):
        NamedPacket(
            step="packet",
            invocation_id="producer_1",
            content_hash=packet_content_hash(packet),
            packet=packet,
        )


def test_no_note_prompt_has_one_canonical_representation() -> None:
    expected = compose_prompt("# Skeleton", None)
    assert expected == compose_prompt("\n# Skeleton\n", "")
    assert expected == compose_prompt("# Skeleton", " \t\n")
    assert expected.endswith("\nno operating note")
    assert expected.count("# Desk operating note") == 1
    assert compose_prompt("# Skeleton", "  Investigate uncertainty.\n").endswith(
        "\nInvestigate uncertainty."
    )
    assert len(compose_prompt("# Skeleton", "x" * MAX_OPERATING_NOTE_CHARS)) > 8_000


@pytest.mark.parametrize("control", ["\x00", "\x0b", "\x1b", "\x7f", "\x85", "\u202e", "\ud800"])
def test_prompt_controls_cannot_be_hidden_by_stripping(control: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        compose_prompt("# Skeleton", f"{control}Note")


def test_prompt_note_bound_applies_before_normalization() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        compose_prompt("# Skeleton", " " * (MAX_OPERATING_NOTE_CHARS + 1))
    with pytest.raises(ValueError, match="skeleton must not be blank"):
        compose_prompt(" \n", "Note")
