from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select

from trader.agent.models import TradeProposal
from trader.broker.models import (
    Account,
    BrokerFill,
    BrokerOrder,
    MarketClock,
    OrderQueryStatus,
    OrderSide,
    Position,
    Quote,
    TradableAsset,
)
from trader.execution.canary import paper_canary
from trader.execution.executor import TradingHalted
from trader.persistence.db import create_session_factory
from trader.persistence.models import BrokerOrderRecord, MarketSnapshot, RiskDecisionRecord, Run
from trader.persistence.repositories import persist_trade_proposal, snapshot
from trader.risk.config import load_risk_config
from trader.risk.runtime import PaperRiskExecutionPipeline

PROJECT_ROOT = Path(__file__).parents[2]
RISK_CONFIG = load_risk_config(PROJECT_ROOT / "config" / "risk.yaml")


class RuntimeBroker:
    is_paper = True
    paper_options_level_is_provider_managed = False

    def __init__(self, *, market_open: bool = True) -> None:
        self.market_open = market_open
        self.remote: dict[str, BrokerOrder] = {}
        self.submitted = 0
        self.canceled: list[str] = []

    def get_account(self) -> Account:
        return Account(equity=2000, cash=2000, buying_power=2000)

    def get_positions(self) -> list[Position]:
        return []

    def get_open_orders(self) -> list[BrokerOrder]:
        return [
            item
            for item in self.remote.values()
            if item.status in {"accepted", "partially_filled"}
        ]

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]:
        del status, after, until
        return list(self.remote.values())[:limit]

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.remote.get(client_order_id)

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]:
        del after, until
        return []

    def get_clock(self) -> MarketClock:
        now = datetime.now(UTC)
        return MarketClock(
            timestamp=now,
            is_open=self.market_open,
            next_open=now + timedelta(days=1),
            next_close=now + timedelta(hours=6),
        )

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            bid=Decimal("9.99"),
            ask=Decimal("10"),
            timestamp=datetime.now(UTC),
            feed="test",
            average_daily_dollar_volume=Decimal("10000000"),
        )

    def get_asset(self, symbol: str) -> TradableAsset:
        return TradableAsset(
            symbol=symbol,
            asset_class="us_equity",
            status="active",
            tradable=True,
            exchange="ARCA",
        )

    def submit_limit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: str,
        limit_price: str,
    ) -> BrokerOrder:
        self.submitted += 1
        now = datetime.now(UTC)
        order = BrokerOrder(
            id=f"paper-{self.submitted}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=cast("OrderSide", side),
            status="accepted",
            qty=Decimal(qty),
            limit_price=Decimal(limit_price),
            submitted_at=now,
            updated_at=now,
        )
        self.remote[client_order_id] = order
        return order

    def cancel_order(self, order_id: str) -> None:
        self.canceled.append(order_id)
        for client_id, order in tuple(self.remote.items()):
            if order.id == order_id:
                self.remote[client_id] = order.model_copy(
                    update={"status": "canceled", "updated_at": datetime.now(UTC)}
                )

    def cancel_all_orders(self) -> None:
        for order in tuple(self.remote.values()):
            self.cancel_order(order.id)


def proposal() -> TradeProposal:
    return TradeProposal(
        symbol="SPY",
        action="BUY",
        target_notional_usd=Decimal("25"),
        confidence=0.8,
        time_horizon="days",
        rationale="bounded test proposal",
        max_acceptable_price=Decimal("10"),
    )


def setup(tmp_path: Path) -> tuple[object, Run, Path]:
    session = create_session_factory(f"sqlite:///{tmp_path}/runtime.db")()
    run = Run(run_key=f"runtime:{tmp_path.name}", scheduled_for=datetime.now(UTC), config_hash="x")
    session.add(run)
    session.commit()
    directory = tmp_path / "run"
    directory.mkdir()
    snapshot(session, Account(equity=2000, cash=2000, buying_power=2000), [], run.id)
    return session, run, directory


def test_runtime_persists_approval_without_submitting_when_disabled(tmp_path: Path) -> None:
    session, run, directory = setup(tmp_path)
    broker = RuntimeBroker()
    item = proposal()
    persist_trade_proposal(session, run.id, item)
    pipeline = PaperRiskExecutionPipeline(
        session,
        broker,
        RISK_CONFIG,
        trading_enabled=False,
        stop_file=tmp_path / "STOP",
    )

    result = pipeline.run(
        run_id=run.id,
        run_directory=directory,
        account=broker.get_account(),
        positions=(),
        open_orders=(),
        proposals=(item,),
        allowed_symbols=frozenset({"SPY"}),
        allow_execution=True,
    )

    assert result.decisions[0].approved is True
    assert result.execution_enabled is False
    assert result.submitted_orders == ()
    assert broker.submitted == 0
    assert session.scalar(select(RiskDecisionRecord)) is not None
    assert session.scalar(select(MarketSnapshot)) is not None
    assert (directory / "risk_context.json").is_file()
    assert (directory / "risk_decisions.json").is_file()


