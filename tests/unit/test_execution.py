from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from requests.exceptions import Timeout as RequestsTimeout
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.broker.models import (
    Account,
    BrokerFill,
    BrokerOrder,
    OrderQueryStatus,
    OrderSide,
    Position,
    Quote,
)
from trader.execution.executor import (
    DuplicateOrderError,
    Executor,
    OrderOutcomeUnknown,
    OrderSubmissionRejected,
    PaperTradingRequired,
    PersistedRiskApprovalRequired,
    TradingHalted,
    client_order_id,
)
from trader.execution.reconciliation import Reconciler
from trader.persistence.db import create_session_factory
from trader.persistence.models import BrokerOrderEvent, BrokerOrderRecord, Fill, Run
from trader.persistence.repositories import persist_risk_decision
from trader.risk.models import NormalizedOrder, RiskDecision


def now() -> datetime:
    return datetime.now(UTC)


def make_order(
    cid: str,
    status: str = "accepted",
    *,
    order_id: str = "b1",
    symbol: str = "AAA",
) -> BrokerOrder:
    return BrokerOrder(
        id=order_id,
        client_order_id=cid,
        symbol=symbol,
        side="buy",
        status=status,
        qty=Decimal("1"),
        limit_price=Decimal("10"),
        submitted_at=now(),
        updated_at=now(),
    )


class FakeBroker:
    def __init__(self) -> None:
        self.is_paper = True
        self.submitted = 0
        self.lookup_count = 0
        self.submit_error: Exception | None = None
        self.store_before_submit_error = False
        self.before_submit: Callable[[], None] | None = None
        self.remote: dict[str, BrokerOrder] = {}
        self.recent: list[BrokerOrder] | None = None
        self.fills: list[BrokerFill] = []
        self.canceled: list[str] = []

    def get_account(self) -> Account:
        return Account(equity=100, cash=100, buying_power=100)

    def get_positions(self) -> list[Position]:
        return []

    def get_open_orders(self) -> list[BrokerOrder]:
        return [order for order in self.remote.values() if order.status == "accepted"]

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]:
        del status, after, until
        return list(self.remote.values())[:limit] if self.recent is None else self.recent[:limit]

    def get_order_by_client_id(self, client_id: str) -> BrokerOrder | None:
        self.lookup_count += 1
        return self.remote.get(client_id)

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]:
        del after, until
        return self.fills

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, bid=10, ask=10, timestamp=now())

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
        if self.before_submit is not None:
            self.before_submit()
        order = BrokerOrder(
            id=f"b{self.submitted}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=cast("OrderSide", side),
            status="accepted",
            qty=Decimal(qty),
            limit_price=Decimal(limit_price),
            submitted_at=now(),
            updated_at=now(),
        )
        if self.store_before_submit_error:
            self.remote[client_order_id] = order
        if self.submit_error is not None:
            raise self.submit_error
        self.remote[client_order_id] = order
        return order

    def cancel_order(self, order_id: str) -> None:
        self.canceled.append(order_id)

    def cancel_all_orders(self) -> None:
        self.canceled.extend(order.id for order in self.remote.values())


def setup(tmp_path: Path) -> tuple[Session, RiskDecision]:
    session = create_session_factory(f"sqlite:///{tmp_path}/test.db")()
    session.add(
        Run(
            id="run",
            run_key="key",
            scheduled_for=now(),
            config_hash="x",
        )
    )
    session.commit()
    decision = RiskDecision(
        proposal_id="proposal",
        approved=True,
        normalized_order=NormalizedOrder(
            symbol="AAA",
            side="buy",
            qty=Decimal("1"),
            limit_price=Decimal("10"),
            notional=Decimal("10"),
        ),
        human_explanation="ok",
    )
    persist_risk_decision(session, "run", decision, policy_hash="policy-hash")
    return session, decision


def local_record(
    session: Session,
    *,
    cid: str,
    status: str = "SUBMITTING",
    broker_order_id: str | None = None,
) -> BrokerOrderRecord:
    record = BrokerOrderRecord(
        run_id="run",
        proposal_id="proposal",
        client_order_id=cid,
        broker_order_id=broker_order_id,
        symbol="AAA",
        side="buy",
        qty="1",
        notional="10",
        limit_price="10",
        status=status,
    )
    session.add(record)
    session.commit()
    return record


def test_client_id_deterministic() -> None:
    assert client_order_id("r", "p") == client_order_id("r", "p")


