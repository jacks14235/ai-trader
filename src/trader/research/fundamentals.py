"""Deterministic, causal valuation facts derived from retained primary data."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

FORMULA_VERSION = "valuation-facts-v1"

CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": tuple(
        ("us-gaap", name)
        for name in (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
        )
    ),
    "gross_profit": (("us-gaap", "GrossProfit"),),
    "cost_of_revenue": tuple(
        ("us-gaap", name) for name in ("CostOfRevenue", "CostOfGoodsAndServicesSold")
    ),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "net_income": (("us-gaap", "NetIncomeLoss"),),
    "diluted_eps": (("us-gaap", "EarningsPerShareDiluted"),),
    "operating_cash_flow": (("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),),
    "capex": (("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),),
    "cash": (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),),
    "long_term_debt": (("us-gaap", "LongTermDebt"),),
    "current_debt": tuple(("us-gaap", name) for name in ("LongTermDebtCurrent", "DebtCurrent")),
    "equity": (("us-gaap", "StockholdersEquity"),),
    "shares": (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ),
}


class FundamentalsError(ValueError):
    """Raised when an already-validated company-facts payload is structurally unusable."""


@dataclass(frozen=True)
class Fact:
    value: Decimal
    start: date | None
    end: date
    filed: date
    form: str
    concept: str
    unit: str


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FundamentalsError("company fact contains a non-decimal value") from exc
    if not result.is_finite():
        raise FundamentalsError("company fact contains a non-finite value")
    return result


def _facts(payload: Mapping[str, Any], key: str, *, cutoff: date) -> tuple[Fact, ...]:
    for taxonomy, concept in CONCEPTS[key]:
        raw = payload.get("facts", {}).get(taxonomy, {}).get(concept)
        if not isinstance(raw, Mapping):
            continue
        units = raw.get("units")
        if not isinstance(units, Mapping):
            raise FundamentalsError(f"{taxonomy}:{concept} omitted units")
        preferred = "USD/shares" if key == "diluted_eps" else "shares" if key == "shares" else "USD"
        values = units.get(preferred)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        parsed: list[Fact] = []
        for item in values:
            if not isinstance(item, Mapping) or not all(k in item for k in ("val", "end", "filed")):
                raise FundamentalsError(f"{taxonomy}:{concept} contains a malformed fact")
            filed, end = (
                date.fromisoformat(str(item["filed"])),
                date.fromisoformat(str(item["end"])),
            )
            if filed > cutoff:
                continue
            start_value = item.get("start")
            parsed.append(
                Fact(
                    _decimal(item["val"]),
                    date.fromisoformat(str(start_value)) if start_value else None,
                    end,
                    filed,
                    str(item.get("form", "")),
                    f"{taxonomy}:{concept}",
                    preferred,
                )
            )
        if parsed:
            return tuple(parsed)
    return ()


def _dedupe(facts: Sequence[Fact]) -> tuple[Fact, ...]:
    by_period: dict[tuple[date | None, date], Fact] = {}
    for fact in facts:
        key = (fact.start, fact.end)
        if key not in by_period or fact.filed > by_period[key].filed:
            by_period[key] = fact
    return tuple(sorted(by_period.values(), key=lambda fact: (fact.end, fact.filed)))


def _quarters(facts: Sequence[Fact]) -> tuple[Fact, ...]:
    values = list(_dedupe(facts))
    discrete = [f for f in values if f.start and 70 <= (f.end - f.start).days <= 110]
    annuals = [f for f in values if f.start and 330 <= (f.end - f.start).days <= 380]
    ytd = [f for f in values if f.start and 240 <= (f.end - f.start).days <= 300]
    for annual in annuals:
        if any(fact.end == annual.end for fact in discrete):
            continue
        matching = [f for f in ytd if f.start == annual.start and f.end < annual.end]
        if matching:
            nine_month = max(matching, key=lambda fact: fact.end)
            discrete.append(
                Fact(
                    annual.value - nine_month.value,
                    nine_month.end + timedelta(days=1),
                    annual.end,
                    max(annual.filed, nine_month.filed),
                    annual.form,
                    annual.concept,
                    annual.unit,
                )
            )
    return _dedupe(discrete)


def trailing_value(
    payload: Mapping[str, Any], key: str, *, cutoff: date
) -> tuple[Decimal | None, str | None, str]:
    """Return a causal TTM value, selected concept, and explicit availability reason."""
    raw = _facts(payload, key, cutoff=cutoff)
    quarters = [fact for fact in _quarters(raw) if fact.end <= cutoff]
    if len(quarters) < 4:
        return None, raw[0].concept if raw else None, "INSUFFICIENT_CONSECUTIVE_QUARTERS"
    latest = quarters[-4:]
    if any(
        (right.end - left.end).days < 45 or (right.end - left.end).days > 140
        for left, right in zip(latest, latest[1:], strict=False)
    ):
        return None, latest[-1].concept, "INSUFFICIENT_CONSECUTIVE_QUARTERS"
    return sum((fact.value for fact in latest), Decimal("0")), latest[-1].concept, "AVAILABLE"


def _instant(
    payload: Mapping[str, Any], key: str, cutoff: date
) -> tuple[Decimal | None, str | None]:
    facts = [fact for fact in _dedupe(_facts(payload, key, cutoff=cutoff)) if fact.end <= cutoff]
    if not facts:
        return None, None
    selected = facts[-1]
    return selected.value, selected.concept


def _metric(value: Decimal | None, *, reason: str = "MISSING_INPUT") -> dict[str, str]:
    return (
        {"status": "available", "value": str(value)}
        if value is not None
        else {"status": "unavailable", "reason": reason}
    )


def _ratio(
    numerator: Decimal | None, denominator: Decimal | None, *, nonpositive: bool = False
) -> dict[str, str]:
    if numerator is None or denominator is None:
        return _metric(None)
    if denominator == 0 or (nonpositive and denominator < 0):
        return {"status": "not_meaningful", "reason": "NON_POSITIVE_DENOMINATOR"}
    return _metric(numerator / denominator)


def build_valuation_record(
    *,
    symbol: str,
    companyfacts: Mapping[str, Any],
    current_price: Decimal,
    as_of: datetime,
    monthly_bars: Sequence[Mapping[str, Any]],
    input_provenance: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """Build a compact serializable valuation record without opinions or estimates."""
    cutoff = as_of.date()
    values: dict[str, Decimal | None] = {}
    concepts: dict[str, str | None] = {}
    reasons: dict[str, str] = {}
    for key in (
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "diluted_eps",
        "operating_cash_flow",
        "capex",
    ):
        values[key], concepts[key], reasons[key] = trailing_value(companyfacts, key, cutoff=cutoff)
    if values["gross_profit"] is None:
        cost, concept, reason = trailing_value(companyfacts, "cost_of_revenue", cutoff=cutoff)
        if values["revenue"] is not None and cost is not None:
            values["gross_profit"] = values["revenue"] - cost
            concepts["gross_profit"] = f"derived:{concept}"
            reasons["gross_profit"] = "AVAILABLE"
    for key in ("cash", "long_term_debt", "current_debt", "equity", "shares"):
        values[key], concepts[key] = _instant(companyfacts, key, cutoff)
    debt = (
        None
        if values["long_term_debt"] is None and values["current_debt"] is None
        else ((values["long_term_debt"] or Decimal("0")) + (values["current_debt"] or Decimal("0")))
    )
    market_cap = current_price * values["shares"] if values["shares"] is not None else None
    enterprise = (
        market_cap + debt - values["cash"]
        if market_cap is not None and debt is not None and values["cash"] is not None
        else None
    )
    metrics = {
        "price": _metric(current_price),
        "market_cap": _metric(market_cap),
        "enterprise_value": _metric(enterprise),
        "price_to_earnings": _ratio(current_price, values["diluted_eps"], nonpositive=True),
        "price_to_sales": _ratio(market_cap, values["revenue"]),
        "enterprise_value_to_sales": _ratio(enterprise, values["revenue"]),
        "gross_margin": _ratio(values["gross_profit"], values["revenue"]),
        "operating_margin": _ratio(values["operating_income"], values["revenue"]),
        "free_cash_flow_yield": _ratio(
            values["operating_cash_flow"] - values["capex"]
            if values["operating_cash_flow"] is not None and values["capex"] is not None
            else None,
            market_cap,
        ),
    }
    prior_revenue, _, _ = trailing_value(
        companyfacts, "revenue", cutoff=date(cutoff.year - 1, cutoff.month, cutoff.day)
    )
    metrics["ttm_revenue_growth"] = _ratio(
        values["revenue"] - prior_revenue
        if values["revenue"] is not None and prior_revenue is not None
        else None,
        prior_revenue,
    )
    closes = [Decimal(str(bar["c"])) for bar in monthly_bars if "c" in bar]
    price_history: dict[str, Any] = {"status": "unavailable", "reason": "NO_MONTHLY_BARS"}
    if closes:
        ordered = sorted(closes)
        price_history = {
            "status": "available",
            "min": str(ordered[0]),
            "median": str(median(ordered)),
            "max": str(ordered[-1]),
            "current_percentile": str(
                Decimal(sum(v <= current_price for v in closes)) / Decimal(len(closes))
            ),
        }
    return {
        "formula_version": FORMULA_VERSION,
        "derived": True,
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "current_metrics": metrics,
        "historical": {
            "price": price_history,
            "price_to_sales": {"status": "unavailable", "reason": "INSUFFICIENT_CAUSAL_POINTS"},
            "price_to_earnings": {"status": "unavailable", "reason": "INSUFFICIENT_CAUSAL_POINTS"},
        },
        "concepts": concepts,
        "availability_reasons": reasons,
        "input_provenance": list(input_provenance),
    }


def valuation_json(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
