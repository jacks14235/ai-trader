"""Evaluate strategy variants against their own simulated books.

Each active book runs the daily trader under its own strategy document, against its own cash and
positions, and settles through the deterministic simulator. It reuses the run's candidate slate and
research so a variant is compared with the incumbent on identical information: any divergence in
the resulting equity curves is attributable to the strategy, not to a different view of the world.

Two boundaries hold this apart from the live line:

- This pipeline accepts a `MarketDataSource`, never a `Broker`. It can read quotes and cannot
  submit, cancel, or reconcile anything.
- Every proposal, risk decision, fill, and snapshot it writes carries a `book_id`. Readers of the
  live record filter those out, so a variant's activity can never be mistaken for the portfolio's.

Authorization is not reimplemented. A book's proposals go through the same `risk.engine.evaluate`
as the portfolio's, against a context built from the book's own state, so a variant cannot escape
the human-owned risk policy by being simulated.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.codex_cli import CodexCLIProvider, StructuredReasoningProvider
from trader.agent.config import AgentConfig, load_agent_config, resolved_prompt_path
from trader.agent.invocation import WorkflowStep, invoke_role, resolve_role
from trader.agent.models import TradeProposal
from trader.agent.reasoning import (
    DailyAgentContext,
    DailyDecision,
    assemble_daily_context,
    validate_daily_decision,
)
from trader.books.models import (
    BookRunSummary,
    BookState,
    FillAssumptions,
    MarketDataSource,
    SimulatedFillResult,
)
from trader.books.service import (
    book_fills,
    list_books,
    load_book_state,
    normalize_book_name,
    persist_simulated_fill,
    sync_strategy_document,
)
from trader.books.simulator import apply_fill, simulate_fill
from trader.broker.models import Quote
from trader.ledger.service import record_performance_snapshot
from trader.persistence.models import Book, SimulatedFill, TradeProposalRecord
from trader.persistence.repositories import persist_risk_decision, persist_trade_proposal
from trader.research.service import ResearchRunResult
from trader.risk.config import RiskConfig, load_risk_config
from trader.risk.engine import evaluate
from trader.risk.models import RiskContext, RiskDecision
from trader.risk.runtime import policy_hash
from trader.settings import Settings
from trader.universe.models import UniverseScan

CENT = Decimal("0.01")


@dataclass(frozen=True)
class BookFailure:
    """One book's evaluation that did not complete, isolated from the rest of the run."""

    book_name: str
    reason: str

    def summary(self) -> dict[str, object]:
        return {"book": self.book_name, "reason": self.reason}


@dataclass(frozen=True)
class BookEvaluationResult:
    summaries: tuple[BookRunSummary, ...]
    failures: tuple[BookFailure, ...]

    def summary(self) -> dict[str, object]:
        return {
            "book_count": len(self.summaries),
            "failure_count": len(self.failures),
            "books": [item.summary() for item in self.summaries],
            "failures": [item.summary() for item in self.failures],
        }


