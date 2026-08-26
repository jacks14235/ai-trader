"""Lifecycle and state derivation for simulated books.

A book keeps no mutable balance. Cash, positions, and realized profit are recomputed by replaying
`simulated_fills` from `starting_cash` every time they are asked for, so a book cannot drift away
from its own audit trail and a replay to any past instant is exact.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.books.models import BookState, FillAssumptions, SimulatedFillResult
from trader.books.simulator import apply_fill
from trader.ledger.strategy import strategy_content_hash
from trader.persistence.models import Book, SimulatedFill
from trader.persistence.repositories import PersistenceConflictError

BOOK_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
MAX_ACTIVE_BOOKS = 8


def normalize_book_name(name: str) -> str:
    """Return a slug safe to use as both a unique key and a workflow step name."""
    normalized = name.strip().lower().replace("_", "-").replace(" ", "-")
    normalized = re.sub(r"-+", "-", normalized)
    if not BOOK_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "book name must be 3-40 lowercase alphanumeric characters or hyphens, "
            f"got {name!r}"
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
) -> Book:
    """Open a new simulated book, refusing a duplicate name or an unbounded roster.

    The roster is capped because every active book costs one model invocation per trading day.
    Making variants cheap is the point; making them free would just move the churn problem from
    the strategy document to the number of books.
    """
    slug = normalize_book_name(name)
    if not starting_cash.is_finite() or starting_cash <= 0:
        raise ValueError("a book must start with finite, positive cash")
    document = read_strategy_document(strategy_document_path)
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
        strategy_document_path=str(strategy_document_path),
        strategy_content_hash=strategy_content_hash(document),
        status="active",
        starting_cash=str(starting_cash),
        description=description,
        created_at=moment,
        updated_at=moment,
    )
    session.add(record)
    session.commit()
    return record


def read_strategy_document(path: Path) -> str:
    """Read a book's strategy document, failing closed on a missing or empty file."""
    if not path.is_file():
        raise ValueError(f"strategy document not found: {path}")
    document = path.read_text(encoding="utf-8").strip()
    if not document:
        raise ValueError(f"strategy document is empty: {path}")
    return document


def sync_strategy_document(
    session: Session,
    book: Book,
    *,
    as_of: datetime,
) -> tuple[str, bool]:
    """Read the book's current document and report whether it changed since the last run.

    A human editing a variant mid-experiment is legitimate, so this does not fail. It returns the
    change so the caller can record it: a book whose document moved no longer has a homogeneous
    track record, and a reviewer needs to know that before trusting its curve.
    """
    document = read_strategy_document(Path(book.strategy_document_path))
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
