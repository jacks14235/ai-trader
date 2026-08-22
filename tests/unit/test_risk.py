from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from trader.agent.models import TradeProposal
from trader.broker.models import Account, BrokerOrder, Position, Quote, TradableAsset
from trader.risk.config import load_risk_config, load_risk_policy, load_universe_config
from trader.risk.engine import evaluate
from trader.risk.models import RiskContext, RiskPolicy

PROJECT_ROOT = Path(__file__).parents[2]
RISK_PATH = PROJECT_ROOT / "config" / "risk.yaml"
UNIVERSE_PATH = PROJECT_ROOT / "config" / "universe.yaml"
AS_OF = datetime(2026, 8, 20, 15, 15, tzinfo=UTC)
BASE_POLICY = load_risk_policy(RISK_PATH, UNIVERSE_PATH)


def policy(**updates: object) -> RiskPolicy:
    values = BASE_POLICY.model_dump()
    values.update(updates)
    return RiskPolicy.model_validate(values)


def proposal(
    symbol: str = "SPY",
    amount: str | None = "100",
    action: str = "BUY",
    target_pct: str | None = None,
) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action=action,
        target_notional_usd=Decimal(amount) if amount is not None else None,
        target_position_pct=Decimal(target_pct) if target_pct is not None else None,
        confidence=0.8,
        time_horizon="months",
        rationale="evidence-based",
        max_acceptable_price=Decimal("10.05") if action == "BUY" else None,
        min_acceptable_price=Decimal("9.95") if action == "SELL" else None,
    )


def quote(symbol: str, timestamp: datetime = AS_OF) -> Quote:
    return Quote(
        symbol=symbol,
        bid=Decimal("9.99"),
        ask=Decimal("10"),
        timestamp=timestamp,
        average_daily_dollar_volume=Decimal("6000000"),
    )


def asset(
    symbol: str,
    *,
    asset_class: str = "us_equity",
    status: str = "active",
    tradable: bool = True,
) -> TradableAsset:
    return TradableAsset(
        symbol=symbol,
        asset_class=asset_class,
        status=status,
        tradable=tradable,
        exchange="ARCA",
    )


def context(
    proposals: list[TradeProposal],
    cash: str = "1000",
    positions: list[Position] | None = None,
    **updates: object,
) -> RiskContext:
    values: dict[str, object] = {
        "as_of": AS_OF,
        "account": Account(
            equity=Decimal("2000"),
            cash=Decimal(cash),
            buying_power=Decimal(cash),
        ),
        "positions": positions or [],
        "open_orders": [],
        "quotes": {item.symbol: quote(item.symbol) for item in proposals},
        "assets": {item.symbol: asset(item.symbol) for item in proposals},
        "proposals": proposals,
        "policy": BASE_POLICY,
        "daily_drawdown_pct": Decimal("0"),
        "weekly_drawdown_pct": Decimal("0"),
        "peak_drawdown_pct": Decimal("0"),
        "orders_today": 0,
        "daily_new_gross_exposure_usd": Decimal("0"),
        "trades_per_symbol_today": {},
        "broker_state_known": True,
        "open_order_state_known": True,
        "portfolio_state_known": True,
        "market_is_open": True,
        "paper_options_level_is_provider_managed": False,
    }
    values.update(updates)
    return RiskContext.model_validate(values)


