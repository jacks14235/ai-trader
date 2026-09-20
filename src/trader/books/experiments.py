"""Append-only experiment identity and durable claims for simulated book evaluations.

Callers supply the complete effective configuration (strategy, profile, prompts, model settings,
risk and simulator assumptions) separately from changing run inputs. Only configuration changes
start a phase; input provenance is retained on every evaluation. Values must already be JSON
primitives: in particular, monetary values must be strings. These APIs commit the audit boundary.
They neither collect data nor reach a broker, and never retry a conflicting evaluation.
"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from trader.persistence.models import (
    AgentInvocation,
    Book,
    BookEvaluation,
    BookExperimentPhase,
    Run,
)
from trader.persistence.repositories import PersistenceConflictError

MANIFEST_VERSION = 1


def _json_value(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("manifest object keys must be strings")
            _json_value(item)
        return
    raise ValueError(f"manifest values must be JSON primitives, got {type(value).__name__}")


def _manifest(kind: str, content: dict[str, object]) -> str:
    _json_value(content)
    return json.dumps(
        {"version": MANIFEST_VERSION, kind: content},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _hash(manifest: str) -> str:
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def _utc(moment: datetime) -> datetime:
    # SQLite reads DateTime(timezone=True) back as naive UTC.
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def begin_book_evaluation(
    session: Session,
    *,
    book: Book,
    run_id: str,
    as_of: datetime,
    configuration: dict[str, object],
    inputs: dict[str, object],
) -> BookEvaluation:
    """Claim a book/run exactly once and append a phase when configuration changes.

    A repeated configuration only reuses the *last* phase: A -> B -> A has three phases.
    Phase created_at is the transaction's wall-clock time, not historical effective time;
    evaluation as_of supplies the effective cutoff for historical analysis.
    Backdated insertion is refused even if the configuration is unchanged; equal timestamps are
    allowed for explicitly distinct test runs. Duplicate and overlapping claims fail before
    creating a phase. A partial unique index retains exclusive ownership of the book through
    completion or failure; committing the claim does not release that ownership.
    """
    with session.no_autoflush:
        if (
            session.scalar(
                select(BookEvaluation.id).where(
                    BookEvaluation.book_id == book.id, BookEvaluation.run_id == run_id
                )
            )
            is not None
        ):
            raise PersistenceConflictError(f"book {book.name} already evaluated run {run_id}")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("book evaluation as_of must be timezone-aware")
        moment = as_of.astimezone(UTC)
        configuration_manifest = _manifest("configuration", configuration)
        input_manifest = _manifest("inputs", inputs)
        configuration_hash = _hash(configuration_manifest)
        # Serializes phase append on databases supporting row locks. SQLite's transaction and
        # unique constraints fail closed on competing writers; there is no blind retry.
        if session.scalar(select(Book.id).where(Book.id == book.id).with_for_update()) is None:
            raise ValueError("book evaluation requires a persisted book")
        if session.get(Run, run_id) is None:
            raise ValueError("book evaluation requires a persisted run")
        active = session.scalar(
            select(BookEvaluation.id).where(
                BookEvaluation.book_id == book.id, BookEvaluation.status == "STARTED"
            )
        )
        if active is not None:
            raise PersistenceConflictError(
                f"book {book.name} has an active evaluation {active}; refusing overlapping run"
            )
        last_evaluation = session.scalar(
            select(BookEvaluation)
            .where(BookEvaluation.book_id == book.id)
            .order_by(BookEvaluation.as_of.desc())
            .limit(1)
        )
        if last_evaluation is not None and moment < _utc(last_evaluation.as_of):
            raise PersistenceConflictError("cannot insert a backdated book evaluation")
        phase = session.scalar(
            select(BookExperimentPhase)
            .where(BookExperimentPhase.book_id == book.id)
            .order_by(BookExperimentPhase.ordinal.desc())
            .limit(1)
        )
        if phase is not None and _hash(phase.manifest_json) != phase.configuration_hash:
            raise PersistenceConflictError("book experiment phase manifest hash mismatch")
        if phase is None or phase.configuration_hash != configuration_hash:
            phase = BookExperimentPhase(
                book_id=book.id,
                ordinal=1 if phase is None else phase.ordinal + 1,
                configuration_hash=configuration_hash,
                manifest_json=configuration_manifest,
            )
        elif phase.manifest_json != configuration_manifest:
            raise PersistenceConflictError("book experiment configuration hash collision")

    try:
        session.add(phase)
        session.flush()
        evaluation = BookEvaluation(
            book_id=book.id,
            run_id=run_id,
            phase_id=phase.id,
            as_of=moment,
            status="STARTED",
            manifest_json=input_manifest,
        )
        session.add(evaluation)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise PersistenceConflictError("book evaluation or phase was already claimed") from exc
    return evaluation


def finish_book_evaluation(
    session: Session,
    evaluation: BookEvaluation,
    *,
    terminal_invocation_id: str,
) -> BookEvaluation:
    """Complete only with this book's completed daily decision in the evaluation's run."""
    book = session.get(Book, evaluation.book_id)
    if book is None:
        raise ValueError("book evaluation requires a persisted book")
    # Matches the runtime namespace. Stored names are canonical slugs from open_book; keep this
    # derivation independent of the lifecycle service to avoid an import cycle.
    prefix = f"book_{book.id.replace('-', '')}_{book.name.replace('-', '_')}_"
    phase = session.get(BookExperimentPhase, evaluation.phase_id)
    if phase is None:
        raise ValueError("book evaluation requires its experiment phase")
    if _hash(phase.manifest_json) != phase.configuration_hash:
        raise PersistenceConflictError("book experiment phase manifest hash mismatch")
    expected_step = prefix + _terminal_step(phase)
    invocation = session.get(AgentInvocation, terminal_invocation_id)
    if (
        invocation is None
        or invocation.run_id != evaluation.run_id
        or invocation.role != "daily_trader"
        or invocation.status != "COMPLETED"
        or invocation.step != expected_step
    ):
        raise ValueError(
            "terminal invocation must be this book's completed daily_trader in the same run"
        )
    return _resolve(
        session,
        evaluation,
        status="COMPLETED",
        terminal_invocation_id=terminal_invocation_id,
        error=None,
    )


def _terminal_step(phase: BookExperimentPhase) -> str:
    """Read the declared terminal step from the phase's immutable configuration."""
    try:
        manifest = json.loads(phase.manifest_json)
        configuration = manifest["configuration"]
        profile = configuration["profile"]
        steps = profile["steps"]
        terminal = steps[-1]
        name = terminal["step"]
        role = terminal["role"]
        output = terminal["output"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("book experiment phase has no valid terminal profile step") from exc
    if (
        not isinstance(name, str)
        or not name
        or role != "daily_trader"
        or output != "daily_decision"
    ):
        raise ValueError("book experiment phase has no valid terminal profile step")
    return name


def fail_book_evaluation(
    session: Session,
    evaluation: BookEvaluation,
    *,
    error: str,
) -> BookEvaluation:
    """Retain failure under the original phase and run input manifest."""
    if not error.strip():
        raise ValueError("failed book evaluation requires a nonempty error")
    return _resolve(session, evaluation, status="FAILED", terminal_invocation_id=None, error=error)


def mark_interrupted_book_evaluation(
    session: Session,
    *,
    evaluation_id: str,
    reviewer: str,
    note: str,
) -> BookEvaluation:
    """Record an operator's interruption decision only after the parent run has FAILED.

    A STARTED parent or book invocation is not proof of a dead process. Neither is cleared here,
    and there is no force/confirmed-stopped override. An operator must establish and resolve those
    upstream states separately before this API can release the book. COMPLETED parent runs are
    also refused so their immutable run artifacts are not contradicted by this recovery path.
    Any STARTED invocation for this book, including its legacy namespace, blocks recovery.

    The original phase, inputs, invocations, proposals and any settled fills are retained. Only
    the evaluation becomes FAILED, with the reviewer and note recorded in its error. Its unique
    book/run claim remains permanent; subsequent evaluation requires a different run.
    """
    reviewer, note = reviewer.strip(), note.strip()
    if not reviewer or len(reviewer) > 200:
        raise ValueError("interruption reviewer must be nonempty and at most 200 characters")
    if not note or len(note) > 4_000:
        raise ValueError("interruption note must be nonempty and at most 4000 characters")
    with session.no_autoflush:
        evaluation = session.scalar(
            select(BookEvaluation)
            .where(BookEvaluation.id == evaluation_id)
            .execution_options(populate_existing=True)
        )
        if evaluation is None:
            raise LookupError(f"book evaluation not found: {evaluation_id}")
        if evaluation.status != "STARTED":
            raise PersistenceConflictError("book evaluation is already resolved")
        book = session.get(Book, evaluation.book_id)
        if book is None:
            raise ValueError("book evaluation requires a persisted book")
        parent_failed = (
            select(Run.id).where(Run.id == evaluation.run_id, Run.status == "FAILED").exists()
        )
        if not session.scalar(select(parent_failed)):
            raise ValueError("interrupted book evaluation requires a FAILED parent run")
        active_invocations = (
            select(AgentInvocation.id)
            .where(
                AgentInvocation.status == "STARTED",
                or_(
                    AgentInvocation.step.startswith(
                        f"book_{book.id.replace('-', '')}_", autoescape=True
                    ),
                    AgentInvocation.step == f"book_{book.name.replace('-', '_')}",
                ),
            )
            .exists()
        )
        if session.scalar(select(active_invocations)):
            raise ValueError("book still has STARTED agent invocations; refusing recovery")
        reason = json.dumps(
            {
                "type": "OPERATOR_MARKED_INTERRUPTED",
                "reviewer": reviewer,
                "note": note,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        return _resolve(
            session,
            evaluation,
            status="FAILED",
            terminal_invocation_id=None,
            error=reason,
            guards=(parent_failed, ~active_invocations),
        )


def _resolve(
    session: Session,
    evaluation: BookEvaluation,
    *,
    status: str,
    terminal_invocation_id: str | None,
    error: str | None,
    guards: tuple[ColumnElement[bool], ...] = (),
) -> BookEvaluation:
    # Compare-and-set prevents a stale ORM object from overwriting an existing outcome.
    resolved_id = session.scalar(
        update(BookEvaluation)
        .where(BookEvaluation.id == evaluation.id, BookEvaluation.status == "STARTED", *guards)
        .values(status=status, terminal_invocation_id=terminal_invocation_id, error=error)
        .returning(BookEvaluation.id)
        .execution_options(synchronize_session=False)
    )
    if resolved_id is None:
        raise PersistenceConflictError(
            "book evaluation is missing, already resolved, or recovery preconditions changed"
        )
    session.commit()
    session.refresh(evaluation)
    return evaluation
