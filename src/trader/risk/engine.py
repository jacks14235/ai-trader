"""Deterministic, cumulative, fail-closed risk authorization."""

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from trader.agent.models import TradeProposal
from trader.broker.models import Position, Quote, TradableAsset

from .models import NormalizedOrder, RiskContext, RiskDecision

CENT = Decimal("0.01")
SHARE_PRECISION = Decimal("0.000001")
PERCENT = Decimal("100")
BASIS_POINTS = Decimal("10000")


def _valid_decimal(value: Decimal, *, positive: bool = False) -> bool:
    return value.is_finite() and (value > 0 if positive else value >= 0)


def _snapshot_codes(context: RiskContext) -> list[str]:
    codes: list[str] = []
    account = context.account
    account_numbers = (account.equity, account.cash, account.buying_power, account.multiplier)
    if (
        not all(_valid_decimal(value) for value in account_numbers)
        or account.equity <= 0
        or account.multiplier < 1
    ):
        codes.append("INVALID_ACCOUNT_STATE")
    elif account.equity > context.policy.expected_max_equity_usd:
        codes.append("UNEXPECTED_ACCOUNT_EQUITY")
    if account.trading_blocked:
        codes.append("ACCOUNT_TRADING_BLOCKED")
    if account.multiplier > 1:
        codes.append("MARGIN_DISABLED")
    if account.shorting_enabled:
        codes.append("BROKER_SHORTING_ENABLED")
    paper_options_exception = (
        context.paper_options_level_is_provider_managed and account.options_level == 3
    )
    if account.options_level > 0 and not paper_options_exception:
        codes.append("OPTIONS_DISABLED")
    if not context.broker_state_known:
        codes.append("UNKNOWN_BROKER_STATE")
    if not context.portfolio_state_known:
        codes.append("UNKNOWN_PORTFOLIO_STATE")
    if not context.open_order_state_known:
        codes.append("UNKNOWN_OPEN_ORDER_STATE")
    if context.open_orders:
        codes.append("OPEN_ORDER_PENDING")
    if not context.market_is_open:
        codes.append("OUTSIDE_TRADING_WINDOW")
    if context.daily_drawdown_pct >= context.policy.max_daily_drawdown_pct:
        codes.append("DAILY_DRAWDOWN_BREAKER")
    if context.weekly_drawdown_pct >= context.policy.max_weekly_drawdown_pct:
        codes.append("WEEKLY_DRAWDOWN_BREAKER")
    if context.peak_drawdown_pct >= context.policy.max_peak_to_trough_drawdown_pct:
        codes.append("TOTAL_DRAWDOWN_BREAKER")
    return codes


def _positions_snapshot(
    positions: list[Position],
) -> tuple[dict[str, tuple[Decimal, Decimal]], Decimal, bool]:
    snapshot: dict[str, tuple[Decimal, Decimal]] = {}
    invested = Decimal("0")
    valid = True
    for position in positions:
        symbol = position.symbol.upper().strip()
        if (
            not symbol
            or symbol in snapshot
            or not _valid_decimal(position.qty)
            or not _valid_decimal(position.market_value)
            or not _valid_decimal(position.current_price, positive=True)
            or (position.qty == 0) != (position.market_value == 0)
        ):
            valid = False
            continue
        snapshot[symbol] = (position.qty, position.market_value)
        if position.qty > 0:
            invested += position.market_value
    return snapshot, invested, valid


def _quote_codes(quote: Quote, proposal: TradeProposal, context: RiskContext) -> list[str]:
    if quote.symbol.upper().strip() != proposal.symbol:
        return ["INVALID_MARKET_DATA"]
    if (
        not _valid_decimal(quote.bid, positive=True)
        or not _valid_decimal(quote.ask, positive=True)
        or quote.ask < quote.bid
        or quote.timestamp.tzinfo is None
        or quote.timestamp.utcoffset() is None
    ):
        return ["INVALID_MARKET_DATA"]
    age = (context.as_of - quote.timestamp).total_seconds()
    if age < 0:
        return ["INVALID_MARKET_DATA"]

    codes: list[str] = []
    if age > context.policy.market_data_max_age_seconds:
        codes.append("STALE_MARKET_DATA")
    reference = quote.ask if proposal.action == "BUY" else quote.bid
    if reference < context.policy.min_price_usd:
        codes.append("INSUFFICIENT_LIQUIDITY")
    volume = quote.average_daily_dollar_volume
    if (
        volume is None
        or not _valid_decimal(volume, positive=True)
        or volume < context.policy.min_average_daily_dollar_volume_usd
    ):
        codes.append("INSUFFICIENT_LIQUIDITY")
    return codes