def write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_loads_complete_paper_only_policy() -> None:
    risk = load_risk_config(RISK_PATH)
    universe = load_universe_config(UNIVERSE_PATH)
    loaded = load_risk_policy(RISK_PATH, UNIVERSE_PATH)

    assert risk.mode == "paper"
    assert loaded.allowed_symbols == frozenset({"SPY", "QQQ"})
    assert universe.source == "alpaca"
    assert universe.asset_class == "us_equity"
    assert universe.benchmark_symbols == ("SPY", "QQQ")
    assert universe.event_symbols == ("SPY", "QQQ")
    assert universe.candidate_selection.max_candidates == 50
    assert loaded.market_data_max_age_seconds == 900


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        (("mode",), "live"),
        (("broker_account", "no_shorting"), False),
        (("instruments", "options"), True),
        (("orders", "allow_market_orders"), True),
        (("circuit_breakers", "reject_if_market_data_stale"), False),
        (("portfolio", "expected_max_equity_usd"), -1),
        (("portfolio", "minimum_cash_reserve_pct"), 0),
        (("orders", "max_limit_slippage_bps"), 1001),
        (("instruments", "equities"), "true"),
        (("activity", "max_orders_per_day"), True),
        (("broker_account", "max_margin_multiplier"), True),
    ],
)
def test_risk_config_fails_closed_on_unsafe_values(
    tmp_path: Path, mutation: tuple[str, ...], value: object
) -> None:
    raw = yaml.safe_load(RISK_PATH.read_text(encoding="utf-8"))
    target = raw
    for key in mutation[:-1]:
        target = target[key]
    target[mutation[-1]] = value
    path = tmp_path / "risk.yaml"
    write_yaml(path, raw)

    with pytest.raises(ValidationError):
        load_risk_config(path)


def test_risk_config_rejects_missing_and_unknown_safety_values(tmp_path: Path) -> None:
    raw = yaml.safe_load(RISK_PATH.read_text(encoding="utf-8"))
    del raw["liquidity"]["market_data_max_age_seconds"]
    raw["orders"]["surprise_override"] = True
    path = tmp_path / "risk.yaml"
    write_yaml(path, raw)

    with pytest.raises(ValidationError) as error:
        load_risk_config(path)

    assert "market_data_max_age_seconds" in str(error.value)
    assert "surprise_override" in str(error.value)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        (("source",), "manual"),
        (("require_active",), False),
        (("benchmark_symbols",), ["SPY", "SPY"]),
        (("event_symbols",), ["SPY;DROP"]),
        (("excluded_symbols",), ["SPY"]),
        (("candidate_selection", "max_candidates"), 0),
        (("candidate_selection", "most_active_by_volume"), True),
        (("candidate_selection", "market_movers_per_side"), 51),
    ],
)
def test_universe_rejects_invalid_or_inconsistent_symbols(
    tmp_path: Path, mutation: tuple[str, ...], value: object
) -> None:
    universe = yaml.safe_load(UNIVERSE_PATH.read_text(encoding="utf-8"))
    target = universe
    for key in mutation[:-1]:
        target = target[key]
    target[mutation[-1]] = value
    path = tmp_path / "universe.yaml"
    write_yaml(path, universe)
    with pytest.raises(ValidationError):
        load_universe_config(path)


