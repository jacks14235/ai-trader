"""Read-only non-interactive Codex CLI boundary for structured reasoning."""

import json
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from trader.agent.config import ModelProfile


@dataclass(frozen=True)
class InvocationResponse:
    response_text: str
    stdout: str
    stderr: str
    input_token_count: int | None = None
    output_token_count: int | None = None
    cached_input_token_count: int | None = None
    reasoning_output_token_count: int | None = None
    cost_usd: Decimal | None = None
    cost_source: str = "NOT_REPORTED"

    @property
    def total_token_count(self) -> int | None:
        if self.input_token_count is None or self.output_token_count is None:
            return None
        return self.input_token_count + self.output_token_count

    def usage_payload(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_token_count,
            "cached_input_tokens": self.cached_input_token_count,
            "output_tokens": self.output_token_count,
            "reasoning_output_tokens": self.reasoning_output_token_count,
            "total_tokens": self.total_token_count,
            "cost_usd": None if self.cost_usd is None else str(self.cost_usd),
            "cost_source": self.cost_source,
        }


class CodexCLIInvocationError(RuntimeError):
    """A failed CLI process with its machine-readable diagnostics retained."""

    def __init__(
        self,
        message: str,
        *,
        stdout: str,
        stderr: str,
        usage: InvocationResponse | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.usage = usage


class StructuredReasoningProvider(Protocol):
    provider_name: str

    def invoke(
        self,
        *,
        prompt: str,
        output_schema: dict[str, object],
        profile: ModelProfile,
        timeout_seconds: int,
        max_output_chars: int,
    ) -> InvocationResponse: ...


class CodexCLIProvider:
    """Invoke Codex without shell interpolation, tools, writes, or persistent sessions."""

    provider_name = "codex_cli"

    def __init__(self, executable: str = "codex") -> None:
        if executable != "codex":
            raise ValueError("the reasoning executable is fixed to codex")
        self.executable = executable

    def invoke(
        self,
        *,
        prompt: str,
        output_schema: dict[str, object],
        profile: ModelProfile,
        timeout_seconds: int,
        max_output_chars: int,
    ) -> InvocationResponse:
        with tempfile.TemporaryDirectory(prefix="trader-codex-") as temporary:
            workdir = Path(temporary)
            schema_path = workdir / "output-schema.json"
            response_path = workdir / "response.json"
            schema_path.write_text(
                json.dumps(output_schema, sort_keys=True), encoding="utf-8"
            )
            command = [
                self.executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(response_path),
                "--color",
                "never",
                "--json",
                "--cd",
                str(workdir),
                "-c",
                'approval_policy="never"',
                "-c",
                'web_search="disabled"',
                "-c",
                "tools.shell=false",
                "-c",
                "tools.web_search=false",
                "-c",
                "agents.enabled=false",
                "-c",
                f'model_reasoning_effort="{profile.reasoning_effort}"',
            ]
            if profile.model is not None:
                command.extend(["--model", profile.model])
            command.append("-")
            try:
                completed = subprocess.run(  # noqa: S603 - fixed executable and argv only
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"Codex CLI invocation failed: {exc}") from exc
            if completed.returncode != 0:
                detail = _failure_detail(completed.stdout, completed.stderr)
                raise CodexCLIInvocationError(
                    f"Codex CLI exited with status {completed.returncode}: {detail}",
                    stdout=completed.stdout[-100_000:],
                    stderr=completed.stderr[-100_000:],
                    usage=_response_usage(completed.stdout),
                )
            try:
                response_text = response_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError("Codex CLI did not write its final response") from exc
            if not response_text.strip():
                raise RuntimeError("Codex CLI returned an empty final response")
            if len(response_text) > max_output_chars:
                raise RuntimeError("Codex CLI response exceeded the configured output limit")
            usage = _response_usage(completed.stdout)
            return InvocationResponse(
                response_text=response_text,
                stdout=completed.stdout[-20_000:],
                stderr=completed.stderr[-20_000:],
                input_token_count=usage.input_token_count,
                output_token_count=usage.output_token_count,
                cached_input_token_count=usage.cached_input_token_count,
                reasoning_output_token_count=usage.reasoning_output_token_count,
                cost_usd=usage.cost_usd,
                cost_source=usage.cost_source,
            )


def codex_output_schema(schema: dict[str, object]) -> dict[str, object]:
    """Normalize a Pydantic schema to Codex's strict structured-output subset."""
    normalized = _normalize_schema(schema)
    if not isinstance(normalized, dict):
        raise TypeError("Codex output schema root must be an object")
    return cast(dict[str, object], normalized)


def _normalize_schema(value: object) -> object:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        normalized = {
            key: _normalize_schema(item)
            for key, item in mapping.items()
            # Pydantic's Decimal regex uses lookaround, which the structured-output
            # engine rejects. Runtime Pydantic validation remains authoritative.
            if key not in {"default", "pattern"}
        }
        properties = normalized.get("properties")
        if isinstance(properties, dict):
            normalized["required"] = list(properties)
            normalized["additionalProperties"] = False
        return normalized
    if isinstance(value, list):
        return [_normalize_schema(item) for item in value]
    return value


def _failure_detail(stdout: str, stderr: str) -> str:
    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        event = cast(dict[str, object], value)
        event_type = event.get("type")
        if event_type == "error" and isinstance(event.get("message"), str):
            messages.append(cast(str, event["message"]))
        if event_type == "turn.failed" and isinstance(event.get("error"), dict):
            error = cast(dict[str, object], event["error"])
            if isinstance(error.get("message"), str):
                messages.append(cast(str, error["message"]))
    unique_messages = list(
        dict.fromkeys(message.strip() for message in messages if message.strip())
    )
    if unique_messages:
        return " | ".join(unique_messages)[-8_000:]
    if stderr.strip():
        return stderr.strip()[-8_000:]
    if stdout.strip():
        return stdout.strip()[-8_000:]
    return "no diagnostic output"


def _response_usage(stdout: str) -> InvocationResponse:
    """Extract the final provider usage object without assuming one event envelope."""
    result = InvocationResponse("", "", "")
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        for candidate in _objects(value):
            input_tokens = _token(candidate.get("input_tokens"))
            output_tokens = _token(candidate.get("output_tokens"))
            if input_tokens is None or output_tokens is None:
                continue
            cached = _token(candidate.get("cached_input_tokens"))
            reasoning = _token(candidate.get("reasoning_output_tokens"))
            if cached is not None and cached > input_tokens:
                continue
            if reasoning is not None and reasoning > output_tokens:
                continue
            cost = _cost(candidate.get("cost_usd"))
            result = InvocationResponse(
                "",
                "",
                "",
                input_token_count=input_tokens,
                output_token_count=output_tokens,
                cached_input_token_count=cached,
                reasoning_output_token_count=reasoning,
                cost_usd=cost,
                cost_source="CLI_REPORTED" if cost is not None else "NOT_REPORTED",
            )
    return result


def _token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _cost(value: object) -> Decimal | None:
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _objects(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        yield mapping
        for nested in mapping.values():
            yield from _objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _objects(nested)