class BookEvaluationPipeline:
    """Run every active book over one daily run's slate and research."""

    def __init__(
        self,
        session: Session,
        config: AgentConfig,
        market_data: MarketDataSource,
        risk_config: RiskConfig,
        *,
        prompt: str,
        portfolio_policy: str,
        provider: StructuredReasoningProvider,
        assumptions: FillAssumptions | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.market_data = market_data
        self.risk_config = risk_config
        self.prompt = prompt
        self.portfolio_policy = portfolio_policy
        self.provider = provider
        self.assumptions = assumptions or FillAssumptions()
        self.role, self.profile = resolve_role(config, "daily_trader")

    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        as_of: datetime,
        scan: UniverseScan,
        research: ResearchRunResult,
        allowed_symbols: frozenset[str],
    ) -> BookEvaluationResult:
        """Evaluate each active book, isolating a failure to the book that caused it.

        A book is a shadow experiment with no path to the broker, so one failing must not abort a
        run that has already executed the portfolio's real decisions. Failures are recorded and
        returned rather than raised.
        """
        books = list_books(self.session, status="active")
        summaries: list[BookRunSummary] = []
        failures: list[BookFailure] = []
        directory = run_directory / "books"
        directory.mkdir(parents=True, exist_ok=True)

        for book in books:
            try:
                summary = self._evaluate(
                    book=book,
                    run_id=run_id,
                    run_directory=run_directory,
                    directory=directory / book.name,
                    as_of=as_of,
                    scan=scan,
                    research=research,
                    allowed_symbols=allowed_symbols,
                )
            except Exception as error:  # noqa: BLE001 - isolation is the point; see docstring
                failures.append(BookFailure(book_name=book.name, reason=f"{error}"))
                self.session.rollback()
                continue
            summaries.append(summary)

        result = BookEvaluationResult(summaries=tuple(summaries), failures=tuple(failures))
        _write_json(run_directory / "books_summary.json", result.summary())
        return result

    def _evaluate(
        self,
        *,
        book: Book,
        run_id: str,
        run_directory: Path,
        directory: Path,
        as_of: datetime,
        scan: UniverseScan,
        research: ResearchRunResult,
        allowed_symbols: frozenset[str],
    ) -> BookRunSummary:
        directory.mkdir(parents=True, exist_ok=True)
        strategy, document_changed = sync_strategy_document(self.session, book, as_of=as_of)
        state = load_book_state(self.session, book, as_of=as_of)

        held = tuple(position.symbol for position in state.positions)
        prices = self._prices(held)
        context = assemble_daily_context(
            self.session,
            run_id=run_id,
            as_of=as_of,
            strategy=strategy,
            portfolio_policy=self.portfolio_policy,
            account=state.account(prices),
            positions=state.broker_positions(prices),
            open_orders=(),
            scan=scan,
            research=research,
            role=self.role,
            book_id=book.id,
        )

        decision, invocation_id = self._invoke(
            book=book,
            run_id=run_id,
            run_directory=run_directory,
            context=context,
        )
        proposals = decision.proposals
        risk_decisions = self._authorize(
            run_id=run_id,
            state=state,
            context=context,
            proposals=proposals,
            allowed_symbols=allowed_symbols,
            as_of=as_of,
        )

        fills, state = self._settle(
            book=book,
            run_id=run_id,
            state=state,
            decisions=risk_decisions,
            as_of=as_of,
        )

        final_prices = self._prices(tuple(position.symbol for position in state.positions))
        account = state.account(final_prices)
        record_performance_snapshot(
            self.session,
            run_id=run_id,
            account=account,
            as_of=as_of,
            book_id=book.id,
        )

        summary = BookRunSummary(
            book_id=book.id,
            name=book.name,
            run_id=run_id,
            invocation_id=invocation_id,
            decision_status=decision.status,
            proposal_count=len(proposals),
            approved_count=sum(item.approved for item in risk_decisions),
            rejected_count=sum(not item.approved for item in risk_decisions),
            filled_count=sum(item.filled for item in fills),
            unfilled_outcomes=tuple(
                sorted({item.outcome for item in fills if not item.filled})
            ),
            equity=account.equity,
            cash=account.cash,
            position_count=len(state.positions),
        )
        _write_json(
            directory / "book_run.json",
            {
                **summary.summary(),
                "strategy_content_hash": book.strategy_content_hash,
                "strategy_document_changed": document_changed,
                "assumptions": self.assumptions.model_dump(mode="json"),
                "fills": [item.summary() for item in fills],
                "positions": [
                    {
                        "symbol": position.symbol,
                        "qty": str(position.qty),
                        "average_entry_price": str(position.average_entry_price),
                    }
                    for position in state.positions
                ],
            },
        )
        return summary

    def _invoke(
        self,
        *,
        book: Book,
        run_id: str,
        run_directory: Path,
        context: DailyAgentContext,
    ) -> tuple[DailyDecision, str]:
        def persist(decision: DailyDecision, invocation_id: str) -> None:
            for proposal in decision.proposals:
                persist_trade_proposal(
                    self.session,
                    run_id,
                    proposal,
                    agent_invocation_id=invocation_id,
                    book_id=book.id,
                )

        result = invoke_role(
            self.session,
            self.config,
            run_id=run_id,
            run_directory=run_directory,
            workflow=WorkflowStep(role="daily_trader", step=_step_name(book.name)),
            prompt=self.prompt,
            context=context,
            output_model=DailyDecision,
            admitted_evidence_ids=context.admitted_evidence_ids,
            provider=self.provider,
            validate=lambda decision: validate_daily_decision(decision, context),
            on_output=persist,
        )
        return result.output, result.invocation_id

    def _authorize(
        self,
        *,
        run_id: str,
        state: BookState,
        context: DailyAgentContext,
        proposals: tuple[TradeProposal, ...],
        allowed_symbols: frozenset[str],
        as_of: datetime,
    ) -> tuple[RiskDecision, ...]:
        if not proposals:
            return ()
        symbols = tuple(sorted({proposal.symbol for proposal in proposals}))
        quotes = {symbol: self.market_data.get_quote(symbol) for symbol in symbols}
        assets = {symbol: self.market_data.get_asset(symbol) for symbol in symbols}
        clock = self.market_data.get_clock()

        prices = self._prices(tuple(position.symbol for position in state.positions))
        orders_today, exposure_today, trades_today = self._activity(state.book_id, as_of)
        risk_context = RiskContext(
            as_of=as_of,
            account=state.account(prices),
            positions=list(state.broker_positions(prices)),
            open_orders=[],
            quotes=quotes,
            assets=assets,
            proposals=list(proposals),
            policy=self.risk_config.to_policy(allowed_symbols=allowed_symbols),
            daily_drawdown_pct=Decimal("0"),
            weekly_drawdown_pct=Decimal("0"),
            peak_drawdown_pct=self._peak_drawdown(state, prices),
            orders_today=orders_today,
            daily_new_gross_exposure_usd=exposure_today,
            trades_per_symbol_today=trades_today,
            symbols_traded_today=set(trades_today),
            broker_state_known=True,
            open_order_state_known=True,
            portfolio_state_known=True,
            market_is_open=clock.is_open,
            paper_options_level_is_provider_managed=False,
        )
        decisions = tuple(evaluate(risk_context))
        effective_hash = policy_hash(risk_context.policy)
        for decision in decisions:
            persist_risk_decision(
                self.session,
                run_id,
                decision,
                policy_hash=effective_hash,
            )
        return decisions

    def _settle(
        self,
        *,
        book: Book,
        run_id: str,
        state: BookState,
        decisions: tuple[RiskDecision, ...],
        as_of: datetime,
    ) -> tuple[tuple[SimulatedFillResult, ...], BookState]:
        results: list[SimulatedFillResult] = []
        for decision in decisions:
            if not decision.approved or decision.normalized_order is None:
                continue
            symbol = decision.normalized_order.symbol
            quote = self._quote(symbol)
            fill = simulate_fill(
                decision=decision,
                quote=quote,
                state=state,
                assumptions=self.assumptions,
                as_of=as_of,
            )
            results.append(fill)
            if fill.filled:
                persist_simulated_fill(
                    self.session,
                    book=book,
                    run_id=run_id,
                    fill=fill,
                    assumptions=self.assumptions,
                    as_of=as_of,
                )
                state = apply_fill(state, fill)
        return tuple(results), state

    def _prices(self, symbols: tuple[str, ...]) -> dict[str, Decimal]:
        """Mark every held symbol to the current mid, refusing to value what cannot be priced."""
        prices: dict[str, Decimal] = {}
        for symbol in sorted(set(symbols)):
            quote = self._quote(symbol)
            if quote is None:
                raise ValueError(f"cannot price {symbol}: no quote is available")
            prices[symbol] = ((quote.bid + quote.ask) / Decimal("2")).quantize(
                CENT,
                rounding=ROUND_HALF_UP,
            )
        return prices

    def _quote(self, symbol: str) -> Quote | None:
        try:
            return self.market_data.get_quote(symbol)
        except Exception:  # noqa: BLE001 - an unpriceable symbol refuses to fill, see simulator
            return None

    def _activity(self, book_id: str, as_of: datetime) -> tuple[int, Decimal, dict[str, int]]:
        """Count what this book already did today, so per-day risk limits bind for a book too."""
        local_date = as_of.astimezone(UTC).date()
        records = self.session.scalars(
            select(SimulatedFill).where(SimulatedFill.book_id == book_id)
        )
        exposure = Decimal("0")
        trades: dict[str, int] = defaultdict(int)
        count = 0
        for record in records:
            moment = record.transaction_time
            if moment.tzinfo is None or moment.utcoffset() is None:
                moment = moment.replace(tzinfo=UTC)
            if moment.astimezone(UTC).date() != local_date:
                continue
            count += 1
            trades[record.symbol.upper()] += 1
            if record.side.lower() == "buy":
                exposure += Decimal(record.qty) * Decimal(record.price)
        return count, exposure, dict(trades)

    def _peak_drawdown(self, state: BookState, prices: dict[str, Decimal]) -> Decimal:
        """Measure the book's drawdown against its own equity peak, not the portfolio's."""
        from trader.persistence.models import PerformanceSnapshot

        equities = [
            Decimal(record.equity)
            for record in self.session.scalars(
                select(PerformanceSnapshot).where(
                    PerformanceSnapshot.book_id == state.book_id,
                    PerformanceSnapshot.period == "daily",
                )
            )
        ]
        current = state.equity(prices)
        equities.append(current)
        peak = max(equities)
        if peak <= 0 or current >= peak:
            return Decimal("0")
        return (peak - current) * Decimal("100") / peak


