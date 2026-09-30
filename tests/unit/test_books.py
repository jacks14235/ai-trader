import hashlib
import inspect
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session
from typer.main import get_command

from trader.agent.catalog import load_pipeline_catalog
from trader.agent.codex_cli import InvocationResponse
from trader.agent.config import load_agent_config
from trader.agent.models import TradeProposal
from trader.agent.reasoning import DailyDecision, DailyUpdate
from trader.agent.runner import daily_run
from trader.books.models import BookState, FillAssumptions, SimulatedFillResult
from trader.books.runtime import BookEvaluationPipeline, BookRunSummary
from trader.books.service import (
    MAX_ACTIVE_BOOKS,
    list_books,
    load_book_state,
    open_book,
    persist_simulated_fill,
    set_book_status,
)
from trader.books.simulator import apply_fill, simulate_fill
from trader.broker.models import (
    Account,
    BrokerFill,
    BrokerOrder,
    MarketClock,
    OrderQueryStatus,
    Position,
    Quote,
    TradableAsset,
)
from trader.cli import app
from trader.ledger.history import load_recent_decisions
from trader.ledger.performance import load_weekly_performance
from trader.ledger.service import record_decision_ledger, record_performance_snapshot
from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    Book,
    BrokerOrderRecord,
    PerformanceSnapshot,
    Run,
    SimulatedFill,
    TradeProposalRecord,
)
from trader.persistence.repositories import (
    PersistenceConflictError,
    persist_research_item,
    persist_trade_proposal,
)
from trader.research.artifacts import ResearchArtifact
from trader.research.collection import ResearchCollection
from trader.research.models import ResearchPlan, ResearchRequest
from trader.research.service import ResearchRunResult
from trader.risk.config import load_risk_config
from trader.risk.models import NormalizedOrder, RiskDecision
from trader.universe.models import (
    CandidateSignal,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)


class DailyRunBroker:
    """Enough of a broker for `daily_run` to reconcile a clean paper account."""

    def __init__(self) -> None:
        self.submitted = 0

    def get_account(self) -> Account:
        return Account(equity="2000", cash="1000", buying_power="1000")

    def get_positions(self) -> list[Position]:
        return [Position(symbol="SPY", qty="1", market_value="600", current_price="600")]

    def get_open_orders(self) -> list[BrokerOrder]:
        return []

    @property
    def is_paper(self) -> bool:
        return True

    @property
    def paper_options_level_is_provider_managed(self) -> bool:
        return False

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]:
        del status, after, until, limit
        return []

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        del client_order_id
        return None

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]:
        del after, until
        return []


class DailyRunScanner:
    def scan(
        self,
        *,
        as_of: datetime,
        portfolio_symbols: tuple[str, ...],
    ) -> UniverseScan:
        del portfolio_symbols
        asset = UniverseAsset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class="us_equity",
            status="active",
            tradable=True,
        )
        return UniverseScan(
            as_of=as_of,
            asset_content_hash="a" * 64,
            eligible_assets=(asset,),
            candidates=(
                ResearchCandidate(
                    symbol="SPY",
                    score=100,
                    asset=asset,
                    signals=(CandidateSignal(source="PORTFOLIO"),),
                ),
            ),
            most_active_volume_updated_at=as_of,
            most_active_trades_updated_at=as_of,
            market_movers_updated_at=as_of,
            skipped_screener_symbols=0,
        )


class DailyRunResearch:
    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        scan: UniverseScan,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchRunResult:
        del run_id, portfolio_symbols, event_symbols
        question = ResearchRequest.create(
            symbol="SPY",
            question_type="MARKET_CONTEXT",
            query="What changed?",
            window_start=scan.as_of - timedelta(hours=1),
            window_end=scan.as_of,
            priority=100,
        )
        payload = b"{}"
        (run_directory / "research" / "alpaca").mkdir(parents=True)
        (run_directory / "research" / "alpaca" / "document.json").write_bytes(payload)
        return ResearchRunResult(
            plan=ResearchPlan(
                as_of=scan.as_of,
                candidate_symbols=("SPY",),
                deep_symbols=("SPY",),
                questions=(question,),
            ),
            collection=ResearchCollection(batches=(), request_count=1, response_bytes=2),
            artifacts={
                "document": ResearchArtifact(
                    relative_path="alpaca/document.json",
                    content_hash=hashlib.sha256(payload).hexdigest(),
                    byte_count=2,
                    created=True,
                )
            },
            persisted_research_ids=("research-1",),
            elapsed_seconds=0.1,
        )