def test_loader_wraps_missing_or_malformed_yaml(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot load configuration"):
        load_risk_config(tmp_path / "missing.yaml")

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("orders: [", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot load configuration"):
        load_risk_config(malformed)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_notional_usd": Decimal("NaN")},
        {"target_notional_usd": Decimal("Infinity")},
        {"target_notional_usd": Decimal("100"), "target_position_pct": Decimal("5")},
        {"target_notional_usd": None, "target_position_pct": Decimal("101")},
        {"max_acceptable_price": Decimal("NaN")},
        {"confidence": float("nan")},
        {"symbol": "SPY;DROP TABLE"},
    ],
)
def test_proposal_rejects_ambiguous_or_nonfinite_inputs(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "symbol": "SPY",
        "action": "BUY",
        "target_notional_usd": Decimal("100"),
        "confidence": 0.8,
        "time_horizon": "months",
        "rationale": "bounded",
        "max_acceptable_price": Decimal("10.05"),
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(values)


def test_hold_cannot_smuggle_order_fields() -> None:
    with pytest.raises(ValidationError):
        TradeProposal(
            symbol="SPY",
            action="HOLD",
            target_notional_usd=Decimal("100"),
            confidence=0.5,
            time_horizon="days",
            rationale="wait",
        )


def test_approves_bounded_limit_buy() -> None:
    decision = evaluate(context([proposal()]))[0]
    assert decision.approved
    assert decision.normalized_order is not None
    assert decision.normalized_order.limit_price == Decimal("10.05")


def test_context_requires_all_safety_state_instead_of_assuming_known() -> None:
    complete = context([proposal()]).model_dump()
    del complete["open_order_state_known"]
    del complete["daily_new_gross_exposure_usd"]

    with pytest.raises(ValidationError) as error:
        RiskContext.model_validate(complete)

    assert "open_order_state_known" in str(error.value)
    assert "daily_new_gross_exposure_usd" in str(error.value)


def test_rejects_oversized_and_million_dollar_positions() -> None:
    oversized = evaluate(context([proposal(amount="301")]))[0]
    malicious = evaluate(context([proposal(amount="1000000")]))[0]
    assert "MAX_POSITION_USD" in oversized.rejection_codes
    assert {"MAX_POSITION_USD", "INSUFFICIENT_CASH"}.issubset(malicious.rejection_codes)


def test_cumulative_daily_exposure_includes_earlier_approvals() -> None:
    results = evaluate(context([proposal("SPY", "260"), proposal("QQQ", "260")]))
    assert results[0].approved
    assert "MAX_DAILY_EXPOSURE" in results[1].rejection_codes


def test_daily_exposure_includes_usage_before_this_batch() -> None:
    decision = evaluate(
        context([proposal()], daily_new_gross_exposure_usd=Decimal("450"))
    )[0]
    assert "MAX_DAILY_EXPOSURE" in decision.rejection_codes


def test_cannot_sell_more_shares_than_held() -> None:
    held = [
        Position(
            symbol="SPY",
            qty=Decimal("10"),
            market_value=Decimal("100"),
            current_price=Decimal("10"),
        )
    ]
    order = evaluate(context([proposal(amount="1000", action="SELL")], positions=held))[
        0
    ].normalized_order
    assert order is not None
    assert order.qty == Decimal("10")


def test_rejects_no_position_sell() -> None:
    result = evaluate(context([proposal(action="SELL")]))[0]
    assert "SHORTING_DISABLED" in result.rejection_codes


def test_target_position_pct_means_final_position_value() -> None:
    held = [
        Position(
            symbol="SPY",
            qty=Decimal("10"),
            market_value=Decimal("100"),
            current_price=Decimal("10"),
        )
    ]
    buy = proposal(amount=None, target_pct="10")
    order = evaluate(context([buy], positions=held))[0].normalized_order
    assert order is not None
    assert Decimal("99") < order.notional <= Decimal("100")


def test_target_position_rejects_direction_already_met() -> None:
    held = [
        Position(
            symbol="SPY",
            qty=Decimal("30"),
            market_value=Decimal("300"),
            current_price=Decimal("10"),
        )
    ]
    buy = proposal(amount=None, target_pct="10")
    result = evaluate(context([buy], positions=held))[0]
    assert "TARGET_ALREADY_MET" in result.rejection_codes


def test_target_position_sell_can_exit_without_shorting() -> None:
    held = [
        Position(
            symbol="SPY",
            qty=Decimal("10"),
            market_value=Decimal("100"),
            current_price=Decimal("10"),
        )
    ]
    sell = proposal(amount=None, action="SELL", target_pct="0")
    order = evaluate(context([sell], positions=held))[0].normalized_order
    assert order is not None
    assert order.qty == Decimal("10")


def test_rejects_disallowed_symbol() -> None:
    result = evaluate(context([proposal("AAPL")]))[0]
    assert "SYMBOL_NOT_ALLOWED" in result.rejection_codes


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"broker_state_known": False}, "UNKNOWN_BROKER_STATE"),
        ({"portfolio_state_known": False}, "UNKNOWN_PORTFOLIO_STATE"),
        ({"open_order_state_known": False}, "UNKNOWN_OPEN_ORDER_STATE"),
        ({"market_is_open": False}, "OUTSIDE_TRADING_WINDOW"),
        ({"orders_today": 5}, "MAX_ORDER_COUNT"),
        ({"daily_drawdown_pct": Decimal("5")}, "DAILY_DRAWDOWN_BREAKER"),
        ({"weekly_drawdown_pct": Decimal("10")}, "WEEKLY_DRAWDOWN_BREAKER"),
        ({"peak_drawdown_pct": Decimal("20")}, "TOTAL_DRAWDOWN_BREAKER"),
    ],
)
def test_context_circuit_breakers(update: dict[str, object], code: str) -> None:
    risk_context = context([proposal()])
    risk_context = RiskContext.model_validate({**risk_context.model_dump(), **update})
    assert code in evaluate(risk_context)[0].rejection_codes


