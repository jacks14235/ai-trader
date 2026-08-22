"""Deterministic runtime assembly and paper-only execution for approved proposals."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.models import TradeProposal
from trader.broker.base import Broker
from trader.broker.models import Account, BrokerOrder, Position, Quote, TradableAsset
from trader.execution.executor import Executor
from trader.execution.reconciliation import Reconciler, ReconciliationReport
from trader.persistence.models import BrokerOrderRecord, MarketSnapshot, PortfolioSnapshot
from trader.persistence.repositories import persist_risk_decision
from trader.risk.config import RiskConfig
from trader.risk.engine import evaluate
from trader.risk.models import RiskContext, RiskDecision, RiskPolicy

EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DailyRiskExecutionResult:
    decisions: tuple[RiskDecision, ...]
    submitted_orders: tuple[BrokerOrder, ...]
    policy_hash: str | None
    execution_enabled: bool
    reconciliation: ReconciliationReport | None

    def summary(self) -> dict[str, object]:
        return {
            "proposal_count": len(self.decisions),
            "approved_count": sum(item.approved for item in self.decisions),
            "rejected_count": sum(not item.approved for item in self.decisions),
            "submitted_order_count": len(self.submitted_orders),
            "execution_enabled": self.execution_enabled,
            "policy_hash": self.policy_hash,
        }


class DailyRiskExecutionPipeline(Protocol):
    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        account: Account,
        positions: tuple[Position, ...],
        open_orders: tuple[BrokerOrder, ...],
        proposals: tuple[TradeProposal, ...],
        allowed_symbols: frozenset[str],
        allow_execution: bool,
    ) -> DailyRiskExecutionResult: ...


class PaperRiskExecutionPipeline:
    """Build a complete risk snapshot, persist decisions, and optionally submit paper orders."""

    def __init__(
        self,
        session: Session,
        broker: Broker,
        risk_config: RiskConfig,
        *,
        trading_enabled: bool,
        stop_file: Path,
    ) -> None:
        if not broker.is_paper:
            raise ValueError("paper broker required")
        self.session = session
        self.broker = broker
        self.risk_config = risk_config
        self.trading_enabled = trading_enabled
        self.stop_file = stop_file

    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        account: Account,
        positions: tuple[Position, ...],
        open_orders: tuple[BrokerOrder, ...],
        proposals: tuple[TradeProposal, ...],
        allowed_symbols: frozenset[str],
        allow_execution: bool,
    ) -> DailyRiskExecutionResult:
        execution_enabled = self.trading_enabled and allow_execution
        if not proposals:
            result = DailyRiskExecutionResult(
                decisions=(),
                submitted_orders=(),
                policy_hash=None,
                execution_enabled=execution_enabled,
                reconciliation=None,
            )
            _write_json(run_directory / "risk_summary.json", result.summary())
            return result

        context, clock_payload = self._context(
            run_id=run_id,
            account=account,
            positions=positions,
            open_orders=open_orders,
            proposals=proposals,
            allowed_symbols=allowed_symbols,
        )
        effective_policy_hash = policy_hash(context.policy)
        _write_json(run_directory / "risk_context.json", context.model_dump(mode="json"))
        _write_json(run_directory / "market_clock.json", clock_payload)

        decisions = tuple(evaluate(context))
        for decision in decisions:
            persist_risk_decision(
                self.session,
                run_id,
                decision,
                policy_hash=effective_policy_hash,
            )
        _write_json(
            run_directory / "risk_decisions.json",
            [item.model_dump(mode="json") for item in decisions],
        )

        submitted: list[BrokerOrder] = []
        reconciliation: ReconciliationReport | None = None
        if execution_enabled:
            executor = Executor(
                self.broker,
                self.session,
                trading_enabled=True,
                stop_file=self.stop_file,
            )
            for decision in decisions:
                if decision.approved:
                    submitted.append(executor.submit(run_id, decision))
            reconciliation = Reconciler(self.broker, self.session).reconcile()
            _write_json(
                run_directory / "reconciliation_after.json",
                reconciliation.model_dump(mode="json"),
            )
            if reconciliation.issues:
                issue_codes = ", ".join(issue.code for issue in reconciliation.issues)
                raise RuntimeError(f"post-execution reconciliation reported issues: {issue_codes}")

        _write_json(
            run_directory / "orders_submitted.json",
            [item.model_dump(mode="json") for item in submitted],
        )
        result = DailyRiskExecutionResult(
            decisions=decisions,
            submitted_orders=tuple(submitted),
            policy_hash=effective_policy_hash,
            execution_enabled=execution_enabled,
            reconciliation=reconciliation,
        )
        _write_json(run_directory / "risk_summary.json", result.summary())
        return result

    def _context(
        self,
        *,
        run_id: str,
        account: Account,
        positions: tuple[Position, ...],
        open_orders: tuple[BrokerOrder, ...],
        proposals: tuple[TradeProposal, ...],
        allowed_symbols: frozenset[str],
    ) -> tuple[RiskContext, dict[str, object]]:
        clock = self.broker.get_clock()
        symbols = tuple(sorted({proposal.symbol for proposal in proposals}))
        quotes: dict[str, Quote] = {}
        assets: dict[str, TradableAsset] = {}
        for symbol in symbols:
            quotes[symbol] = self.broker.get_quote(symbol)
            assets[symbol] = self.broker.get_asset(symbol)

        timestamps = [datetime.now(UTC), _utc(clock.timestamp)]
        timestamps.extend(_utc(quote.timestamp) for quote in quotes.values())
        as_of = max(timestamps)
        self._persist_market_snapshots(run_id, quotes)
        daily_drawdown, weekly_drawdown, peak_drawdown = self._drawdowns(account, as_of)
        orders_today, exposure_today, trades_per_symbol = self._activity(as_of)
        policy = self.risk_config.to_policy(allowed_symbols=allowed_symbols)
        context = RiskContext(
            as_of=as_of,
            account=account,
            positions=list(positions),
            open_orders=list(open_orders),
            quotes=quotes,
            assets=assets,
            proposals=list(proposals),
            policy=policy,
            daily_drawdown_pct=daily_drawdown,
            weekly_drawdown_pct=weekly_drawdown,
            peak_drawdown_pct=peak_drawdown,
            orders_today=orders_today,
            daily_new_gross_exposure_usd=exposure_today,
            trades_per_symbol_today=trades_per_symbol,
            symbols_traded_today=set(trades_per_symbol),
            broker_state_known=True,
            open_order_state_known=True,
            portfolio_state_known=True,
            market_is_open=clock.is_open,
            paper_options_level_is_provider_managed=(
                self.broker.paper_options_level_is_provider_managed
            ),
        )
        return context, clock.model_dump(mode="json")

    def _persist_market_snapshots(self, run_id: str, quotes: dict[str, Quote]) -> None:
        for quote in quotes.values():
            raw_json = quote.model_dump_json()
            record = MarketSnapshot(
                run_id=run_id,
                symbol=quote.symbol,
                quote_at=quote.timestamp,
                bid=str(quote.bid),
                ask=str(quote.ask),
                average_daily_dollar_volume=(
                    str(quote.average_daily_dollar_volume)
                    if quote.average_daily_dollar_volume is not None
                    else None
                ),
                source=quote.feed,
                raw_json=raw_json,
                content_hash=hashlib.sha256(raw_json.encode()).hexdigest(),
            )
            self.session.add(record)
        self.session.commit()

    def _drawdowns(
        self,
        account: Account,
        as_of: datetime,
    ) -> tuple[Decimal, Decimal, Decimal]:
        local = as_of.astimezone(EASTERN)
        day_start = datetime.combine(local.date(), datetime.min.time(), EASTERN).astimezone(UTC)
        week_start_date = local.date() - timedelta(days=local.weekday())
        week_start = datetime.combine(
            week_start_date,
            datetime.min.time(),
            EASTERN,
        ).astimezone(UTC)
        snapshots = list(
            self.session.scalars(
                select(PortfolioSnapshot).order_by(PortfolioSnapshot.captured_at)
            )
        )
        history = [
            (_utc(item.captured_at), Decimal(item.equity))
            for item in snapshots
            if _utc(item.captured_at) <= as_of
        ]
        history.append((as_of, account.equity))
        return (
            _drawdown(account.equity, [equity for at, equity in history if at >= day_start]),
            _drawdown(account.equity, [equity for at, equity in history if at >= week_start]),
            _drawdown(account.equity, [equity for _at, equity in history]),
        )

    def _activity(self, as_of: datetime) -> tuple[int, Decimal, dict[str, int]]:
        local_date = as_of.astimezone(EASTERN).date()
        records = list(
            self.session.scalars(
                select(BrokerOrderRecord).order_by(BrokerOrderRecord.created_at)
            )
        )
        today = [
            item
            for item in records
            if _utc(item.created_at).astimezone(EASTERN).date() == local_date
        ]
        exposure = Decimal("0")
        trades: dict[str, int] = defaultdict(int)
        for item in today:
            if item.status.upper() == "REJECTED":
                continue
            trades[item.symbol.upper()] += 1
            if item.side.lower() == "buy" and item.notional is not None:
                exposure += Decimal(item.notional)
        return len(today), exposure, dict(trades)


def policy_hash(policy: RiskPolicy) -> str:
    value = policy.model_dump(mode="json")
    value["allowed_symbols"] = sorted(policy.allowed_symbols)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _drawdown(current: Decimal, equities: list[Decimal]) -> Decimal:
    peak = max(equities, default=current)
    if peak <= 0 or current >= peak:
        return Decimal("0")
    return (peak - current) * Decimal("100") / peak


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, default=str, indent=2, sort_keys=True),
        encoding="utf-8",
    )