PROJECT_ROOT = Path(__file__).parents[2]
AS_OF = datetime(2026, 8, 22, 19, 15, tzinfo=UTC)
STRATEGY = "# Variant\n\nKeep any single new position under ten percent of equity.\n"


class BookMarket:
    def __init__(self, *, ask: Decimal = Decimal("10"), bid: Decimal = Decimal("9.99")) -> None:
        self.ask = ask
        self.bid = bid
        self.submitted = 0

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            bid=self.bid,
            ask=self.ask,
            timestamp=AS_OF,
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

    def get_clock(self) -> MarketClock:
        return MarketClock(
            timestamp=AS_OF,
            is_open=True,
            next_open=AS_OF + timedelta(days=1),
            next_close=AS_OF + timedelta(hours=1),
        )


class BookProvider:
    provider_name = "test"

    def __init__(self, decision: DailyDecision) -> None:
        self.decision = decision

    def invoke(self, **kwargs: object) -> InvocationResponse:
        del kwargs
        return InvocationResponse(self.decision.model_dump_json(), "stdout", "")


class FakeBookPipeline:
    def __init__(self, summary: BookRunSummary) -> None:
        self.summary = summary
        self.calls = 0

    def run(self, **kwargs: object) -> object:
        del kwargs
        self.calls += 1
        from trader.books.runtime import BookEvaluationResult

        return BookEvaluationResult(summaries=(self.summary,), failures=())


def test_a_simulated_buy_crosses_the_ask_and_a_sell_hits_the_bid() -> None:
    state = BookState(
        book_id="book",
        name="control",
        starting_cash=Decimal("2000"),
        cash=Decimal("2000"),
    )
    buy = simulate_fill(
        decision=_approved("buy", qty=Decimal("10"), limit=Decimal("10.50")),
        quote=_quote(bid=Decimal("9.90"), ask=Decimal("10.00")),
        state=state,
        assumptions=FillAssumptions(),
        as_of=AS_OF,
    )
    assert buy.filled
    assert buy.price == Decimal("10.00")
    held = apply_fill(state, buy)
    assert held.cash == Decimal("1900")
    assert held.positions[0].qty == Decimal("10")
    assert held.realized_pnl == Decimal("0")

    sell = simulate_fill(
        decision=_approved("sell", qty=Decimal("10"), limit=Decimal("9.50")),
        quote=_quote(bid=Decimal("11.00"), ask=Decimal("11.10")),
        state=held,
        assumptions=FillAssumptions(),
        as_of=AS_OF,
    )
    assert sell.price == Decimal("11.00")
    closed = apply_fill(held, sell)
    assert closed.positions == ()
    assert closed.cash == Decimal("2010")
    assert closed.realized_pnl == Decimal("10")


def test_simulation_refuses_stale_quotes_unmarketable_limits_and_missing_cash() -> None:
    state = BookState(
        book_id="book",
        name="control",
        starting_cash=Decimal("50"),
        cash=Decimal("50"),
    )
    stale = simulate_fill(
        decision=_approved("buy", qty=Decimal("1"), limit=Decimal("20")),
        quote=_quote(bid=Decimal("9.90"), ask=Decimal("10.00"), age=timedelta(hours=1)),
        state=state,
        assumptions=FillAssumptions(max_quote_age_seconds=60),
        as_of=AS_OF,
    )
    assert stale.outcome == "STALE_QUOTE"

    unmarketable = simulate_fill(
        decision=_approved("buy", qty=Decimal("1"), limit=Decimal("9.50")),
        quote=_quote(bid=Decimal("9.90"), ask=Decimal("10.00")),
        state=state,
        assumptions=FillAssumptions(),
        as_of=AS_OF,
    )
    assert unmarketable.outcome == "LIMIT_NOT_MARKETABLE"

    expensive = simulate_fill(
        decision=_approved("buy", qty=Decimal("10"), limit=Decimal("20")),
        quote=_quote(bid=Decimal("9.90"), ask=Decimal("10.00")),
        state=state,
        assumptions=FillAssumptions(),
        as_of=AS_OF,
    )
    assert expensive.outcome == "INSUFFICIENT_CASH"


