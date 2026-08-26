"""Deterministic settlement of risk-approved orders against a simulated book.

This module is the whole of a book's optimism budget. Every way a simulated result can flatter a
strategy relative to the real world lives here, in one place, under explicit assumptions:

- A fill crosses the spread — buys lift the ask, sells hit the bid — so the book pays the spread
  it would really pay, rather than a mid price nobody trades at.
- A fill is all-or-nothing at a single price. Real partial fills and market impact are not modeled,
  which flatters large orders in thin names.
- An order settles in the same instant it is decided. Overnight gaps between decision and open are
  not modeled.
- A stale or missing quote refuses to fill rather than reusing the last known price.

Nothing here touches a broker, and nothing here decides *whether* an order is allowed: it settles
what the deterministic risk engine already authorized.
"""

from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from trader.books.models import (
    BookPosition,
    BookState,
    FillAssumptions,
    SimulatedFillResult,
)
from trader.broker.models import Quote
from trader.risk.models import RiskDecision

CENT = Decimal("0.01")
BASIS_POINTS = Decimal("10000")


def simulate_fill(
    *,
    decision: RiskDecision,
    quote: Quote | None,
    state: BookState,
    assumptions: FillAssumptions,
    as_of: datetime,
) -> SimulatedFillResult:
    """Settle one approved order against a book, or explain why it did not settle."""
    order = decision.normalized_order
    if not decision.approved or order is None:
        raise ValueError("only approved decisions carrying a normalized order can be simulated")

    side = order.side.lower()
    if side not in {"buy", "sell"}:
        raise ValueError(f"unsupported simulated side: {order.side}")

    symbol = order.symbol.upper().strip()
    base = {
        "proposal_id": decision.proposal_id,
        "symbol": symbol,
        "side": side,
    }

    if quote is None:
        return SimulatedFillResult(
            **base,
            outcome="NO_QUOTE",
            detail="no quote was available to price this order",
        )

    quote_at = _utc(quote.timestamp)
    age = (as_of - quote_at).total_seconds()
    if age > assumptions.max_quote_age_seconds:
        return SimulatedFillResult(
            **base,
            outcome="STALE_QUOTE",
            quote_bid=quote.bid,
            quote_ask=quote.ask,
            quote_at=quote_at,
            detail=(
                f"quote was {int(age)}s old, "
                f"beyond the {assumptions.max_quote_age_seconds}s cap"
            ),
        )

    quote_fields = {"quote_bid": quote.bid, "quote_ask": quote.ask, "quote_at": quote_at}
    slippage = assumptions.slippage_bps / BASIS_POINTS
    mid = ((quote.bid + quote.ask) / Decimal("2")).quantize(CENT, rounding=ROUND_HALF_UP)

    if side == "buy":
        reference = quote.ask if assumptions.cross_the_spread else mid
        price = (reference * (Decimal("1") + slippage)).quantize(CENT, rounding=ROUND_HALF_UP)
        if price > order.limit_price:
            return SimulatedFillResult(
                **base,
                **quote_fields,
                outcome="LIMIT_NOT_MARKETABLE",
                detail=f"modeled buy at {price} exceeds the {order.limit_price} limit",
            )
        commission = _commission(order.qty, assumptions)
        required = order.qty * price + commission
        if required > state.cash:
            return SimulatedFillResult(
                **base,
                **quote_fields,
                outcome="INSUFFICIENT_CASH",
                detail=f"{required} required against {state.cash} of book cash",
            )
        return SimulatedFillResult(
            **base,
            **quote_fields,
            outcome="FILLED",
            qty=order.qty,
            price=price,
            commission=commission,
            detail=f"bought {order.qty} at the {reference} ask",
        )

    reference = quote.bid if assumptions.cross_the_spread else mid
    price = (reference * (Decimal("1") - slippage)).quantize(CENT, rounding=ROUND_DOWN)
    if price < order.limit_price:
        return SimulatedFillResult(
            **base,
            **quote_fields,
            outcome="LIMIT_NOT_MARKETABLE",
            detail=f"modeled sell at {price} is below the {order.limit_price} limit",
        )
    held = state.held().get(symbol)
    if held is None or held.qty < order.qty:
        available = held.qty if held is not None else Decimal("0")
        return SimulatedFillResult(
            **base,
            **quote_fields,
            outcome="INSUFFICIENT_POSITION",
            detail=f"{order.qty} requested against {available} held",
        )
    return SimulatedFillResult(
        **base,
        **quote_fields,
        outcome="FILLED",
        qty=order.qty,
        price=price,
        commission=_commission(order.qty, assumptions),
        detail=f"sold {order.qty} at the {reference} bid",
    )


def apply_fill(state: BookState, fill: SimulatedFillResult) -> BookState:
    """Fold one filled execution into a book, returning the new state.

    Commissions leave cash and are charged against realized profit immediately rather than
    capitalized into the entry price, so `average_entry_price` stays a pure traded price.
    """
    if not fill.filled:
        return state

    held = state.held()
    cash = state.cash + fill.cash_delta()
    realized = state.realized_pnl - fill.commission

    if fill.side == "buy":
        existing = held.get(fill.symbol)
        if existing is None:
            held[fill.symbol] = BookPosition(
                symbol=fill.symbol,
                qty=fill.qty,
                average_entry_price=fill.price,
            )
        else:
            combined_qty = existing.qty + fill.qty
            combined_cost = existing.cost_basis() + fill.qty * fill.price
            held[fill.symbol] = BookPosition(
                symbol=fill.symbol,
                qty=combined_qty,
                average_entry_price=(combined_cost / combined_qty).quantize(
                    Decimal("0.000001"),
                    rounding=ROUND_HALF_UP,
                ),
            )
    else:
        existing = held.get(fill.symbol)
        if existing is None or existing.qty < fill.qty:
            raise ValueError(f"cannot sell {fill.qty} {fill.symbol}: the book does not hold it")
        realized += fill.qty * (fill.price - existing.average_entry_price)
        remaining = existing.qty - fill.qty
        if remaining > 0:
            held[fill.symbol] = BookPosition(
                symbol=fill.symbol,
                qty=remaining,
                average_entry_price=existing.average_entry_price,
            )
        else:
            del held[fill.symbol]

    return BookState(
        book_id=state.book_id,
        name=state.name,
        starting_cash=state.starting_cash,
        cash=cash,
        positions=tuple(held[symbol] for symbol in sorted(held)),
        realized_pnl=realized,
        fill_count=state.fill_count + 1,
    )


def _commission(qty: Decimal, assumptions: FillAssumptions) -> Decimal:
    return (qty * assumptions.commission_per_share).quantize(CENT, rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
