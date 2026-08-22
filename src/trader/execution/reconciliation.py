"""Read-only broker reconciliation for locally persisted paper orders."""

import json
from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.broker.base import Broker
from trader.broker.models import BrokerFill, BrokerOrder
from trader.persistence.models import BrokerOrderRecord
from trader.persistence.repositories import record_broker_order_event, record_fill

from .executor import PaperTradingRequired
from .statuses import canonical_order_status


class ReconciliationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    client_order_id: str | None = None
    detail: str


class ReconciliationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    checked: int
    updated: int
    unchanged: int
    issues: tuple[ReconciliationIssue, ...]


class Reconciler:
    """Compare local rows to paper-broker state without any order-submission capability."""

    def __init__(self, broker: Broker, session: Session) -> None:
        self.broker = broker
        self.session = session

    def _record_event(
        self,
        local: BrokerOrderRecord,
        *,
        event_type: str,
        status: str,
        raw_json: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        raw_hash = sha256((raw_json or "").encode()).hexdigest()[:16]
        event_key = f"reconcile:{local.client_order_id}:{event_type}:{status}:{raw_hash}"
        record_broker_order_event(
            self.session,
            broker_order_record_id=local.id,
            event_key=event_key,
            event_type=event_type,
            status=status,
            occurred_at=occurred_at,
            raw_json=raw_json,
        )

    def _record_fills(self, local: BrokerOrderRecord, fills: list[BrokerFill]) -> None:
        for fill in fills:
            record_fill(
                self.session,
                broker_order_record_id=local.id,
                broker_activity_id=fill.id,
                qty=fill.qty,
                price=fill.price,
                side=fill.side,
                transaction_time=fill.transaction_time,
                raw_json=fill.model_dump_json(),
            )

    @staticmethod
    def _fill_status(fill: BrokerFill) -> str:
        status = canonical_order_status(fill.order_status)
        if status != "UNKNOWN":
            return status
        if fill.leaves_qty > 0:
            return "PARTIALLY_FILLED"
        if fill.cumulative_qty > 0:
            return "FILLED"
        return "UNKNOWN"

    @staticmethod
    def _identity_issue(
        local: BrokerOrderRecord,
        remote: BrokerOrder,
    ) -> ReconciliationIssue | None:
        if local.broker_order_id is not None and local.broker_order_id != remote.id:
            return ReconciliationIssue(
                code="BROKER_ORDER_ID_MISMATCH",
                client_order_id=local.client_order_id,
                detail=f"local={local.broker_order_id} remote={remote.id}",
            )
        if (
            local.client_order_id != remote.client_order_id
            or local.symbol.upper() != remote.symbol.upper()
            or local.side.lower() != remote.side.lower()
        ):
            return ReconciliationIssue(
                code="ORDER_IDENTITY_MISMATCH",
                client_order_id=local.client_order_id,
                detail="remote client ID, symbol, or side does not match the local order",
            )
        return None

    @staticmethod
    def _apply_order(local: BrokerOrderRecord, remote: BrokerOrder, status: str) -> bool:
        raw_json = remote.model_dump_json()
        changed = (
            local.broker_order_id != remote.id
            or local.status != status
            or local.raw_json != raw_json
        )
        local.broker_order_id = remote.id
        local.status = status
        local.raw_json = raw_json
        return changed

    @staticmethod
    def _apply_fill(local: BrokerOrderRecord, fill: BrokerFill, status: str) -> bool:
        raw_json = fill.model_dump_json()
        changed = local.status != status or local.raw_json != raw_json
        local.status = status
        local.raw_json = raw_json
        return changed

    def reconcile(
        self,
        *,
        run_id: str | None = None,
        after: datetime | None = None,
    ) -> ReconciliationReport:
        if not self.broker.is_paper:
            raise PaperTradingRequired("paper broker required; live reconciliation is unavailable")

        statement = select(BrokerOrderRecord)
        if run_id is not None:
            statement = statement.where(BrokerOrderRecord.run_id == run_id)
        local_orders = list(self.session.scalars(statement).all())

        issues: list[ReconciliationIssue] = []
        remote_by_client_id: dict[str, BrokerOrder] = {}
        try:
            recent = self.broker.get_orders(status="all", after=after, limit=500)
            remote_by_client_id = {order.client_order_id: order for order in recent}
        except Exception as exc:
            issues.append(ReconciliationIssue(code="ORDER_QUERY_FAILED", detail=str(exc)))

        fills_by_order_id: dict[str, list[BrokerFill]] = {}
        try:
            fills = self.broker.get_fills(after=after)
            for fill in fills:
                fills_by_order_id.setdefault(fill.order_id, []).append(fill)
        except Exception as exc:
            issues.append(ReconciliationIssue(code="FILL_QUERY_FAILED", detail=str(exc)))

        local_client_ids = {order.client_order_id for order in local_orders}
        for untracked_remote in remote_by_client_id.values():
            if untracked_remote.client_order_id not in local_client_ids:
                issues.append(
                    ReconciliationIssue(
                        code="UNTRACKED_REMOTE_ORDER",
                        client_order_id=untracked_remote.client_order_id,
                        detail=(
                            f"broker_order_id={untracked_remote.id} "
                            f"status={untracked_remote.status}"
                        ),
                    )
                )
        updated = 0
        unchanged = 0
        for local in local_orders:
            remote = remote_by_client_id.get(local.client_order_id)
            if remote is None:
                try:
                    remote = self.broker.get_order_by_client_id(local.client_order_id)
                except Exception as exc:
                    raw_json = json.dumps(
                        {"lookup_error": type(exc).__name__, "message": str(exc)},
                        sort_keys=True,
                    )
                    changed = local.status != "UNKNOWN" or local.raw_json != raw_json
                    local.status = "UNKNOWN"
                    local.raw_json = raw_json
                    issues.append(
                        ReconciliationIssue(
                            code="ORDER_LOOKUP_FAILED",
                            client_order_id=local.client_order_id,
                            detail=str(exc),
                        )
                    )
                    updated += int(changed)
                    unchanged += int(not changed)
                    self._record_event(
                        local,
                        event_type="LOOKUP_FAILED",
                        status="UNKNOWN",
                        raw_json=raw_json,
                    )
                    continue

            if remote is not None:
                identity_issue = self._identity_issue(local, remote)
                if identity_issue is not None:
                    changed = (
                        local.status != "UNKNOWN" or local.raw_json != remote.model_dump_json()
                    )
                    local.status = "UNKNOWN"
                    raw_json = remote.model_dump_json()
                    local.raw_json = raw_json
                    issues.append(identity_issue)
                    updated += int(changed)
                    unchanged += int(not changed)
                    self._record_event(
                        local,
                        event_type=identity_issue.code,
                        status="UNKNOWN",
                        raw_json=raw_json,
                        occurred_at=remote.updated_at,
                    )
                    continue
                status = canonical_order_status(remote.status)
                if status == "UNKNOWN":
                    issues.append(
                        ReconciliationIssue(
                            code="UNMAPPED_ORDER_STATUS",
                            client_order_id=local.client_order_id,
                            detail=remote.status,
                        )
                    )
                changed = self._apply_order(local, remote, status)
                updated += int(changed)
                unchanged += int(not changed)
                raw_json = remote.model_dump_json()
                self._record_event(
                    local,
                    event_type="BROKER_STATUS",
                    status=status,
                    raw_json=raw_json,
                    occurred_at=remote.updated_at,
                )
                self._record_fills(local, fills_by_order_id.get(remote.id, []))
                continue

            order_fills = (
                fills_by_order_id.get(local.broker_order_id, [])
                if local.broker_order_id is not None
                else []
            )
            if order_fills:
                fill = max(order_fills, key=lambda item: item.transaction_time)
                status = self._fill_status(fill)
                if status == "UNKNOWN":
                    issues.append(
                        ReconciliationIssue(
                            code="UNMAPPED_FILL_STATUS",
                            client_order_id=local.client_order_id,
                            detail=fill.order_status,
                        )
                    )
                changed = self._apply_fill(local, fill, status)
                updated += int(changed)
                unchanged += int(not changed)
                self._record_event(
                    local,
                    event_type="FILL_STATUS",
                    status=status,
                    raw_json=fill.model_dump_json(),
                    occurred_at=fill.transaction_time,
                )
                self._record_fills(local, order_fills)
                continue

            changed = local.status != "UNKNOWN"
            local.status = "UNKNOWN"
            issues.append(
                ReconciliationIssue(
                    code="REMOTE_ORDER_MISSING",
                    client_order_id=local.client_order_id,
                    detail="order was absent from recent history and direct lookup",
                )
            )
            updated += int(changed)
            unchanged += int(not changed)
            self._record_event(local, event_type="REMOTE_MISSING", status="UNKNOWN")

        local_broker_ids = {
            order.broker_order_id for order in local_orders if order.broker_order_id is not None
        }
        for broker_order_id, order_fills in fills_by_order_id.items():
            if broker_order_id not in local_broker_ids:
                issues.append(
                    ReconciliationIssue(
                        code="UNTRACKED_REMOTE_FILL",
                        detail=(f"broker_order_id={broker_order_id} activities={len(order_fills)}"),
                    )
                )

        self.session.commit()
        return ReconciliationReport(
            checked=len(local_orders),
            updated=updated,
            unchanged=unchanged,
            issues=tuple(issues),
        )