def test_a_book_is_derived_from_its_fills_and_cannot_be_settled_twice(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    book = _open(session, tmp_path)
    fill = SimulatedFillResult(
        proposal_id=str(uuid4()),
        symbol="SPY",
        side="buy",
        outcome="FILLED",
        qty=Decimal("10"),
        price=Decimal("10"),
        commission=Decimal("0"),
    )
    persist_simulated_fill(
        session,
        book=book,
        run_id=_run(session).id,
        fill=fill,
        assumptions=FillAssumptions(),
        as_of=AS_OF,
    )
    persist_simulated_fill(
        session,
        book=book,
        run_id=_run(session, key="daily:2026-08-23").id,
        fill=fill,
        assumptions=FillAssumptions(),
        as_of=AS_OF,
    )
    assert session.scalars(select(SimulatedFill)).all().__len__() == 1
    state = load_book_state(session, book)
    assert state.cash == Decimal("1900")
    assert state.positions[0].symbol == "SPY"

    conflicting = fill.model_copy(update={"qty": Decimal("11")})
    with pytest.raises(PersistenceConflictError, match="simulated fill conflict"):
        persist_simulated_fill(
            session,
            book=book,
            run_id=_run(session, key="daily:2026-08-24").id,
            fill=conflicting,
            assumptions=FillAssumptions(),
            as_of=AS_OF,
        )


def test_the_active_roster_is_capped_and_resume_honors_the_cap(tmp_path: Path) -> None:
    session = _session(tmp_path)
    opened = [
        _open(session, tmp_path, name=f"book-{index:02d}", cash="100")
        for index in range(MAX_ACTIVE_BOOKS)
    ]
    with pytest.raises(ValueError, match="already active"):
        _open(session, tmp_path, name="one-more", cash="100")
    set_book_status(session, opened[0], "paused")
    extra = _open(session, tmp_path, name="replacement", cash="100")
    assert extra.status == "active"
    with pytest.raises(ValueError, match="already active"):
        set_book_status(session, opened[0], "active")
    assert len(list_books(session, status="active")) == MAX_ACTIVE_BOOKS


def test_book_proposals_never_contaminate_the_live_decision_line(tmp_path: Path) -> None:
    session = _session(tmp_path)
    book = _open(session, tmp_path)
    live_run = _run(
        session,
        key="daily:2026-08-20:15:15:America/New_York",
        scheduled_for=datetime(2026, 8, 20, 19, 15, tzinfo=UTC),
    )
    live = TradeProposal(
        symbol="SPY",
        action="BUY",
        target_notional_usd=Decimal("100"),
        confidence=0.6,
        time_horizon="weeks",
        rationale="Live entry.",
        invalidation_conditions=["Support fails"],
        evidence_ids=[],
        max_acceptable_price=Decimal("650"),
    )
    persist_trade_proposal(session, live_run.id, live)
    record_decision_ledger(
        session,
        run_id=live_run.id,
        as_of=live_run.scheduled_for,
        proposals=(live,),
        approved_proposal_ids=frozenset({str(live.proposal_id)}),
    )
    book_run = _run(session, key="daily:2026-08-21:15:15:America/New_York")
    variant = TradeProposal(
        symbol="QQQ",
        action="BUY",
        target_notional_usd=Decimal("100"),
        confidence=0.6,
        time_horizon="weeks",
        rationale="Variant entry.",
        invalidation_conditions=["Support fails"],
        evidence_ids=[],
        max_acceptable_price=Decimal("650"),
    )
    persist_trade_proposal(session, book_run.id, variant, book_id=book.id)
    record_performance_snapshot(
        session,
        run_id=book_run.id,
        account=Account(equity="2000", cash="2000", buying_power="2000"),
        as_of=book_run.scheduled_for,
        book_id=book.id,
    )

    live_memory = load_recent_decisions(
        session,
        exclude_run_id="review",
        as_of=AS_OF + timedelta(days=1),
    )
    symbols = {outcome.symbol for record in live_memory for outcome in record.proposals}
    assert "SPY" in symbols
    assert "QQQ" not in symbols

    book_memory = load_recent_decisions(
        session,
        exclude_run_id="review",
        as_of=AS_OF + timedelta(days=1),
        book_id=book.id,
    )
    book_symbols = {outcome.symbol for record in book_memory for outcome in record.proposals}
    assert book_symbols == {"QQQ"}

    performance = load_weekly_performance(
        session,
        period_start=datetime(2026, 8, 15, 4, tzinfo=UTC),
        period_end=datetime(2026, 8, 22, 4, tzinfo=UTC),
    )
    assert performance.proposal_count == 1
    assert performance.equity_points == ()


def test_the_book_pipeline_settles_against_its_own_cash_and_never_submits(
    tmp_path: Path,
) -> None:
    session, run, scan, research, research_id = _research_state(tmp_path)
    book = _open(session, tmp_path, cash="2000")
    market = BookMarket()
    decision = _decision(research_id)
    pipeline = BookEvaluationPipeline(
        session,
        load_agent_config(PROJECT_ROOT / "config" / "agents.yaml"),
        market,
        load_risk_config(PROJECT_ROOT / "config" / "risk.yaml"),
        prompt="Return structured output only.",
        portfolio_policy="# Policy\n",
        provider=BookProvider(decision),
        catalog=_catalog(),
        project_root=tmp_path,
    )

    result = pipeline.run(
        run_id=run.id,
        run_directory=tmp_path / "raw",
        as_of=AS_OF,
        scan=scan,
        research=research,
        allowed_symbols=frozenset({"SPY"}),
    )

    assert result.failures == ()
    assert result.summaries[0].filled_count == 1
    assert market.submitted == 0
    assert session.scalar(select(BrokerOrderRecord)) is None
    snapshot = session.scalar(
        select(PerformanceSnapshot).where(PerformanceSnapshot.book_id == book.id)
    )
    assert snapshot is not None
    proposal = session.scalar(
        select(TradeProposalRecord).where(TradeProposalRecord.book_id == book.id)
    )
    assert proposal is not None
    fill = session.scalar(select(SimulatedFill))
    assert fill is not None
    assert fill.proposal_id == proposal.id
    state = load_book_state(session, book)
    assert state.positions[0].symbol == "SPY"
    assert state.cash < Decimal("2000")


def test_one_book_failing_does_not_block_the_others(tmp_path: Path) -> None:
    session, run, scan, research, research_id = _research_state(tmp_path)
    healthy = _open(session, tmp_path, name="healthy", cash="2000")
    broken = _open(session, tmp_path, name="broken", cash="2000")
    broken.strategy_document_path = str(tmp_path / "missing.md")
    session.add(broken)
    session.commit()
    pipeline = BookEvaluationPipeline(
        session,
        load_agent_config(PROJECT_ROOT / "config" / "agents.yaml"),
        BookMarket(),
        load_risk_config(PROJECT_ROOT / "config" / "risk.yaml"),
        prompt="Return structured output only.",
        portfolio_policy="# Policy\n",
        provider=BookProvider(_decision(research_id)),
        catalog=_catalog(),
        project_root=tmp_path,
    )

    result = pipeline.run(
        run_id=run.id,
        run_directory=tmp_path / "raw",
        as_of=AS_OF,
        scan=scan,
        research=research,
        allowed_symbols=frozenset({"SPY"}),
    )

    assert {item.name for item in result.summaries} == {"healthy"}
    assert result.failures[0].book_name == "broken"
    assert session.scalar(select(SimulatedFill)).book_id == healthy.id  # type: ignore[union-attr]


def test_daily_run_evaluates_books_after_the_live_line_and_never_submits_for_them(
    tmp_path: Path,
) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    summary = BookRunSummary(
        book_id="book",
        name="control",
        run_id="pending",
        decision_status="NO_ACTION",
        equity=Decimal("2000"),
        cash=Decimal("2000"),
    )
    pipeline = FakeBookPipeline(summary)
    run_id = daily_run(
        session,
        DailyRunBroker(),
        tmp_path / "raw",
        b"mode: paper\n",
        AS_OF,
        universe_config_bytes=b"allowed_symbols: [SPY]\nbenchmark_symbols: [SPY]\n",
        candidate_scanner=DailyRunScanner(),
        research_pipeline=DailyRunResearch(),
        book_evaluation_pipeline=pipeline,  # type: ignore[arg-type]
    )

    assert pipeline.calls == 1
    report = (tmp_path / "raw" / "paper" / "runs" / run_id / "daily_report.md").read_text()
    assert "Simulated books" in report
    assert "control" in report
    assert session.scalar(select(BrokerOrderRecord)) is None


def test_the_books_package_cannot_reach_a_broker() -> None:
    for path in (
        PROJECT_ROOT / "src" / "trader" / "books" / "runtime.py",
        PROJECT_ROOT / "src" / "trader" / "books" / "simulator.py",
        PROJECT_ROOT / "src" / "trader" / "books" / "service.py",
    ):
        imports = [
            line for line in path.read_text().splitlines() if line.startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "trader.broker.base" in line]
        assert not [line for line in imports if "trader.execution" in line]
    assert "Broker" not in str(inspect.signature(BookEvaluationPipeline))


def test_books_migration_round_trips_and_guards_recorded_history(tmp_path: Path) -> None:
    database_path = tmp_path / "pre-books.sqlite"
    database_url = f"sqlite:///{database_path}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "b7e5109c34aa")
    engine = create_engine(database_url)
    timestamp = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs "
                "(id, run_key, mode, scheduled_for, started_at, status, config_hash) "
                "VALUES ('older-run', 'daily:older', 'paper', :scheduled, :started, "
                "'COMPLETED', 'hash')"
            ),
            {"scheduled": timestamp, "started": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO performance_snapshots "
                "(id, run_id, period, captured_at, equity) "
                "VALUES ('snap-1', 'older-run', 'daily', :captured, '1000')"
            ),
            {"captured": timestamp},
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT book_id FROM performance_snapshots WHERE id = 'snap-1'")
            ).scalar_one()
            is None
        )
        assert "books" in sa_inspect(engine).get_table_names()

    command.downgrade(config, "b7e5109c34aa")
    assert "books" not in sa_inspect(engine).get_table_names()

    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO books "
                "(id, name, strategy_document_path, strategy_content_hash, status, "
                "starting_cash, created_at, updated_at) "
                "VALUES ('book-1', 'control', 'knowledge/strategy.md', :hash, 'active', "
                "'2000', :created, :created)"
            ),
            {"hash": "a" * 64, "created": timestamp},
        )

    with pytest.raises(RuntimeError, match="simulated books hold their own history"):
        command.downgrade(config, "b7e5109c34aa")