def test_rejects_broker_account_safety_mismatch() -> None:
    unsafe = Account(
        equity=Decimal("2000"),
        cash=Decimal("1000"),
        buying_power=Decimal("2000"),
        trading_blocked=True,
        shorting_enabled=True,
        options_level=1,
        multiplier=Decimal("2"),
    )
    codes = set(evaluate(context([proposal()], account=unsafe))[0].rejection_codes)
    assert {
        "ACCOUNT_TRADING_BLOCKED",
        "BROKER_SHORTING_ENABLED",
        "OPTIONS_DISABLED",
        "MARGIN_DISABLED",
    }.issubset(codes)


def test_provider_managed_paper_level_three_does_not_relax_instrument_policy() -> None:
    paper_account = Account(
        equity=Decimal("2000"),
        cash=Decimal("1000"),
        buying_power=Decimal("1000"),
        options_level=3,
    )
    equity_decision = evaluate(
        context(
            [proposal()],
            account=paper_account,
            paper_options_level_is_provider_managed=True,
        )
    )[0]
    option_decision = evaluate(
        context(
            [proposal()],
            account=paper_account,
            paper_options_level_is_provider_managed=True,
            assets={"SPY": asset("SPY", asset_class="us_option")},
        )
    )[0]

    assert equity_decision.approved
    assert "OPTIONS_DISABLED" in option_decision.rejection_codes


def test_provider_managed_exception_is_narrowly_limited_to_level_three() -> None:
    account = Account(
        equity=Decimal("2000"),
        cash=Decimal("1000"),
        buying_power=Decimal("1000"),
        options_level=2,
    )
    result = evaluate(
        context(
            [proposal()],
            account=account,
            paper_options_level_is_provider_managed=True,
        )
    )[0]
    assert "OPTIONS_DISABLED" in result.rejection_codes


@pytest.mark.parametrize(
    ("assets", "code"),
    [
        ({}, "UNKNOWN_ASSET_STATE"),
        ({"SPY": asset("QQQ")}, "INVALID_ASSET_STATE"),
        ({"SPY": asset("SPY", asset_class="crypto")}, "CRYPTO_DISABLED"),
        ({"SPY": asset("SPY", asset_class="us_option")}, "OPTIONS_DISABLED"),
        ({"SPY": asset("SPY", status="inactive")}, "ASSET_NOT_TRADABLE"),
        ({"SPY": asset("SPY", tradable=False)}, "ASSET_NOT_TRADABLE"),
    ],
)
def test_asset_metadata_fails_closed(
    assets: dict[str, TradableAsset], code: str
) -> None:
    result = evaluate(context([proposal()], assets=assets))[0]
    assert code in result.rejection_codes


def test_expected_equity_limit_is_inclusive_and_fails_above_boundary() -> None:
    exact = Account(equity=Decimal("2500"), cash=Decimal("1000"), buying_power=Decimal("1000"))
    above = exact.model_copy(update={"equity": Decimal("2500.01")})
    assert "UNEXPECTED_ACCOUNT_EQUITY" not in evaluate(
        context([proposal()], account=exact)
    )[0].rejection_codes
    assert "UNEXPECTED_ACCOUNT_EQUITY" in evaluate(
        context([proposal()], account=above)
    )[0].rejection_codes