@pytest.mark.parametrize("enabled,stop_exists", [(False, False), (True, True)])
def test_kill_switches_block_before_broker_lookup(
    tmp_path: Path,
    enabled: bool,
    stop_exists: bool,
) -> None:
    session, decision = setup(tmp_path)
    stop = tmp_path / "STOP"
    if stop_exists:
        stop.touch()
    broker = FakeBroker()

    with pytest.raises(TradingHalted, match="kill switch"):
        Executor(broker, session, enabled, stop).submit("run", decision)

    assert broker.lookup_count == 0
    assert broker.submitted == 0
    assert session.scalar(select(BrokerOrderRecord)) is None


def test_live_broker_is_unavailable(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    broker.is_paper = False

    with pytest.raises(PaperTradingRequired, match="paper broker"):
        Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    assert broker.lookup_count == 0
    assert broker.submitted == 0


def test_live_broker_reconciliation_is_unavailable(tmp_path: Path) -> None:
    session, _decision = setup(tmp_path)
    broker = FakeBroker()
    broker.is_paper = False

    with pytest.raises(PaperTradingRequired, match="paper broker"):
        Reconciler(broker, session).reconcile()

    assert broker.lookup_count == 0


def test_unpersisted_risk_approval_cannot_reach_broker(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    decision = decision.model_copy(update={"proposal_id": "not-persisted"})

    with pytest.raises(PersistedRiskApprovalRequired, match="persisted"):
        Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    assert broker.lookup_count == 0
    assert broker.submitted == 0


def test_tampered_order_cannot_reuse_persisted_approval(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    assert decision.normalized_order is not None
    changed_order = decision.normalized_order.model_copy(update={"qty": Decimal("2")})
    decision = decision.model_copy(update={"normalized_order": changed_order})

    with pytest.raises(PersistedRiskApprovalRequired, match="does not match"):
        Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    assert broker.lookup_count == 0
    assert broker.submitted == 0


def test_submitting_is_committed_before_network_call(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    cid = client_order_id("run", "proposal")

    def verify_pre_submission_state() -> None:
        session.expire_all()
        record = session.scalar(
            select(BrokerOrderRecord).where(BrokerOrderRecord.client_order_id == cid)
        )
        assert record is not None
        assert record.status == "SUBMITTING"

    broker.before_submit = verify_pre_submission_state
    result = Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    assert result.client_order_id == cid
    record = session.scalar(select(BrokerOrderRecord))
    assert record is not None
    assert record.status == "ACCEPTED"
    assert record.qty == "1"
    assert record.limit_price == "10"


def test_duplicate_prevention_never_resubmits(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    executor = Executor(broker, session, True, tmp_path / "STOP")
    executor.submit("run", decision)

    with pytest.raises(DuplicateOrderError, match="duplicate"):
        executor.submit("run", decision)

    assert broker.submitted == 1


def test_requests_timeout_after_submission_recovers_remote_order(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    broker.store_before_submit_error = True
    broker.submit_error = RequestsTimeout("read timed out")

    result = Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    assert result.status == "accepted"
    record = session.scalar(select(BrokerOrderRecord))
    assert record is not None
    assert record.status == "ACCEPTED"
    assert record.broker_order_id == result.id


def test_timeout_without_remote_order_is_unknown(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    broker.submit_error = RequestsTimeout("connect timed out")

    with pytest.raises(OrderOutcomeUnknown, match="reconciliation required"):
        Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    record = session.scalar(select(BrokerOrderRecord))
    assert record is not None
    assert record.status == "UNKNOWN"
    assert broker.submitted == 1


def test_broker_500_without_remote_order_is_unknown(tmp_path: Path) -> None:
    class BrokerServerError(Exception):
        status_code = 500

    session, decision = setup(tmp_path)
    broker = FakeBroker()
    broker.submit_error = BrokerServerError("internal error")

    with pytest.raises(OrderOutcomeUnknown, match="reconciliation required"):
        Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    record = session.scalar(select(BrokerOrderRecord))
    assert record is not None
    assert record.status == "UNKNOWN"


def test_explicit_local_submission_error_is_rejected(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    broker.submit_error = ValueError("invalid quantity")

    with pytest.raises(OrderSubmissionRejected, match="rejected"):
        Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    record = session.scalar(select(BrokerOrderRecord))
    assert record is not None
    assert record.status == "REJECTED"


def test_remote_duplicate_identity_mismatch_is_unknown(tmp_path: Path) -> None:
    session, decision = setup(tmp_path)
    broker = FakeBroker()
    cid = client_order_id("run", "proposal")
    broker.remote[cid] = make_order(cid, symbol="WRONG")

    with pytest.raises(OrderOutcomeUnknown, match="identity"):
        Executor(broker, session, True, tmp_path / "STOP").submit("run", decision)

    record = session.scalar(select(BrokerOrderRecord))
    assert record is not None
    assert record.status == "UNKNOWN"
    assert broker.submitted == 0


def test_restart_reconciliation_recovers_submitting_order(tmp_path: Path) -> None:
    session, _decision = setup(tmp_path)
    cid = client_order_id("run", "proposal")
    record = local_record(session, cid=cid)
    broker = FakeBroker()
    broker.remote[cid] = make_order(cid)

    report = Reconciler(broker, session).reconcile(run_id="run")

    session.refresh(record)
    assert report.updated == 1
    assert record.status == "ACCEPTED"
    assert record.broker_order_id == "b1"
    assert broker.submitted == 0


def test_reconciliation_falls_back_to_direct_lookup(tmp_path: Path) -> None:
    session, _decision = setup(tmp_path)
    cid = client_order_id("run", "proposal")
    record = local_record(session, cid=cid)
    broker = FakeBroker()
    broker.remote[cid] = make_order(cid, "filled")
    broker.recent = []

    report = Reconciler(broker, session).reconcile()

    session.refresh(record)
    assert report.updated == 1
    assert record.status == "FILLED"
    assert broker.lookup_count == 1
    assert broker.submitted == 0


def test_missing_remote_order_is_flagged_unknown(tmp_path: Path) -> None:
    session, _decision = setup(tmp_path)
    record = local_record(session, cid="missing", status="ACCEPTED")
    broker = FakeBroker()

    report = Reconciler(broker, session).reconcile()

    session.refresh(record)
    assert record.status == "UNKNOWN"
    assert report.updated == 1
    assert [issue.code for issue in report.issues] == ["REMOTE_ORDER_MISSING"]
    assert broker.submitted == 0


def test_untracked_remote_order_is_flagged_even_without_local_orders(tmp_path: Path) -> None:
    session, _decision = setup(tmp_path)
    broker = FakeBroker()
    broker.remote["manual-order"] = make_order("manual-order")

    report = Reconciler(broker, session).reconcile()

    assert report.checked == 0
    assert [issue.code for issue in report.issues] == ["UNTRACKED_REMOTE_ORDER"]
    assert broker.submitted == 0


def test_untracked_remote_fill_is_flagged(tmp_path: Path) -> None:
    session, _decision = setup(tmp_path)
    broker = FakeBroker()
    broker.fills = [
        BrokerFill(
            id="manual-fill",
            order_id="manual-order",
            symbol="AAA",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("10"),
            cumulative_qty=Decimal("1"),
            leaves_qty=Decimal("0"),
            transaction_time=now(),
            order_status="filled",
        )
    ]

    report = Reconciler(broker, session).reconcile()

    assert report.checked == 0
    assert [issue.code for issue in report.issues] == ["UNTRACKED_REMOTE_FILL"]
    assert broker.submitted == 0


@pytest.mark.parametrize(
    ("remote_status", "local_status"),
    [
        ("partially_filled", "PARTIALLY_FILLED"),
        ("filled", "FILLED"),
        ("canceled", "CANCELED"),
        ("rejected", "REJECTED"),
        ("expired", "EXPIRED"),
        ("suspended", "UNKNOWN"),
    ],
)
def test_reconciliation_maps_authoritative_terminal_states(
    tmp_path: Path,
    remote_status: str,
    local_status: str,
) -> None:
    session, _decision = setup(tmp_path)
    cid = client_order_id("run", "proposal")
    record = local_record(session, cid=cid, status="ACCEPTED", broker_order_id="b1")
    broker = FakeBroker()
    broker.remote[cid] = make_order(cid, remote_status)

    Reconciler(broker, session).reconcile()

    session.refresh(record)
    assert record.status == local_status
    assert broker.submitted == 0


def test_fill_ledger_recovers_order_and_is_idempotent(tmp_path: Path) -> None:
    session, _decision = setup(tmp_path)
    record = local_record(
        session,
        cid="cid",
        status="ACCEPTED",
        broker_order_id="broker-order",
    )
    broker = FakeBroker()
    broker.fills = [
        BrokerFill(
            id="activity-1",
            order_id="broker-order",
            symbol="AAA",
            side="buy",
            qty=Decimal("1"),
            price=Decimal("9.99"),
            cumulative_qty=Decimal("1"),
            leaves_qty=Decimal("0"),
            transaction_time=now(),
            order_status="filled",
        )
    ]

    reconciler = Reconciler(broker, session)
    reconciler.reconcile()
    reconciler.reconcile()

    session.refresh(record)
    assert record.status == "FILLED"
    assert len(session.scalars(select(Fill)).all()) == 1
    events = session.scalars(select(BrokerOrderEvent)).all()
    assert len([event for event in events if event.event_type == "FILL_STATUS"]) == 1
    assert broker.submitted == 0
