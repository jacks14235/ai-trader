"""Lifecycle and state derivation for simulated books.

A book keeps no mutable balance. Cash, positions, and realized profit are recomputed by replaying
`simulated_fills` from `starting_cash` every time they are asked for, so a book cannot drift away
from its own audit trail and a replay to any past instant is exact.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.prompts import MAX_OPERATING_NOTE_CHARS
from trader.books.models import BookState, FillAssumptions, SimulatedFillResult
from trader.books.simulator import apply_fill
from trader.ledger.strategy import strategy_content_hash
from trader.persistence.models import Book, SimulatedFill
from trader.persistence.repositories import PersistenceConflictError
from trader.risk.config import load_risk_config

if TYPE_CHECKING:
    from trader.agent.catalog import PipelineCatalog

BOOK_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
MAX_ACTIVE_BOOKS = 8


def normalize_book_name(name: str) -> str:
    """Return a slug safe to use as both a unique key and a workflow step name."""
    normalized = name.strip().lower().replace("_", "-").replace(" ", "-")
    normalized = re.sub(r"-+", "-", normalized)
    if not BOOK_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"book name must be 3-40 lowercase alphanumeric characters or hyphens, got {name!r}"
        )
    return normalized


def open_book(
    session: Session,
    *,
    name: str,
    starting_cash: Decimal,
    strategy_document_path: Path,
    description: str | None = None,
    as_of: datetime | None = None,
    process_profile: str = "single_pass",
    operating_note_path: Path | None = None,
    catalog: PipelineCatalog | None = None,
    project_root: Path | None = None,
    max_starting_cash: Decimal | None = None,
) -> Book:
    """Open a new simulated book, refusing a duplicate name or an unbounded roster.

    The roster is capped because every active book consumes its profile's invocation budget.
    Making variants cheap is the point; making them free would just move the churn problem from
    the strategy document to the number of books.
    """
    slug = normalize_book_name(name)
    if not starting_cash.is_finite() or starting_cash <= 0:
        raise ValueError("a book must start with finite, positive cash")
    root = (project_root or Path.cwd()).resolve()
    ceiling = max_starting_cash
    if ceiling is None:
        ceiling = load_risk_config(root / "config/risk.yaml").portfolio.expected_max_equity_usd
    if not ceiling.is_finite() or ceiling <= 0:
        raise ValueError("maximum book starting cash must be finite and positive")
    if starting_cash > ceiling:
        raise ValueError(f"book starting cash exceeds the human-owned ceiling of {ceiling}")
    if catalog is None:
        from trader.agent.catalog import load_pipeline_catalog
        from trader.agent.config import load_agent_config

        agents = load_agent_config(root / "config/agents.yaml", project_root=root)
        catalog = load_pipeline_catalog(root / "config/pipelines.yaml", agents, project_root=root)
    if process_profile not in catalog.profiles:
        raise ValueError(f"unknown process profile: {process_profile}")
    strategy_path = _contained_file(strategy_document_path, project_root=root)
    document = read_strategy_document(strategy_path, project_root=root)
    note_path = (
        None
        if operating_note_path is None
        else _contained_file(operating_note_path, project_root=root)
    )
    read_operating_note(note_path, project_root=root)
    if session.scalar(select(Book).where(Book.name == slug)) is not None:
        raise PersistenceConflictError(f"book {slug} already exists")
    active = session.scalars(select(Book).where(Book.status == "active")).all()
    if len(active) >= MAX_ACTIVE_BOOKS:
        raise ValueError(
            f"{len(active)} books are already active; retire one before opening another "
            f"(cap is {MAX_ACTIVE_BOOKS})"
        )
    moment = as_of or datetime.now(UTC)
    record = Book(
        name=slug,
        strategy_document_path=str(strategy_path),
        strategy_content_hash=strategy_content_hash(document),
        process_profile=process_profile,
        operating_note_path=None if note_path is None else str(note_path),
        status="active",
        starting_cash=str(starting_cash),
        description=description,
        created_at=moment,
        updated_at=moment,
    )
    session.add(record)
    session.commit()
    return record


def _contained_file(path: Path, *, project_root: Path) -> Path:
    root = project_root.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"document is not a contained project file: {path}")
    return resolved


def read_strategy_document(path: Path, *, project_root: Path | None = None) -> str:
    """Read a book's strategy document, failing closed on a missing or empty file."""
    path = _contained_file(path, project_root=project_root or Path.cwd())
    document = path.read_text(encoding="utf-8").strip()
    if not document:
        raise ValueError(f"strategy document is empty: {path}")
    return document


def read_operating_note(path: Path | None, *, project_root: Path) -> str:
    """Read bounded human-owned instructions; an absent note is the empty string."""
    if path is None:
        return ""
    contained = _contained_file(path, project_root=project_root)
    # Read at most one character past the limit, even for an unexpectedly large file.
    with contained.open(encoding="utf-8") as stream:
        note = stream.read(MAX_OPERATING_NOTE_CHARS + 1)
    if len(note) > MAX_OPERATING_NOTE_CHARS:
        raise ValueError("operating note exceeds 8000 characters")
    if any(unicodedata.category(char).startswith("C") and char not in "\n\r\t" for char in note):
        raise ValueError("operating note cannot contain control characters")
    if not note.strip():
        raise ValueError("operating note is empty")
    return note.strip()


