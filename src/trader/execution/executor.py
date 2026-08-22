"""Only component permitted to submit an approved normalized order."""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import httpx
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trader.broker.base import Broker
from trader.broker.models import BrokerOrder
from trader.persistence.models import BrokerOrderRecord, RiskDecisionRecord
from trader.persistence.repositories import record_broker_order_event
from trader.risk.models import RiskDecision

from .statuses import canonical_order_status


class ExecutionError(RuntimeError):
    """Base class for fail-closed execution errors."""


class DuplicateOrderError(ExecutionError):
    """The deterministic order identifier was already observed locally or remotely."""


class OrderOutcomeUnknown(ExecutionError):
    """The broker may have received an order, but its state could not be established."""


class OrderSubmissionRejected(ExecutionError):
    """The broker rejected the submission and has no order for its client identifier."""


class PaperTradingRequired(ExecutionError):
    """Execution was attempted against a broker that is not explicitly paper-only."""


class TradingHalted(ExecutionError):
    """One of the independent trading kill switches is active."""


class PersistedRiskApprovalRequired(ExecutionError):
    """Execution lacked an identical immutable, policy-hashed risk approval."""


AMBIGUOUS_NETWORK_ERRORS = (
    TimeoutError,
    ConnectionError,
    httpx.TransportError,
    RequestsTimeout,
    RequestsConnectionError,
)


def _is_ambiguous_submission_error(exc: Exception) -> bool:
    if isinstance(exc, AMBIGUOUS_NETWORK_ERRORS):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int | str):
        return False
    try:
        numeric_status = int(status_code)
    except ValueError:
        return False
    return numeric_status in {408, 409, 425, 429} or numeric_status >= 500


def client_order_id(run_id: str, proposal_id: str) -> str:
    run_hash = sha256(run_id.encode()).hexdigest()[:8]
    proposal_hash = sha256(proposal_id.encode()).hexdigest()[:8]
    return f"aitrader-{run_hash}-{proposal_hash}"


def _error_json(exc: BaseException) -> str:
    return json.dumps(
        {"error_type": f"{type(exc).__module__}.{type(exc).__name__}", "message": str(exc)},
        sort_keys=True,
    )