def test_books_open_cash_is_a_required_string_option() -> None:
    open_command = get_command(app).commands["books"].commands["open"]
    cash_option = next(option for option in open_command.params if "--cash" in option.opts)
    assert cash_option.required
    assert str(cash_option.type) == "STRING"


def _research_state(
    tmp_path: Path,
) -> tuple[Session, Run, UniverseScan, ResearchRunResult, str]:
    session = create_session_factory(f"sqlite:///{tmp_path}/research.sqlite")()
    as_of = AS_OF
    run = Run(run_key="daily:test", scheduled_for=as_of, config_hash="config")
    session.add(run)
    session.commit()
    content_hash = hashlib.sha256(b"payload").hexdigest()
    research_id = hashlib.sha256(f"{run.id}:{content_hash}".encode()).hexdigest()
    persist_research_item(
        session,
        research_id=research_id,
        run_id=run.id,
        symbols=("SPY",),
        source_tier="BROKER",
        source_type="MARKET_DATA",
        source_name="alpaca-market-context",
        provider="alpaca",
        provider_item_id="SPY:context",
        research_question_id="a" * 64,
        research_question="What changed?",
        raw_artifact_path="research/alpaca/context.json",
        normalized_summary="SPY market context summary",
        normalized_text="SPY detailed market context",
        published_at=as_of - timedelta(minutes=5),
        retrieved_at=as_of,
        content_hash=content_hash,
        headline="SPY market context",
    )
    asset = UniverseAsset(
        symbol="SPY",
        name="SPDR S&P 500 ETF",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )
    scan = UniverseScan(
        as_of=as_of,
        asset_content_hash="b" * 64,
        eligible_assets=(asset,),
        candidates=(
            ResearchCandidate(
                symbol="SPY",
                score=100,
                asset=asset,
                signals=(CandidateSignal(source="BENCHMARK"),),
            ),
        ),
        most_active_volume_updated_at=as_of,
        most_active_trades_updated_at=as_of,
        market_movers_updated_at=as_of,
        skipped_screener_symbols=0,
    )
    question = ResearchRequest.create(
        symbol="SPY",
        question_type="MARKET_CONTEXT",
        query="What changed?",
        window_start=as_of - timedelta(hours=1),
        window_end=as_of,
        priority=100,
    )
    research = ResearchRunResult(
        plan=ResearchPlan(
            as_of=as_of,
            candidate_symbols=("SPY",),
            deep_symbols=("SPY",),
            questions=(question,),
        ),
        collection=ResearchCollection(batches=(), request_count=1, response_bytes=7),
        artifacts={
            "document": ResearchArtifact(
                relative_path="alpaca/context.json",
                content_hash=content_hash,
                byte_count=7,
                created=True,
            )
        },
        persisted_research_ids=(research_id,),
        elapsed_seconds=0.1,
    )
    return session, run, scan, research, research_id