def sync_strategy_document(
    session: Session,
    book: Book,
    *,
    as_of: datetime,
    project_root: Path | None = None,
) -> tuple[str, bool]:
    """Read the book's current document and report whether it changed since the last run.

    A human editing a variant mid-experiment is legitimate, so this does not fail. It returns the
    change so the caller can record it: a book whose document moved no longer has a homogeneous
    track record, and a reviewer needs to know that before trusting its curve.
    """
    root = project_root or Path.cwd()
    document = read_strategy_document(Path(book.strategy_document_path), project_root=root)
    read_operating_note(
        None if book.operating_note_path is None else Path(book.operating_note_path),
        project_root=root,
    )
    content_hash = strategy_content_hash(document)
    changed = content_hash != book.strategy_content_hash
    if changed:
        book.strategy_content_hash = content_hash
        book.updated_at = as_of
        session.add(book)
        session.commit()
    return document, changed


def get_book(session: Session, identifier: str) -> Book:
    """Resolve a book by name or id, failing closed when it does not exist."""
    record = session.scalar(select(Book).where(Book.name == identifier.strip().lower()))
    if record is None:
        record = session.get(Book, identifier)
    if record is None:
        raise ValueError(f"unknown book: {identifier}")
    return record


def list_books(session: Session, *, status: str | None = None) -> tuple[Book, ...]:
    statement = select(Book).order_by(Book.created_at, Book.name)
    if status is not None:
        statement = statement.where(Book.status == status)
    return tuple(session.scalars(statement))


def set_book_status(
    session: Session,
    book: Book,
    status: str,
    *,
    as_of: datetime | None = None,
) -> Book:
    if status not in {"active", "paused", "retired"}:
        raise ValueError(f"unsupported book status: {status}")
    if status == "active" and book.status != "active":
        active = session.scalars(select(Book).where(Book.status == "active")).all()
        if len(active) >= MAX_ACTIVE_BOOKS:
            raise ValueError(
                f"{len(active)} books are already active; retire or pause one first "
                f"(cap is {MAX_ACTIVE_BOOKS})"
            )
    moment = as_of or datetime.now(UTC)
    book.status = status
    book.updated_at = moment
    book.retired_at = moment if status == "retired" else None
    session.add(book)
    session.commit()
    return book


def load_book_state(
    session: Session,
    book: Book,
    *,
    as_of: datetime | None = None,
) -> BookState:
    """Rebuild a book's holdings by replaying its fills in order.

    ``as_of`` cuts the replay off, so a review of a past instant cannot see a fill that had not
    happened yet.
    """
    statement = (
        select(SimulatedFill)
        .where(SimulatedFill.book_id == book.id)
        .order_by(SimulatedFill.transaction_time, SimulatedFill.created_at, SimulatedFill.id)
    )
    if as_of is not None:
        statement = statement.where(SimulatedFill.transaction_time <= as_of)

    state = BookState(
        book_id=book.id,
        name=book.name,
        starting_cash=Decimal(book.starting_cash),
        cash=Decimal(book.starting_cash),
    )
    for record in session.scalars(statement):
        state = apply_fill(state, _replayed(record))
    return state


def persist_simulated_fill(
    session: Session,
    *,
    book: Book,
    run_id: str,
    fill: SimulatedFillResult,
    assumptions: FillAssumptions,
    as_of: datetime,
) -> SimulatedFill:
    """Record one modeled execution, refusing to settle the same proposal twice.

    Re-running a book over a run it has already settled returns the original rows rather than
    compounding the position, which is what makes a book replay safe.
    """
    if not fill.filled:
        raise ValueError("only filled results are persisted; refusals stay in the run summary")
    existing = session.scalar(
        select(SimulatedFill).where(
            SimulatedFill.book_id == book.id,
            SimulatedFill.proposal_id == fill.proposal_id,
        )
    )
    if existing is not None:
        if Decimal(existing.qty) != fill.qty or Decimal(existing.price) != fill.price:
            raise PersistenceConflictError(
                f"simulated fill conflict for book {book.name} proposal {fill.proposal_id}"
            )
        return existing

    record = SimulatedFill(
        book_id=book.id,
        run_id=run_id,
        proposal_id=fill.proposal_id,
        symbol=fill.symbol,
        side=fill.side,
        qty=str(fill.qty),
        price=str(fill.price),
        commission=str(fill.commission),
        quote_bid=None if fill.quote_bid is None else str(fill.quote_bid),
        quote_ask=None if fill.quote_ask is None else str(fill.quote_ask),
        quote_at=fill.quote_at,
        assumptions_json=json.dumps(assumptions.model_dump(mode="json"), sort_keys=True),
        transaction_time=as_of,
    )
    session.add(record)
    session.commit()
    return record


def book_fills(
    session: Session,
    book: Book,
    *,
    as_of: datetime | None = None,
) -> tuple[SimulatedFill, ...]:
    statement = (
        select(SimulatedFill)
        .where(SimulatedFill.book_id == book.id)
        .order_by(SimulatedFill.transaction_time, SimulatedFill.created_at, SimulatedFill.id)
    )
    if as_of is not None:
        statement = statement.where(SimulatedFill.transaction_time <= as_of)
    return tuple(session.scalars(statement))


def _replayed(record: SimulatedFill) -> SimulatedFillResult:
    side = record.side.lower()
    if side not in {"buy", "sell"}:
        raise ValueError(f"stored simulated fill has an unsupported side: {record.side}")
    return SimulatedFillResult(
        proposal_id=record.proposal_id,
        symbol=record.symbol,
        side=side,
        outcome="FILLED",
        qty=Decimal(record.qty),
        price=Decimal(record.price),
        commission=Decimal(record.commission),
    )