def _asset_codes(asset: TradableAsset | None, proposal: TradeProposal) -> list[str]:
    if asset is None:
        return ["UNKNOWN_ASSET_STATE"]
    if asset.symbol.upper().strip() != proposal.symbol:
        return ["INVALID_ASSET_STATE"]

    codes: list[str] = []
    asset_class = asset.asset_class.lower().strip()
    if asset_class == "us_option":
        codes.append("OPTIONS_DISABLED")
    elif asset_class == "crypto":
        codes.append("CRYPTO_DISABLED")
    elif asset_class != "us_equity":
        codes.append("UNSUPPORTED_INSTRUMENT")
    if asset.status.lower().strip() != "active" or not asset.tradable:
        codes.append("ASSET_NOT_TRADABLE")
    return codes


def _requested_notional(
    proposal: TradeProposal,
    equity: Decimal,
    current_position_value: Decimal,
) -> Decimal:
    if proposal.target_notional_usd is not None:
        return proposal.target_notional_usd
    assert proposal.target_position_pct is not None
    target_value = equity * proposal.target_position_pct / PERCENT
    if proposal.action == "BUY":
        return target_value - current_position_value
    return current_position_value - target_value


def _decision(proposal: TradeProposal, codes: list[str]) -> RiskDecision:
    unique = sorted(set(codes))
    return RiskDecision(
        proposal_id=str(proposal.proposal_id),
        approved=False,
        rejection_codes=unique,
        human_explanation=", ".join(unique),
    )