def _session(tmp_path: Path) -> Session:
    return create_session_factory(f"sqlite:///{tmp_path}/books.sqlite")()


def _run(
    session: Session,
    key: str = "daily:2026-08-22:15:15:America/New_York",
    scheduled_for: datetime | None = None,
) -> Run:
    run = Run(
        run_key=key,
        scheduled_for=scheduled_for or AS_OF,
        config_hash="config",
        status="COMPLETED",
    )
    session.add(run)
    session.commit()
    return run


def _open(
    session: Session,
    tmp_path: Path,
    *,
    name: str = "control",
    cash: str = "2000",
) -> Book:
    shutil.copytree(PROJECT_ROOT / "prompts", tmp_path / "prompts", dirs_exist_ok=True)
    path = tmp_path / f"{name}.md"
    if not path.is_file():
        path.write_text(STRATEGY)
    return open_book(
        session,
        name=name,
        starting_cash=Decimal(cash),
        strategy_document_path=path,
        as_of=AS_OF,
        project_root=tmp_path,
        catalog=_catalog(),
        max_starting_cash=load_risk_config(
            PROJECT_ROOT / "config/risk.yaml"
        ).portfolio.expected_max_equity_usd,
    )


def _quote(
    *,
    bid: Decimal,
    ask: Decimal,
    age: timedelta = timedelta(0),
) -> Quote:
    return Quote(
        symbol="SPY",
        bid=bid,
        ask=ask,
        timestamp=AS_OF - age,
        feed="test",
        average_daily_dollar_volume=Decimal("10000000"),
    )