class Executor:
    def __init__(
        self,
        broker: Broker,
        session: Session,
        trading_enabled: bool,
        stop_file: Path,
    ) -> None:
        self.broker = broker
        self.session = session
        self.trading_enabled = trading_enabled
        self.stop_file = stop_file

    def _assert_submission_allowed(self) -> None:
        if not self.broker.is_paper:
            raise PaperTradingRequired("paper broker required; live execution is unavailable")
        if not self.trading_enabled or self.stop_file.exists():
            raise TradingHalted("trading kill switch active")

    def _find_local(self, cid: str) -> BrokerOrderRecord | None:
        return self.session.scalar(
            select(BrokerOrderRecord).where(BrokerOrderRecord.client_order_id == cid)
        )

    def _assert_persisted_approval(self, run_id: str, decision: RiskDecision) -> None:
        persisted = self.session.scalar(
            select(RiskDecisionRecord).where(
                RiskDecisionRecord.run_id == run_id,
                RiskDecisionRecord.proposal_id == decision.proposal_id,
            )
        )
        normalized_order_json = (
            decision.normalized_order.model_dump_json()
            if decision.normalized_order is not None
            else None
        )
        if (
            persisted is None
            or not persisted.approved
            or not persisted.policy_hash
            or persisted.normalized_order_json is None
            or normalized_order_json is None
        ):
            raise PersistedRiskApprovalRequired(
                "matching persisted, policy-hashed risk approval required"
            )
        if json.loads(persisted.normalized_order_json) != json.loads(normalized_order_json):
            raise PersistedRiskApprovalRequired(
                "persisted risk approval does not match the requested order"
            )

    def _record_event(
        self,
        record: BrokerOrderRecord,
        *,
        event_type: str,
        status: str,
        raw_json: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        raw_hash = sha256((raw_json or "").encode()).hexdigest()[:16]
        event_key = f"executor:{record.client_order_id}:{event_type}:{status}:{raw_hash}"
        record_broker_order_event(
            self.session,
            broker_order_record_id=record.id,
            event_key=event_key,
            event_type=event_type,
            status=status,
            occurred_at=occurred_at,
            broker_event_id=None,
            raw_json=raw_json,
        )

    def _apply_remote(self, record: BrokerOrderRecord, order: BrokerOrder) -> None:
        status = canonical_order_status(order.status)
        raw_json = order.model_dump_json()
        if (
            order.client_order_id != record.client_order_id
            or order.symbol.upper() != record.symbol.upper()
            or order.side.lower() != record.side.lower()
        ):
            record.status = "UNKNOWN"
            record.raw_json = raw_json
            self.session.commit()
            self._record_event(
                record,
                event_type="IDENTITY_MISMATCH",
                status="UNKNOWN",
                raw_json=raw_json,
                occurred_at=order.updated_at,
            )
            raise OrderOutcomeUnknown("broker order identity did not match the submitted order")
        record.broker_order_id = order.id
        record.status = status
        record.raw_json = raw_json
        record.submitted_at = order.submitted_at
        self.session.commit()
        self._record_event(
            record,
            event_type="BROKER_STATUS",
            status=status,
            raw_json=raw_json,
            occurred_at=order.updated_at,
        )
        if status == "UNKNOWN":
            raise OrderOutcomeUnknown(f"unrecognized broker order status {order.status!r}")

    def _handle_existing(
        self,
        *,
        record: BrokerOrderRecord,
        cid: str,
    ) -> None:
        try:
            remote = self.broker.get_order_by_client_id(cid)
        except Exception as exc:
            if record.status.upper() in {"SUBMITTING", "UNKNOWN"}:
                record.status = "UNKNOWN"
                record.raw_json = _error_json(exc)
                self.session.commit()
                self._record_event(
                    record,
                    event_type="LOOKUP_FAILED",
                    status="UNKNOWN",
                    raw_json=record.raw_json,
                )
                raise OrderOutcomeUnknown("existing order could not be reconciled") from exc
            raise DuplicateOrderError("duplicate local order") from exc
        if remote is not None:
            self._apply_remote(record, remote)
        elif record.status.upper() == "SUBMITTING":
            record.status = "UNKNOWN"
            self.session.commit()
            self._record_event(record, event_type="REMOTE_MISSING", status="UNKNOWN")
            raise OrderOutcomeUnknown("existing submission has no authoritative broker state")
        raise DuplicateOrderError("duplicate local order")

    def _recover_after_submission_error(
        self,
        *,
        record: BrokerOrderRecord,
        cid: str,
        submit_error: Exception,
    ) -> BrokerOrder:
        try:
            remote = self.broker.get_order_by_client_id(cid)
        except Exception as lookup_error:
            record.status = "UNKNOWN"
            record.raw_json = _error_json(lookup_error)
            self.session.commit()
            self._record_event(
                record,
                event_type="LOOKUP_FAILED",
                status="UNKNOWN",
                raw_json=record.raw_json,
            )
            raise OrderOutcomeUnknown(
                "submission and broker lookup outcomes are unknown"
            ) from submit_error
        if remote is not None:
            self._apply_remote(record, remote)
            return remote
        record.raw_json = _error_json(submit_error)
        if _is_ambiguous_submission_error(submit_error):
            record.status = "UNKNOWN"
            self.session.commit()
            self._record_event(
                record,
                event_type="SUBMISSION_UNKNOWN",
                status="UNKNOWN",
                raw_json=record.raw_json,
            )
            raise OrderOutcomeUnknown(
                "order outcome unknown; reconciliation required"
            ) from submit_error
        record.status = "REJECTED"
        self.session.commit()
        self._record_event(
            record,
            event_type="SUBMISSION_REJECTED",
            status="REJECTED",
            raw_json=record.raw_json,
        )
        raise OrderSubmissionRejected("broker rejected order submission") from submit_error

    def submit(self, run_id: str, decision: RiskDecision) -> BrokerOrder:
        if not decision.approved or decision.normalized_order is None:
            raise ValueError("risk approval required")
        self._assert_submission_allowed()
        self._assert_persisted_approval(run_id, decision)

        cid = client_order_id(run_id, decision.proposal_id)
        existing = self._find_local(cid)
        if existing is not None:
            self._handle_existing(record=existing, cid=cid)

        remote = self.broker.get_order_by_client_id(cid)
        if remote is not None:
            order = decision.normalized_order
            recovered = BrokerOrderRecord(
                run_id=run_id,
                proposal_id=decision.proposal_id,
                client_order_id=cid,
                symbol=order.symbol,
                side=order.side,
                qty=str(order.qty),
                notional=str(order.notional),
                limit_price=str(order.limit_price),
                status="SUBMITTING",
            )
            self.session.add(recovered)
            try:
                self.session.commit()
            except IntegrityError as exc:
                self.session.rollback()
                raise DuplicateOrderError("duplicate broker order") from exc
            self._apply_remote(recovered, remote)
            self._record_event(
                recovered,
                event_type="REMOTE_DUPLICATE_FOUND",
                status=recovered.status,
                raw_json=recovered.raw_json,
                occurred_at=remote.updated_at,
            )
            raise DuplicateOrderError("duplicate broker order")

        order = decision.normalized_order
        record = BrokerOrderRecord(
            run_id=run_id,
            proposal_id=decision.proposal_id,
            client_order_id=cid,
            symbol=order.symbol,
            side=order.side,
            qty=str(order.qty),
            notional=str(order.notional),
            limit_price=str(order.limit_price),
            status="SUBMITTING",
        )
        self.session.add(record)
        try:
            # This commit is deliberately before the only broker submission call.
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raced = self._find_local(cid)
            if raced is not None:
                self._handle_existing(record=raced, cid=cid)
            raise DuplicateOrderError("duplicate local order") from exc
        self._record_event(record, event_type="SUBMITTING", status="SUBMITTING")

        # Re-check the independent safety gates immediately before submitting.
        try:
            self._assert_submission_allowed()
        except ExecutionError:
            record.status = "REJECTED"
            self.session.commit()
            self._record_event(record, event_type="KILL_SWITCH", status="REJECTED")
            raise

        try:
            result = self.broker.submit_limit_order(
                client_order_id=cid,
                symbol=order.symbol,
                side=order.side,
                qty=str(order.qty),
                limit_price=str(order.limit_price),
            )
        except Exception as exc:
            return self._recover_after_submission_error(
                record=record,
                cid=cid,
                submit_error=exc,
            )

        self._apply_remote(record, result)
        return result