def evaluate(context: RiskContext) -> list[RiskDecision]:
    """Evaluate proposals in order against one cumulative hypothetical portfolio."""
    cash = context.account.cash
    positions, invested, positions_valid = _positions_snapshot(context.positions)
    exposure_today = context.daily_new_gross_exposure_usd
    approved_count = 0
    trade_counts = {
        symbol.upper().strip(): count for symbol, count in context.trades_per_symbol_today.items()
    }
    for symbol in context.symbols_traded_today:
        normalized = symbol.upper().strip()
        trade_counts[normalized] = max(1, trade_counts.get(normalized, 0))
    snapshot_codes = _snapshot_codes(context)
    if not positions_valid:
        snapshot_codes.append("INVALID_PORTFOLIO_STATE")
    decisions: list[RiskDecision] = []

    for proposal in context.proposals:
        if proposal.action == "HOLD":
            decisions.append(
                RiskDecision(
                    proposal_id=str(proposal.proposal_id),
                    approved=False,
                    rejection_codes=["NO_ACTION"],
                    human_explanation="No order requested.",
                )
            )
            continue

        codes = list(snapshot_codes)
        if proposal.symbol not in context.policy.allowed_symbols:
            codes.append("SYMBOL_NOT_ALLOWED")
        if context.orders_today + approved_count >= context.policy.max_orders_per_day:
            codes.append("MAX_ORDER_COUNT")
        if trade_counts.get(proposal.symbol, 0) >= context.policy.max_trades_per_symbol_per_day:
            codes.append("MAX_TRADES_PER_SYMBOL")
        codes.extend(_asset_codes(context.assets.get(proposal.symbol), proposal))

        quote = context.quotes.get(proposal.symbol)
        if quote is None:
            codes.append("UNKNOWN_MARKET_DATA")
        if codes or quote is None:
            decisions.append(_decision(proposal, codes))
            continue

        codes.extend(_quote_codes(quote, proposal, context))
        if codes:
            decisions.append(_decision(proposal, codes))
            continue

        reference = quote.ask if proposal.action == "BUY" else quote.bid
        old_qty, old_value = positions.get(proposal.symbol, (Decimal("0"), Decimal("0")))
        requested = _requested_notional(proposal, context.account.equity, old_value)
        if requested <= 0:
            codes.append("TARGET_ALREADY_MET")
        elif requested < context.policy.min_order_usd:
            codes.append("MIN_ORDER_USD")

        if proposal.action == "BUY":
            cap = proposal.max_acceptable_price
            assert cap is not None
            slippage_cap = reference * (
                Decimal("1") + context.policy.max_limit_slippage_bps / BASIS_POINTS
            )
            if cap > slippage_cap:
                codes.append("LIMIT_PRICE_TOO_AGGRESSIVE")
            limit_price = min(cap, slippage_cap).quantize(CENT, rounding=ROUND_DOWN)
            qty = (
                (requested / limit_price).quantize(SHARE_PRECISION, rounding=ROUND_DOWN)
                if requested > 0
                else Decimal("0")
            )
            notional = qty * limit_price
            new_value = old_value + notional
            if new_value > context.policy.max_single_position_usd:
                codes.append("MAX_POSITION_USD")
            if new_value > (
                context.account.equity * context.policy.max_single_position_pct / PERCENT
            ):
                codes.append("MAX_POSITION_PCT")
            if invested + notional > (
                context.account.equity * context.policy.max_invested_pct / PERCENT
            ):
                codes.append("MAX_INVESTED_PCT")
            if (
                exposure_today + notional
                > context.policy.max_new_gross_exposure_per_day_usd
            ):
                codes.append("MAX_DAILY_EXPOSURE")
            reserve = context.account.equity * context.policy.minimum_cash_reserve_pct / PERCENT
            if cash - notional < reserve:
                codes.append("MIN_CASH_RESERVE")
            if cash < notional:
                codes.append("INSUFFICIENT_CASH")
            if context.account.buying_power < notional:
                codes.append("INSUFFICIENT_BUYING_POWER")
            position_count = sum(qty_held > 0 for qty_held, _ in positions.values())
            if old_qty == 0 and position_count >= context.policy.max_positions:
                codes.append("MAX_POSITIONS")
        else:
            floor = proposal.min_acceptable_price
            assert floor is not None
            slippage_floor = reference * (
                Decimal("1") - context.policy.max_limit_slippage_bps / BASIS_POINTS
            )
            if floor < slippage_floor:
                codes.append("LIMIT_PRICE_TOO_AGGRESSIVE")
            limit_price = max(floor, slippage_floor).quantize(CENT, rounding=ROUND_UP)
            held_qty = old_qty
            qty = min(
                (
                    (requested / limit_price).quantize(SHARE_PRECISION, rounding=ROUND_DOWN)
                    if requested > 0
                    else Decimal("0")
                ),
                held_qty,
            )
            notional = qty * limit_price
            if held_qty <= 0 or qty <= 0:
                codes.append("SHORTING_DISABLED")
            elif held_qty * limit_price < context.policy.min_order_usd:
                codes.append("MIN_ORDER_USD")

        if codes:
            decisions.append(_decision(proposal, codes))
            continue

        order = NormalizedOrder(
            symbol=proposal.symbol,
            side=proposal.action.lower(),
            qty=qty,
            limit_price=limit_price,
            notional=notional,
        )
        decisions.append(
            RiskDecision(
                proposal_id=str(proposal.proposal_id),
                approved=True,
                normalized_order=order,
                human_explanation="Approved by deterministic policy.",
            )
        )
        trade_counts[proposal.symbol] = trade_counts.get(proposal.symbol, 0) + 1
        approved_count += 1
        if proposal.action == "BUY":
            cash -= notional
            exposure_today += notional
            positions[proposal.symbol] = (old_qty + qty, new_value)
            invested += notional
        else:
            remaining_value = max(Decimal("0"), old_value - notional)
            positions[proposal.symbol] = (held_qty - qty, remaining_value)
            cash += notional
            invested = max(Decimal("0"), invested - min(old_value, notional))
    return decisions