def test_runtime_submits_only_after_persisted_approval_and_reconciles(tmp_path: Path) -> None:
    session, run, directory = setup(tmp_path)
    broker = RuntimeBroker()
    item = proposal()
    persist_trade_proposal(session, run.id, item)
    pipeline = PaperRiskExecutionPipeline(
        session,
        broker,
        RISK_CONFIG,
        trading_enabled=True,
        stop_file=tmp_path / "STOP",
    )

    result = pipeline.run(
        run_id=run.id,
        run_directory=directory,
        account=broker.get_account(),
        positions=(),
        open_orders=(),
        proposals=(item,),
        allowed_symbols=frozenset({"SPY"}),
        allow_execution=True,
    )

    assert result.decisions[0].approved is True
    assert len(result.submitted_orders) == 1
    assert broker.submitted == 1
    local = session.scalar(select(BrokerOrderRecord))
    assert local is not None
    assert local.status == "ACCEPTED"
    assert result.reconciliation is not None
    assert result.reconciliation.issues == ()


def test_runtime_rejects_when_broker_clock_is_closed(tmp_path: Path) -> None:
    session, run, directory = setup(tmp_path)
    broker = RuntimeBroker(market_open=False)
    item = proposal()
    persist_trade_proposal(session, run.id, item)
    pipeline = PaperRiskExecutionPipeline(
        session,
        broker,
        RISK_CONFIG,
        trading_enabled=True,
        stop_file=tmp_path / "STOP",
    )

    result = pipeline.run(
        run_id=run.id,
        run_directory=directory,
        account=broker.get_account(),
        positions=(),
        open_orders=(),
        proposals=(item,),
        allowed_symbols=frozenset({"SPY"}),
        allow_execution=True,
    )

    assert "OUTSIDE_TRADING_WINDOW" in result.decisions[0].rejection_codes
    assert broker.submitted == 0


def test_runtime_stop_file_blocks_after_approval_before_submission(tmp_path: Path) -> None:
    session, run, directory = setup(tmp_path)
    broker = RuntimeBroker()
    item = proposal()
    persist_trade_proposal(session, run.id, item)
    stop_file = tmp_path / "STOP"
    stop_file.touch()
    pipeline = PaperRiskExecutionPipeline(
        session,
        broker,
        RISK_CONFIG,
        trading_enabled=True,
        stop_file=stop_file,
    )

    with pytest.raises(TradingHalted, match="kill switch"):
        pipeline.run(
            run_id=run.id,
            run_directory=directory,
            account=broker.get_account(),
            positions=(),
            open_orders=(),
            proposals=(item,),
            allowed_symbols=frozenset({"SPY"}),
            allow_execution=True,
        )

    assert broker.submitted == 0
    persisted = session.scalar(select(RiskDecisionRecord))
    assert persisted is not None and persisted.approved is True


def test_paper_canary_submits_then_cancels_an_open_paper_order(tmp_path: Path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/canary.db")()
    broker = RuntimeBroker()

    result = paper_canary(
        session,
        broker,
        tmp_path / "raw",
        RISK_CONFIG,
        b"mode: paper\n",
        stop_file=tmp_path / "STOP",
        submit=True,
        trading_enabled=True,
    )

    assert result.approved is True
    assert result.submitted_order_count == 1
    assert result.canceled_order_count == 1
    assert broker.canceled == ["paper-1"]
    run = session.get(Run, result.run_id)
    assert run is not None and run.status == "COMPLETED"


def test_submitting_canary_requires_environment_gate_before_creating_run(
    tmp_path: Path,
) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/canary-gate.db")()

    with pytest.raises(RuntimeError, match="TRADING_ENABLED=true"):
        paper_canary(
            session,
            RuntimeBroker(),
            tmp_path / "raw",
            RISK_CONFIG,
            b"mode: paper\n",
            stop_file=tmp_path / "STOP",
            submit=True,
            trading_enabled=False,
        )

    assert session.scalar(select(Run)) is None
