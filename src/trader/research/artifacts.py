"""Immutable, content-verified storage for raw research payloads."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class ResearchArtifactError(RuntimeError):
    """Base error for invalid or conflicting research artifacts."""


class ResearchArtifactConflictError(ResearchArtifactError):
    """Raised when an immutable artifact path already contains different bytes."""


@dataclass(frozen=True)
class ResearchArtifact:
    """Manifest-ready metadata for one immutable file."""

    relative_path: str
    content_hash: str
    byte_count: int
    created: bool


def canonical_json_bytes(value: object) -> bytes:
    """Return a stable UTF-8 JSON representation suitable for hashing and replay."""
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchArtifactError(f"research payload is not canonical JSON: {exc}") from exc
    return encoded.encode("utf-8")


class ImmutableResearchArtifactWriter:
    """Write raw payloads exactly once beneath a single run's research directory.

    Replaying the same bytes at the same relative path is idempotent. Reusing a path
    for different bytes is an explicit conflict; existing research evidence is never
    silently replaced.
    """

    def __init__(self, run_research_dir: Path) -> None:
        self.root = run_research_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ResearchArtifactError(f"research artifact root is not a directory: {self.root}")

    def write_json(self, relative_path: str, value: object) -> ResearchArtifact:
        return self.write_bytes(relative_path, canonical_json_bytes(value))

    def write_bytes(self, relative_path: str, content: bytes) -> ResearchArtifact:
        if not isinstance(content, bytes):
            raise TypeError("research artifact content must be bytes")
        normalized, target = self._target(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(target.parent)
        digest = hashlib.sha256(content).hexdigest()

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError:
            try:
                existing = target.read_bytes()
            except OSError as exc:
                raise ResearchArtifactError(
                    f"cannot verify existing artifact {normalized}: {exc}"
                ) from exc
            existing_digest = hashlib.sha256(existing).hexdigest()
            if existing != content:
                raise ResearchArtifactConflictError(
                    f"immutable research artifact conflict at {normalized}: "
                    f"existing={existing_digest} requested={digest}"
                ) from None
            return ResearchArtifact(
                relative_path=normalized,
                content_hash=digest,
                byte_count=len(content),
                created=False,
            )
        except OSError as exc:
            raise ResearchArtifactError(
                f"cannot create research artifact {normalized}: {exc}"
            ) from exc

        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # A partially written, still-immutable artifact must never be mistaken for
            # valid evidence. Best-effort removal keeps a later retry possible.
            target.unlink(missing_ok=True)
            raise
        return ResearchArtifact(
            relative_path=normalized,
            content_hash=digest,
            byte_count=len(content),
            created=True,
        )

    def _target(self, relative_path: str) -> tuple[str, Path]:
        if not relative_path or "\\" in relative_path or "\0" in relative_path:
            raise ResearchArtifactError("artifact path must be a non-empty POSIX relative path")
        candidate = PurePosixPath(relative_path)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ResearchArtifactError("artifact path must remain below the research directory")
        normalized = candidate.as_posix()
        target = self.root.joinpath(*candidate.parts)
        resolved_parent = target.parent.resolve()
        if not resolved_parent.is_relative_to(self.root):
            raise ResearchArtifactError("artifact path escapes the research directory")
        return normalized, target

    def _reject_symlink_components(self, directory: Path) -> None:
        current = self.root
        for part in directory.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise ResearchArtifactError("artifact directories cannot contain symbolic links")


def _json_default(value: object) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    raise TypeError(f"unsupported canonical JSON value {type(value).__name__}")
