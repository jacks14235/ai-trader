"""Explicit, audited end-to-end canary for the Alpaca paper execution path."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from trader.agent.models import TradeProposal
from trader.agent.runner import verify_paper_broker_configuration
from trader.broker.base import Broker
from trader.execution.reconciliation import Reconciler
from trader.execution.statuses import canonical_order_status
from trader.persistence.repositories import claim_run, event, persist_trade_proposal, snapshot
from trader.risk.config import RiskConfig
from trader.risk.runtime import PaperRiskExecutionPipeline


@dataclass(frozen=True)
class PaperCanaryResult:
    run_id: str
    approved: bool
    submitted_order_count: int
    canceled_order_count: int
    rejection_codes: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "approved": self.approved,
            "submitted_order_count": self.submitted_order_count,
            "canceled_order_count": self.canceled_order_count,
            "rejection_codes": self.rejection_codes,
        }


def paper_canary(
    session: Session,
    broker: Broker,
    raw_root: Path,
    risk_config: RiskConfig,
    risk_config_bytes: bytes,
    *,
    stop_file: Path,
    symbol: str = "SPY",
    notional: Decimal = Decimal("25"),
    submit: bool = False,
    trading_enabled: bool = False,
) -> PaperCanaryResult:
    """Evaluate one deterministic buy and optionally submit/cancel it in paper trading."""
    normalized_symbol = symbol.upper().strip()
    if notional <= 0 or not notional.is_finite():
        raise ValueError("canary notional must be finite and positive")
    if submit and not trading_enabled:
        raise RuntimeError("TRADING_ENABLED=true is required for a submitting paper canary")

    now = datetime.now(UTC)
    run_key = f"paper-canary:{now.isoformat()}:{uuid4()}"
    config_hash = hashlib.sha256(risk_config_bytes).hexdigest()
    run = claim_run(session, run_key, now, config_hash)
    if run is None:
        raise RuntimeError("paper canary run key was unexpectedly duplicated")
    directory = raw_root / "paper" / "runs" / run.id
    try:
        directory.mkdir(parents=True, exist_ok=False)
        resolved = directory.resolve()
        if not resolved.is_relative_to(raw_root.resolve()):
            raise RuntimeError("canary artifact directory escaped the configured raw-data root")
        run.raw_artifact_path = str(resolved)
        session.commit()
        (directory / "risk_config.yaml").write_bytes(risk_config_bytes)
        event(
            session,
            run.id,
            "PAPER_CANARY_STARTED",
            metadata={"symbol": normalized_symbol, "notional": str(notional), "submit": submit},
        )

        account = broker.get_account()
        verify_paper_broker_configuration(
            broker,
            account,
            run_id=run.id,
            component="paper_canary",
        )
        positions = tuple(broker.get_positions())
        open_orders = tuple(broker.get_open_orders())
        reconciliation_before = Reconciler(broker, session).reconcile()
        _write_json(
            directory / "reconciliation_before.json",
            reconciliation_before.model_dump(mode="json"),
        )
        if reconciliation_before.issues:
            codes = ", ".join(item.code for item in reconciliation_before.issues)
            raise RuntimeError(f"paper canary reconciliation reported issues: {codes}")
        if open_orders:
            raise RuntimeError("paper canary requires no open broker orders")
        snapshot(session, account, list(positions), run.id, snapshot_type="CANARY")

        quote = broker.get_quote(normalized_symbol)
        canary_limit = (quote.bid * Decimal("0.99")).quantize(Decimal("0.01"))
        proposal = TradeProposal(
            symbol=normalized_symbol,
            action="BUY",
            target_notional_usd=notional,
            confidence=1,
            time_horizon="days",
            rationale="Deterministic operator-requested paper execution canary.",
            catalysts=["Paper execution integration test"],
            key_risks=["The small canary may fill before cancellation"],
            invalidation_conditions=["Any deterministic risk rejection"],
            max_acceptable_price=canary_limit,
        )
        persist_trade_proposal(session, run.id, proposal)
        _write_json(directory / "canary_proposal.json", proposal.model_dump(mode="json"))

        pipeline = PaperRiskExecutionPipeline(
            session,
            broker,
            risk_config,
            trading_enabled=trading_enabled,
            stop_file=stop_file,
        )
        risk_result = pipeline.run(
            run_id=run.id,
            run_directory=directory,
            account=account,
            positions=positions,
            open_orders=open_orders,
            proposals=(proposal,),
            allowed_symbols=frozenset({normalized_symbol}),
            allow_execution=submit,
        )
        decision = risk_result.decisions[0]
        canceled = 0
        for order in risk_result.submitted_orders:
            if canonical_order_status(order.status) in {"ACCEPTED", "PARTIALLY_FILLED"}:
                broker.cancel_order(order.id)
                canceled += 1
        reconciliation_after = Reconciler(broker, session).reconcile(run_id=run.id)
        _write_json(
            directory / "canary_reconciliation_after.json",
            reconciliation_after.model_dump(mode="json"),
        )
        if reconciliation_after.issues:
            codes = ", ".join(item.code for item in reconciliation_after.issues)
            raise RuntimeError(f"paper canary post-cancel reconciliation reported issues: {codes}")

        result = PaperCanaryResult(
            run_id=run.id,
            approved=decision.approved,
            submitted_order_count=len(risk_result.submitted_orders),
            canceled_order_count=canceled,
            rejection_codes=tuple(decision.rejection_codes),
        )
        _write_json(directory / "canary_summary.json", result.summary())
        manifest = {
            path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*")
            if path.is_file()
        }
        _write_json(directory / "manifest.json", manifest)
        event(session, run.id, "PAPER_CANARY_COMPLETED", metadata=result.summary())
        run.status = "COMPLETED"
        run.completed_at = datetime.now(UTC)
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        run.status = "FAILED"
        run.error_summary = str(exc)
        run.completed_at = datetime.now(UTC)
        session.commit()
        event(session, run.id, "FAILED", str(exc))
        raise


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, default=str, indent=2, sort_keys=True),
        encoding="utf-8",
    )
