"""Policy-aware promotion from the fast candidate pass into deep research."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from trader.research.collection import ResearchCollection
from trader.universe.models import UniverseAsset

_FUND_MARKER = re.compile(
    r"\b(etf|fund|shares|proshares|direxion|ultrapro|ultra|graniteshares)\b",
    re.IGNORECASE,
)
_LEVERAGE_MARKER = re.compile(
    r"(?:\b(?:inverse|short|bear)\b|\b(?:2x|3x|4x|two times|three times)\b)",
    re.IGNORECASE,
)


class DeepSelectionAssessment(BaseModel):
    """Auditable reason a fast-pass symbol was promoted or withheld."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    selected: bool
    pinned: bool
    price: Decimal | None = None
    average_daily_dollar_volume: Decimal | None = None
    reason_codes: tuple[str, ...]


class DeepSelectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_symbols: tuple[str, ...]
    assessments: tuple[DeepSelectionAssessment, ...]


def select_deep_symbols(
    *,
    ordered_symbols: tuple[str, ...],
    assets: Mapping[str, UniverseAsset],
    fast_collection: ResearchCollection,
    pinned_symbols: frozenset[str],
    max_symbols: int,
    min_price: Decimal,
    min_average_daily_dollar_volume: Decimal,
) -> DeepSelectionResult:
    """Promote only policy-compatible symbols, while never displacing held/event names."""

    if not 1 <= max_symbols <= 12:
        raise ValueError("deep research symbol limit must be between 1 and 12")
    market = _market_observations(fast_collection)
    preliminary: list[DeepSelectionAssessment] = []
    eligible: list[str] = []
    pinned: list[str] = []
    for symbol in ordered_symbols:
        observation = market.get(symbol, (None, None))
        price, dollar_volume = observation
        is_pinned = symbol in pinned_symbols
        reasons: list[str] = []
        asset = assets[symbol]
        if _looks_leveraged_or_inverse(asset):
            reasons.append("PROHIBITED_LEVERAGED_OR_INVERSE_FUND")
        if price is None:
            reasons.append("MISSING_PRICE")
        elif price < min_price:
            reasons.append("BELOW_MIN_PRICE")
        if dollar_volume is None:
            reasons.append("MISSING_DOLLAR_VOLUME")
        elif dollar_volume < min_average_daily_dollar_volume:
            reasons.append("BELOW_MIN_DOLLAR_VOLUME")
        if is_pinned:
            pinned.append(symbol)
            reasons.append("PINNED_FOR_PORTFOLIO_OR_EVENT_RESEARCH")
        elif not reasons:
            eligible.append(symbol)
        preliminary.append(
            DeepSelectionAssessment(
                symbol=symbol,
                selected=False,
                pinned=is_pinned,
                price=price,
                average_daily_dollar_volume=dollar_volume,
                reason_codes=tuple(reasons or ("POLICY_SCREEN_PASSED",)),
            )
        )
    if len(pinned) > max_symbols:
        raise ValueError("deep research cap is smaller than pinned portfolio/event symbols")
    selected = tuple((pinned + eligible)[:max_symbols])
    selected_set = set(selected)
    assessments = tuple(
        item.model_copy(update={"selected": item.symbol in selected_set})
        for item in preliminary
    )
    return DeepSelectionResult(selected_symbols=selected, assessments=assessments)


def _market_observations(
    collection: ResearchCollection,
) -> dict[str, tuple[Decimal | None, Decimal | None]]:
    snapshots: dict[str, Decimal] = {}
    volumes: dict[str, Decimal] = {}
    for document in collection.documents:
        if document.source_name not in {
            "Alpaca stock snapshot",
            "Alpaca adjusted daily bars",
        }:
            continue
        try:
            payload = json.loads(document.raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        for symbol in document.symbols:
            raw = payload.get(symbol)
            if document.source_name == "Alpaca stock snapshot":
                price = _snapshot_price(raw)
                if price is not None:
                    snapshots[symbol] = price
                snapshot_volume = _snapshot_dollar_volume(raw)
                if snapshot_volume is not None:
                    volumes[symbol] = snapshot_volume
            else:
                volume = _bars_dollar_volume(raw)
                if volume is not None:
                    volumes[symbol] = volume
    return {
        symbol: (snapshots.get(symbol), volumes.get(symbol))
        for symbol in set(snapshots).union(volumes)
    }


def _snapshot_price(value: object) -> Decimal | None:
    if not isinstance(value, Mapping):
        return None
    for container, field in (
        ("latestTrade", "p"),
        ("minuteBar", "c"),
        ("dailyBar", "c"),
        ("prevDailyBar", "c"),
    ):
        nested = value.get(container)
        if isinstance(nested, Mapping):
            parsed = _positive_decimal(nested.get(field))
            if parsed is not None:
                return parsed
    quote = value.get("latestQuote")
    if isinstance(quote, Mapping):
        bid = _positive_decimal(quote.get("bp"))
        ask = _positive_decimal(quote.get("ap"))
        if bid is not None and ask is not None and ask >= bid:
            return (bid + ask) / Decimal("2")
    return None


def _snapshot_dollar_volume(value: object) -> Decimal | None:
    if not isinstance(value, Mapping):
        return None
    observations: list[Decimal] = []
    for container in ("dailyBar", "prevDailyBar"):
        bar = value.get(container)
        amount = _bar_dollar_volume(bar)
        if amount is not None:
            observations.append(amount)
    return _mean(observations)


def _bars_dollar_volume(value: object) -> Decimal | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    return _mean(
        [amount for bar in value if (amount := _bar_dollar_volume(bar)) is not None]
    )


def _bar_dollar_volume(value: object) -> Decimal | None:
    if not isinstance(value, Mapping):
        return None
    close = _positive_decimal(value.get("c"))
    volume = _positive_decimal(value.get("v"))
    if close is None or volume is None:
        return None
    return close * volume


def _positive_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _looks_leveraged_or_inverse(asset: UniverseAsset) -> bool:
    name = (asset.name or "").strip()
    return bool(name and _FUND_MARKER.search(name) and _LEVERAGE_MARKER.search(name))
