"""Content-addressed versioning of the human-owned strategy document.

A version is created by observing the bytes a run actually reasoned under, never by a model. That
is what lets a later review say which policy was in effect when a decision was made, and what gives
a proposed edit a concrete predecessor to be measured against.
"""

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.ledger.models import StrategyVersion
from trader.persistence.models import Run, Strategy
from trader.persistence.repositories import PersistenceConflictError

MAX_STRATEGY_CHARS = 200_000


def strategy_content_hash(document: str) -> str:
    return hashlib.sha256(document.strip().encode()).hexdigest()


def record_strategy_version(
    session: Session,
    *,
    document: str,
    as_of: datetime,
    markdown_path: str | None = None,
) -> StrategyVersion:
    """Record the strategy bytes in effect, superseding any earlier active version.

    Recording the same content twice returns the existing version, so a normal run adds nothing.
    Different content supersedes rather than overwrites: history stays reconstructable.
    """
    normalized = document.strip()
    if not normalized:
        raise ValueError("strategy document cannot be empty")
    if len(normalized) > MAX_STRATEGY_CHARS:
        raise ValueError(
            f"strategy document has {len(normalized)} chars; limit is {MAX_STRATEGY_CHARS}"
        )
    content_hash = strategy_content_hash(normalized)
    existing = session.scalar(select(Strategy).where(Strategy.content_hash == content_hash))
    if existing is not None:
        if existing.description != normalized:
            raise PersistenceConflictError(
                f"strategy content hash {content_hash} does not match its recorded text"
            )
        return _version(existing)

    for superseded in session.scalars(select(Strategy).where(Strategy.status == "active")):
        superseded.status = "superseded"
        superseded.updated_at = as_of
    record = Strategy(
        name=f"strategy@{content_hash[:12]}",
        content_hash=content_hash,
        status="active",
        description=normalized,
        created_at=as_of,
        updated_at=as_of,
        markdown_path=markdown_path,
    )
    session.add(record)
    session.commit()
    return _version(record)


def attribute_run_to_strategy(session: Session, *, run_id: str, strategy_id: str) -> None:
    """Bind a run to the strategy version it reasoned under, refusing to rewrite an existing one."""
    run = session.get(Run, run_id)
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    if run.strategy_id is not None:
        if run.strategy_id != strategy_id:
            raise PersistenceConflictError(
                f"run {run_id} is already attributed to strategy {run.strategy_id}"
            )
        return
    run.strategy_id = strategy_id
    session.commit()


def current_strategy_version(session: Session) -> StrategyVersion | None:
    """Return the active strategy version, or None before any run has recorded one."""
    record = session.scalar(
        select(Strategy)
        .where(Strategy.status == "active")
        .order_by(Strategy.created_at.desc(), Strategy.id.desc())
        .limit(1)
    )
    return None if record is None else _version(record)


def strategy_versions_in_effect(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
) -> tuple[StrategyVersion, ...]:
    """Return the versions the runs in a period actually reasoned under, oldest first."""
    strategy_ids = set(
        session.scalars(
            select(Run.strategy_id).where(
                Run.strategy_id.is_not(None),
                Run.scheduled_for >= period_start,
                Run.scheduled_for < period_end,
            )
        )
    )
    if not strategy_ids:
        return ()
    records = session.scalars(
        select(Strategy)
        .where(Strategy.id.in_(sorted(strategy_ids)))
        .order_by(Strategy.created_at, Strategy.id)
    )
    return tuple(_version(record) for record in records)


def _version(record: Strategy) -> StrategyVersion:
    status = "active" if record.status == "active" else "superseded"
    return StrategyVersion(
        strategy_id=record.id,
        name=record.name,
        content_hash=record.content_hash,
        status=status,
        created_at=record.created_at,
        markdown_path=record.markdown_path,
    )