def book_ledger(session: Session, book: Book) -> dict[str, object]:
    """Return one book's settled history, for review rather than reasoning."""
    fills = book_fills(session, book)
    proposals = session.scalars(
        select(TradeProposalRecord)
        .where(TradeProposalRecord.book_id == book.id)
        .order_by(TradeProposalRecord.created_at)
    ).all()
    return {
        "book": book.name,
        "status": book.status,
        "strategy_document_path": book.strategy_document_path,
        "strategy_content_hash": book.strategy_content_hash,
        "starting_cash": book.starting_cash,
        "proposal_count": len(proposals),
        "fill_count": len(fills),
        "fills": [
            {
                "run_id": record.run_id,
                "symbol": record.symbol,
                "side": record.side,
                "qty": record.qty,
                "price": record.price,
                "transaction_time": record.transaction_time.isoformat(),
            }
            for record in fills
        ],
    }


def configured_book_evaluation_pipeline(
    settings: Settings,
    session: Session,
    market_data: MarketDataSource,
    *,
    provider: StructuredReasoningProvider | None = None,
) -> BookEvaluationPipeline | None:
    """Build the book evaluator only when daily reasoning is itself enabled.

    Books reuse the daily trader. If that role is not allowed to run, a variant has nothing to
    invoke, and constructing a pipeline that would sit idle is more confusing than returning None.
    """
    if not list_books(session, status="active"):
        return None
    config = load_agent_config(settings.trader_agents_config)
    if not settings.trader_reasoning_enabled or not config.automatic_daily_run:
        return None
    prompt_path = resolved_prompt_path(
        settings.trader_agents_config, config.roles["daily_trader"]
    )
    return BookEvaluationPipeline(
        session,
        config,
        market_data,
        load_risk_config(settings.trader_risk_config),
        prompt=prompt_path.read_text(encoding="utf-8"),
        portfolio_policy=settings.trader_portfolio_policy.read_text(encoding="utf-8"),
        provider=provider or CodexCLIProvider(),
    )


def _step_name(book_name: str) -> str:
    """Name the workflow step after the book so both may invoke the trader in one run."""
    return "book_" + normalize_book_name(book_name).replace("-", "_")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, default=str, indent=2, sort_keys=True),
        encoding="utf-8",
    )
