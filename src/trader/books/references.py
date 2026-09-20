"""Deterministic reference curves for simulated books.

References are observations, not books: they invoke no model, hold no strategy, and can never
reach a broker. Each book gets a flat cash curve and a one-time SPY purchase held without
rebalancing, both beginning with that book's own starting cash.
"""

import hashlib
import json
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trader.books.models import FillAssumptions
from trader.broker.models import Quote
from trader.persistence.models import Book, BookEvaluation, BookReferencePoint
from trader.persistence.repositories import PersistenceConflictError

CENT = Decimal("0.01")
QTY_QUANTUM = Decimal("0.000001")
BASIS_POINTS = Decimal("10000")


def record_cash_reference(
    session: Session,
    *,
    book: Book,
    evaluation: BookEvaluation,
    run_id: str,
    as_of: datetime,
    assumptions: FillAssumptions,
) -> BookReferencePoint:
    definition = _definition("CASH", book, assumptions)
    starting = Decimal(book.starting_cash)
    return _persist(
        session,
        BookReferencePoint(
            book_id=book.id,
            run_id=run_id,
            evaluation_id=evaluation.id,
            kind="CASH",
            symbol=None,
            as_of=as_of,
            status="COMPLETED",
            starting_cash=str(starting),
            equity=str(starting),
            cash=str(starting),
            definition_hash=_hash(definition),
            definition_json=definition,
        ),
    )


def record_spy_reference(
    session: Session,
    *,
    book: Book,
    evaluation: BookEvaluation,
    run_id: str,
    as_of: datetime,
    quote: Quote,
    assumptions: FillAssumptions,
) -> BookReferencePoint:
    definition = _definition("SPY_BUY_HOLD", book, assumptions)
    definition_hash = _hash(definition)
    prior = session.scalar(
        select(BookReferencePoint)
        .join(BookEvaluation, BookEvaluation.id == BookReferencePoint.evaluation_id)
        .where(
            BookReferencePoint.book_id == book.id,
            BookReferencePoint.kind == "SPY_BUY_HOLD",
            BookReferencePoint.status == "COMPLETED",
            BookEvaluation.status == "COMPLETED",
            BookEvaluation.book_id == book.id,
            BookReferencePoint.as_of < as_of,
        )
        .order_by(BookReferencePoint.as_of.desc(), BookReferencePoint.id.desc())
        .limit(1)
    )
    starting = Decimal(book.starting_cash)
    if prior is None:
        entry = _entry_price(quote, assumptions)
        per_share = entry + assumptions.commission_per_share
        quantity = (starting / per_share).quantize(QTY_QUANTUM, rounding=ROUND_DOWN)
        commission = (quantity * assumptions.commission_per_share).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        cash = starting - quantity * entry - commission
        while cash < 0 and quantity > 0:
            quantity -= QTY_QUANTUM
            commission = (quantity * assumptions.commission_per_share).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
            cash = starting - quantity * entry - commission
        if quantity <= 0:
            raise ValueError("SPY reference starting cash cannot purchase a fractional share")
    else:
        # The reference methodology is fixed by its first completed point even if a later book
        # phase changes its own simulator assumptions.
        definition = prior.definition_json
        definition_hash = prior.definition_hash
        if _hash(definition) != definition_hash:
            raise PersistenceConflictError("SPY reference definition hash mismatch")
        if prior.quantity is None or prior.entry_price is None or prior.cash is None:
            raise PersistenceConflictError("SPY reference history is incomplete")
        quantity = Decimal(prior.quantity)
        entry = Decimal(prior.entry_price)
        cash = Decimal(prior.cash)
        commission = Decimal(prior.commission or "0")
    mark = ((quote.bid + quote.ask) / Decimal("2")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    equity = cash + quantity * mark
    return _persist(
        session,
        BookReferencePoint(
            book_id=book.id,
            run_id=run_id,
            evaluation_id=evaluation.id,
            kind="SPY_BUY_HOLD",
            symbol="SPY",
            as_of=as_of,
            status="COMPLETED",
            starting_cash=str(starting),
            equity=str(equity),
            cash=str(cash),
            quantity=str(quantity),
            entry_price=str(entry),
            mark_price=str(mark),
            commission=str(commission),
            quote_bid=str(quote.bid),
            quote_ask=str(quote.ask),
            quote_at=quote.timestamp,
            definition_hash=definition_hash,
            definition_json=definition,
        ),
    )


def record_spy_reference_failure(
    session: Session,
    *,
    book: Book,
    evaluation: BookEvaluation,
    run_id: str,
    as_of: datetime,
    assumptions: FillAssumptions,
    error: str,
) -> BookReferencePoint:
    message = error.strip() or "SPY reference failed"
    definition = _definition("SPY_BUY_HOLD", book, assumptions)
    return _persist(
        session,
        BookReferencePoint(
            book_id=book.id,
            run_id=run_id,
            evaluation_id=evaluation.id,
            kind="SPY_BUY_HOLD",
            symbol="SPY",
            as_of=as_of,
            status="FAILED",
            starting_cash=book.starting_cash,
            definition_hash=_hash(definition),
            definition_json=definition,
            error=message[:4_000],
        ),
    )


def _definition(kind: str, book: Book, assumptions: FillAssumptions) -> str:
    payload: dict[str, object] = {
        "version": 1,
        "kind": kind,
        "starting_cash": book.starting_cash,
        "interest": "none",
    }
    if kind == "SPY_BUY_HOLD":
        payload.update(
            {
                "symbol": "SPY",
                "purchase": "once_at_first_completed_reference",
                "quantity_precision": str(QTY_QUANTUM),
                "mark": "quote_midpoint",
                "rebalance": "never",
                "fill_assumptions": assumptions.model_dump(mode="json"),
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _entry_price(quote: Quote, assumptions: FillAssumptions) -> Decimal:
    midpoint = (quote.bid + quote.ask) / Decimal("2")
    reference = quote.ask if assumptions.cross_the_spread else midpoint
    return (reference * (Decimal("1") + assumptions.slippage_bps / BASIS_POINTS)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _persist(session: Session, point: BookReferencePoint) -> BookReferencePoint:
    existing = session.scalar(
        select(BookReferencePoint).where(
            BookReferencePoint.book_id == point.book_id,
            BookReferencePoint.run_id == point.run_id,
            BookReferencePoint.kind == point.kind,
        )
    )
    if existing is not None:
        _verify_same(existing, point)
        return existing
    session.add(point)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise PersistenceConflictError("book reference point was already recorded") from exc
    return point


def _verify_same(existing: BookReferencePoint, point: BookReferencePoint) -> None:
    fields = (
        "evaluation_id",
        "status",
        "starting_cash",
        "equity",
        "cash",
        "quantity",
        "entry_price",
        "mark_price",
        "definition_hash",
        "definition_json",
        "error",
    )
    if any(getattr(existing, field) != getattr(point, field) for field in fields):
        raise PersistenceConflictError(
            f"book reference conflict for {point.book_id}/{point.run_id}/{point.kind}"
        )