def test_existing_open_order_blocks_new_orders() -> None:
    open_order = BrokerOrder(
        id="order-1",
        client_order_id="client-1",
        symbol="SPY",
        side="buy",
        status="new",
        qty=Decimal("1"),
        limit_price=Decimal("10"),
    )
    result = evaluate(context([proposal()], open_orders=[open_order]))[0]
    assert "OPEN_ORDER_PENDING" in result.rejection_codes


def test_configured_trade_count_is_cumulative() -> None:
    two_trade_policy = policy(max_trades_per_symbol_per_day=2)
    results = evaluate(
        context(
            [proposal("SPY", "50"), proposal("SPY", "50")],
            policy=two_trade_policy,
            trades_per_symbol_today={"SPY": 1},
        )
    )
    assert results[0].approved
    assert "MAX_TRADES_PER_SYMBOL" in results[1].rejection_codes


def test_maximum_invested_percentage_uses_existing_portfolio() -> None:
    held = [
        Position(
            symbol="QQQ",
            qty=Decimal("170"),
            market_value=Decimal("1700"),
            current_price=Decimal("10"),
        )
    ]
    result = evaluate(context([proposal(amount="101")], positions=held))[0]
    assert "MAX_INVESTED_PCT" in result.rejection_codes


def test_quote_age_boundary_uses_injected_as_of_time() -> None:
    item = proposal()
    exact = context(
        [item], quotes={"SPY": quote("SPY", AS_OF - timedelta(seconds=900))}
    )
    stale = context(
        [item], quotes={"SPY": quote("SPY", AS_OF - timedelta(seconds=901))}
    )
    assert "STALE_MARKET_DATA" not in evaluate(exact)[0].rejection_codes
    assert "STALE_MARKET_DATA" in evaluate(stale)[0].rejection_codes


@pytest.mark.parametrize(
    "bad_quote",
    [
        Quote.model_construct(
            symbol="SPY",
            bid=Decimal("NaN"),
            ask=Decimal("10"),
            timestamp=AS_OF,
            average_daily_dollar_volume=Decimal("6000000"),
        ),
        Quote(
            symbol="SPY",
            bid=Decimal("11"),
            ask=Decimal("10"),
            timestamp=AS_OF,
            average_daily_dollar_volume=Decimal("6000000"),
        ),
        quote("SPY", AS_OF + timedelta(seconds=1)),
        Quote(
            symbol="QQQ",
            bid=Decimal("9.99"),
            ask=Decimal("10"),
            timestamp=AS_OF,
            average_daily_dollar_volume=Decimal("6000000"),
        ),
    ],
)
def test_rejects_invalid_quotes(bad_quote: Quote) -> None:
    result = evaluate(context([proposal()], quotes={"SPY": bad_quote}))[0]
    assert "INVALID_MARKET_DATA" in result.rejection_codes


def test_rejects_missing_and_insufficient_liquidity_data() -> None:
    missing = quote("SPY").model_copy(update={"average_daily_dollar_volume": None})
    result = evaluate(context([proposal()], quotes={"SPY": missing}))[0]
    assert "INSUFFICIENT_LIQUIDITY" in result.rejection_codes


def test_rejects_limit_price_beyond_slippage_boundary() -> None:
    item = proposal()
    item.max_acceptable_price = Decimal("10.051")
    result = evaluate(context([item]))[0]
    assert "LIMIT_PRICE_TOO_AGGRESSIVE" in result.rejection_codes


def test_evaluation_is_repeatable_for_same_time_pinned_context() -> None:
    risk_context = context([proposal()])
    assert evaluate(risk_context) == evaluate(risk_context)