def _approved(side: str, *, qty: Decimal, limit: Decimal) -> RiskDecision:
    return RiskDecision(
        proposal_id=str(uuid4()),
        approved=True,
        normalized_order=NormalizedOrder(
            symbol="SPY",
            side=side,
            qty=qty,
            limit_price=limit,
            notional=qty * limit,
        ),
        rejection_codes=[],
        human_explanation="approved",
    )


def _decision(research_id: str) -> DailyDecision:
    return DailyDecision(
        status="PROPOSE_TRADES",
        market_assessment="The evidence supports a bounded paper proposal.",
        strongest_counterargument="The observed move may reverse.",
        daily_update=DailyUpdate(
            headline="A small test entry",
            lesson_title="Books have their own cash",
            lesson="A variant must live with the positions it chose.",
            overview="The book buys a small SPY position against its own cash.",
            next_day_plan="Hold unless invalidation fires.",
        ),
        proposals=(
            TradeProposal(
                proposal_id=uuid4(),
                symbol="SPY",
                action="BUY",
                target_notional_usd="100",
                confidence=0.6,
                time_horizon="days",
                rationale="Market context is constructive.",
                key_risks=["Reversal"],
                invalidation_conditions=["Price loses support"],
                evidence_ids=[research_id],
                max_acceptable_price="10.05",
            ),
        ),
    )


def _catalog():
    return load_pipeline_catalog(
        PROJECT_ROOT / "config/pipelines.yaml",
        load_agent_config(PROJECT_ROOT / "config/agents.yaml"),
    )
