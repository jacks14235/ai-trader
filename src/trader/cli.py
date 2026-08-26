"""Operational CLI. Paper mode only; live trading intentionally unavailable."""

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, cast
from zoneinfo import ZoneInfo

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.config import load_agent_config
from trader.agent.event_runner import event_run
from trader.agent.invocation import workflow_trail
from trader.agent.runner import daily_run
from trader.agent.runtime import configured_daily_reasoning_pipeline
from trader.agent.weekly import apply_anchored_change
from trader.agent.weekly_runner import configured_weekly_review
from trader.books.runtime import book_ledger, configured_book_evaluation_pipeline
from trader.books.service import (
    get_book,
    list_books,
    load_book_state,
    open_book,
    set_book_status,
)
from trader.broker.alpaca import AlpacaPaperBroker
from trader.execution.canary import paper_canary
from trader.execution.reconciliation import Reconciler
from trader.ledger.knowledge import (
    get_strategy_proposal,
    pending_strategy_proposals,
    resolve_strategy_proposal,
)
from trader.ledger.strategy import strategy_content_hash
from trader.logging.audit import audit
from trader.persistence.db import create_session_factory
from trader.persistence.models import MarketEvent, Run
from trader.persistence.repositories import latest_snapshot, snapshot
from trader.research.config import load_research_config
from trader.research.events import BeaEventProvider, EventProvider, FileEventProvider
from trader.research.runtime import SecResolvedResearchPipeline, configured_research_pipeline
from trader.risk.config import load_risk_config, load_risk_policy, load_universe_config
from trader.risk.runtime import PaperRiskExecutionPipeline
from trader.scheduling.config import (
    BeaDiscoverySourceConfig,
    DiscoveryConfig,
    FileDiscoverySourceConfig,
    load_dynamic_runs_config,
)
from trader.scheduling.discovery import EventDiscoveryService
from trader.scheduling.models import EventType, MarketEventRequest, ScheduleRequest
from trader.scheduling.service import Scheduler
from trader.settings import Settings, get_settings
from trader.universe.provider import AlpacaUniverseProvider
from trader.universe.scanner import UniverseScanner

app = typer.Typer(no_args_is_help=True)
runs_app = typer.Typer()
events_app = typer.Typer()
schedule_app = typer.Typer()
universe_app = typer.Typer()
research_app = typer.Typer()
agents_app = typer.Typer()
strategy_app = typer.Typer()
books_app = typer.Typer()
app.add_typer(runs_app, name="runs")
app.add_typer(events_app, name="events")
app.add_typer(schedule_app, name="schedule")
app.add_typer(universe_app, name="universe")
app.add_typer(research_app, name="research")
app.add_typer(agents_app, name="agents")
app.add_typer(strategy_app, name="strategy")
app.add_typer(books_app, name="books")
EASTERN = ZoneInfo("America/New_York")


def services() -> tuple[Settings, Session, AlpacaPaperBroker]:
    settings = get_settings()
    key, secret = settings.require_broker_credentials()
    session = create_session_factory(settings.trader_database_url, create_schema=False)()
    return settings, session, AlpacaPaperBroker(key, secret)


def scheduler_service(settings: Settings, session: Session) -> Scheduler:
    config = load_dynamic_runs_config(settings.trader_dynamic_runs_config)
    universe = load_universe_config(settings.trader_universe_config)
    return Scheduler(session, config, frozenset(universe.event_symbols))


def universe_scanner_service(settings: Settings) -> UniverseScanner:
    key, secret = settings.require_broker_credentials()
    universe = load_universe_config(settings.trader_universe_config)
    return UniverseScanner(AlpacaUniverseProvider(key, secret), universe)


def research_pipeline_service(
    settings: Settings,
    session: Session,
    *,
    verify_schema: bool = True,
) -> SecResolvedResearchPipeline:
    return configured_research_pipeline(settings, session, verify_schema=verify_schema)


def risk_execution_pipeline_service(
    settings: Settings,
    session: Session,
    broker: AlpacaPaperBroker,
) -> PaperRiskExecutionPipeline:
    return PaperRiskExecutionPipeline(
        session,
        broker,
        load_risk_config(settings.trader_risk_config),
        trading_enabled=settings.trading_enabled,
        stop_file=settings.stop_trading_file,
    )


def database_service() -> tuple[Settings, Session]:
    settings = get_settings()
    session = create_session_factory(settings.trader_database_url, create_schema=False)()
    return settings, session


def configured_event_provider(config: DiscoveryConfig) -> EventProvider:
    """Build only a provider admitted by the strict discriminated configuration."""
    source = config.source
    if isinstance(source, FileDiscoverySourceConfig):
        return FileEventProvider(source.feed_path)
    if isinstance(source, BeaDiscoverySourceConfig):
        return BeaEventProvider(
            cache_path=source.cache_path,
            included_release_names=source.included_release_names,
            timeout_seconds=source.timeout_seconds,
            max_cache_age_minutes=source.max_cache_age_minutes,
            max_fetch_attempts=source.max_fetch_attempts,
        )
    raise TypeError(f"unsupported event provider configuration: {type(source).__name__}")


def event_discovery_service(
    settings: Settings,
    session: Session,
) -> EventDiscoveryService | None:
    config = load_dynamic_runs_config(settings.trader_dynamic_runs_config)
    if not config.discovery.enabled:
        return None
    universe = load_universe_config(settings.trader_universe_config)
    return EventDiscoveryService(
        configured_event_provider(config.discovery),
        Scheduler(session, config, frozenset(universe.event_symbols)),
        config.discovery,
        frozenset(universe.event_symbols),
    )


@app.command()
def status() -> None:
    """Fetch paper account state and atomically persist an immutable snapshot."""
    _settings, session, broker = services()
    account = broker.get_account()
    positions = broker.get_positions()
    orders = broker.get_open_orders()
    snap = snapshot(session, account, positions)
    typer.echo(
        json.dumps(
            {
                "snapshot_id": snap.id,
                "environment": "paper",
                "account": account.model_dump(mode="json"),
                "positions": [position.model_dump(mode="json") for position in positions],
                "open_orders": [order.model_dump(mode="json") for order in orders],
            },
            default=str,
            indent=2,
        )
    )


@app.command("daily-run")
def daily_run_command(
    test_rerun: Annotated[
        bool,
        typer.Option(
            "--test-rerun",
            help="Run an additional uniquely keyed paper test without deleting today's audit.",
        ),
    ] = False,
) -> None:
    settings = get_settings()
    load_risk_policy(settings.trader_risk_config, settings.trader_universe_config)
    load_dynamic_runs_config(settings.trader_dynamic_runs_config)
    load_research_config(settings.trader_research_config)
    load_agent_config(settings.trader_agents_config)
    universe = load_universe_config(settings.trader_universe_config)
    settings, session, broker = services()
    typer.echo(
        daily_run(
            session,
            broker,
            settings.trader_raw_data_dir,
            settings.trader_risk_config.read_bytes(),
            universe_config_bytes=settings.trader_universe_config.read_bytes(),
            dynamic_runs_config_bytes=settings.trader_dynamic_runs_config.read_bytes(),
            research_config_bytes=settings.trader_research_config.read_bytes(),
            agents_config_bytes=settings.trader_agents_config.read_bytes(),
            strategy_bytes=settings.trader_strategy_document.read_bytes(),
            portfolio_policy_bytes=settings.trader_portfolio_policy.read_bytes(),
            event_discovery=event_discovery_service(settings, session),
            candidate_scanner=universe_scanner_service(settings),
            research_pipeline=research_pipeline_service(settings, session),
            reasoning_pipeline=configured_daily_reasoning_pipeline(settings, session),
            book_evaluation_pipeline=configured_book_evaluation_pipeline(
                settings,
                session,
                broker,
            ),
            risk_execution_pipeline=risk_execution_pipeline_service(
                settings,
                session,
                broker,
            ),
            research_event_symbols=universe.event_symbols,
            test_rerun=test_rerun,
        )
    )


@app.command("weekly-run")
def weekly_run_command(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO timestamp to review as of; defaults to now."),
    ] = None,
    test_rerun: Annotated[
        bool,
        typer.Option(
            "--test-rerun",
            help="Run an additional uniquely keyed review without deleting the week's audit.",
        ),
    ] = False,
) -> None:
    """Review the completed week and record a strategy proposal for human review."""
    settings, session = database_service()
    result = configured_weekly_review(
        settings,
        session,
        as_of=None if as_of is None else _aware_datetime(as_of, "--as-of"),
        test_rerun=test_rerun,
    )
    typer.echo(json.dumps(result.summary(), indent=2))


@strategy_app.command("proposals")
def list_strategy_proposals() -> None:
    """List strategy changes awaiting a human decision."""
    _settings, session = database_service()
    proposals = pending_strategy_proposals(session)
    if not proposals:
        typer.echo("No strategy proposals are awaiting review.")
        return
    for proposal in proposals:
        typer.echo(f"{proposal.change_id} {proposal.created_at.isoformat()} {proposal.run_id}")


@strategy_app.command("show")
def show_strategy_proposal(change_id: str) -> None:
    """Show one pending proposal as an applicable before/after diff."""
    _settings, session = database_service()
    typer.echo(json.dumps(get_strategy_proposal(session, change_id).summary(), indent=2))


@strategy_app.command("approve")
def approve_strategy_proposal(
    change_id: str,
    reviewer: Annotated[str, typer.Option("--reviewer", help="Who is approving this change.")],
    note: Annotated[str, typer.Option("--note")] = "",
) -> None:
    """Apply an approved proposal to the strategy document and record the decision."""
    settings, session = database_service()
    proposal = get_strategy_proposal(session, change_id)
    updated = apply_anchored_change(
        settings.trader_strategy_document.read_text(encoding="utf-8"),
        current_text=proposal.current_text,
        replacement_text=proposal.replacement_text,
    )
    settings.trader_strategy_document.write_text(updated, encoding="utf-8")
    resolution = resolve_strategy_proposal(
        session,
        proposal=proposal,
        approved=True,
        reviewer=reviewer,
        note=note,
        as_of=datetime.now(UTC),
        applied_content_hash=strategy_content_hash(updated),
    )
    typer.echo(
        json.dumps(
            {
                "change_id": change_id,
                "resolution_id": resolution,
                "applied_to": str(settings.trader_strategy_document),
                "content_hash": strategy_content_hash(updated),
            },
            indent=2,
        )
    )


@strategy_app.command("reject")
def reject_strategy_proposal(
    change_id: str,
    reviewer: Annotated[str, typer.Option("--reviewer", help="Who is rejecting this change.")],
    note: Annotated[str, typer.Option("--note")] = "",
) -> None:
    """Record a rejected proposal without touching the strategy document."""
    _settings, session = database_service()
    proposal = get_strategy_proposal(session, change_id)
    resolution = resolve_strategy_proposal(
        session,
        proposal=proposal,
        approved=False,
        reviewer=reviewer,
        note=note,
        as_of=datetime.now(UTC),
    )
    typer.echo(json.dumps({"change_id": change_id, "resolution_id": resolution}, indent=2))


@books_app.command("list")
def list_books_command(
    status: Annotated[str | None, typer.Option("--status")] = None,
) -> None:
    """List simulated strategy books and their derived cash and positions."""
    _settings, session = database_service()
    books = list_books(session, status=status)
    if not books:
        typer.echo("No simulated books are recorded.")
        return
    rows = []
    for book in books:
        state = load_book_state(session, book)
        rows.append(
            {
                "name": book.name,
                "status": book.status,
                "starting_cash": book.starting_cash,
                "cash": str(state.cash),
                "positions": [
                    {
                        "symbol": position.symbol,
                        "qty": str(position.qty),
                        "average_entry_price": str(position.average_entry_price),
                    }
                    for position in state.positions
                ],
                "realized_pnl": str(state.realized_pnl),
                "fill_count": state.fill_count,
                "strategy_document_path": book.strategy_document_path,
            }
        )
    typer.echo(json.dumps(rows, indent=2))


@books_app.command("open")
def open_book_command(
    name: str,
    cash: Annotated[str, typer.Option("--cash", help="Starting cash the book may invest.")],
    strategy: Annotated[
        Path,
        typer.Option("--strategy", help="Path to the variant's strategy document."),
    ],
    description: Annotated[str | None, typer.Option("--description")] = None,
) -> None:
    """Open a simulated book. It never reaches the broker."""
    try:
        starting_cash = Decimal(cash)
    except ArithmeticError as exc:
        raise typer.BadParameter("cash must be a decimal number") from exc
    _settings, session = database_service()
    book = open_book(
        session,
        name=name,
        starting_cash=starting_cash,
        strategy_document_path=strategy,
        description=description,
    )
    typer.echo(
        json.dumps(
            {
                "book_id": book.id,
                "name": book.name,
                "status": book.status,
                "starting_cash": book.starting_cash,
                "strategy_document_path": book.strategy_document_path,
                "strategy_content_hash": book.strategy_content_hash,
            },
            indent=2,
        )
    )


@books_app.command("show")
def show_book_command(name: str) -> None:
    """Show one book's derived state and settled history."""
    _settings, session = database_service()
    book = get_book(session, name)
    state = load_book_state(session, book)
    typer.echo(
        json.dumps(
            {
                **book_ledger(session, book),
                "cash": str(state.cash),
                "realized_pnl": str(state.realized_pnl),
                "positions": [
                    {
                        "symbol": position.symbol,
                        "qty": str(position.qty),
                        "average_entry_price": str(position.average_entry_price),
                    }
                    for position in state.positions
                ],
            },
            indent=2,
        )
    )


@books_app.command("pause")
def pause_book_command(name: str) -> None:
    """Stop evaluating a book without discarding its history."""
    _set_book_status(name, "paused")


@books_app.command("resume")
def resume_book_command(name: str) -> None:
    """Return a paused book to the daily evaluation roster."""
    _set_book_status(name, "active")


@books_app.command("retire")
def retire_book_command(name: str) -> None:
    """Permanently stop a book. Its history remains for comparison."""
    _set_book_status(name, "retired")


def _set_book_status(name: str, status: str) -> None:
    _settings, session = database_service()
    book = set_book_status(session, get_book(session, name), status)
    typer.echo(json.dumps({"name": book.name, "status": book.status}, indent=2))


@agents_app.command("validate")
def validate_agents() -> None:
    """Validate every registered role, prompt, context source, and permission boundary."""
    settings = get_settings()
    config = load_agent_config(settings.trader_agents_config)
    if not settings.trader_strategy_document.is_file():
        raise ValueError(f"strategy document not found: {settings.trader_strategy_document}")
    if not settings.trader_portfolio_policy.is_file():
        raise ValueError(f"portfolio policy not found: {settings.trader_portfolio_policy}")
    typer.echo(
        json.dumps(
            {
                "valid": True,
                "mode": config.mode,
                "environment_reasoning_enabled": settings.trader_reasoning_enabled,
                "automatic_daily_run": config.automatic_daily_run,
                "effective_daily_reasoning": (
                    settings.trader_reasoning_enabled and config.automatic_daily_run
                ),
                "paper_execution_enabled": settings.trading_enabled,
                "roles": {
                    name: {
                        "enabled": role.enabled,
                        "profile": role.profile,
                        "model": config.model_profiles[role.profile].model,
                        "reasoning_effort": (
                            config.model_profiles[role.profile].reasoning_effort
                        ),
                        "context_sources": role.context_sources,
                        "can_submit_orders": role.permissions.can_submit_orders,
                    }
                    for name, role in config.roles.items()
                },
            },
            indent=2,
        )
    )


@app.command("paper-canary")
def paper_canary_command(
    symbol: Annotated[str, typer.Option("--symbol")] = "SPY",
    notional: Annotated[str, typer.Option("--notional")] = "25",
    submit: Annotated[
        bool,
        typer.Option(
            "--submit",
            help="Submit the risk-approved order to Alpaca paper trading, then cancel if open.",
        ),
    ] = False,
) -> None:
    """Run an audited deterministic risk/execution canary; dry-run unless --submit is set."""
    settings, session, broker = services()
    try:
        parsed_notional = Decimal(notional)
    except ArithmeticError as exc:
        raise typer.BadParameter("notional must be a decimal number") from exc
    result = paper_canary(
        session,
        broker,
        settings.trader_raw_data_dir,
        load_risk_config(settings.trader_risk_config),
        settings.trader_risk_config.read_bytes(),
        stop_file=settings.stop_trading_file,
        symbol=symbol,
        notional=parsed_notional,
        submit=submit,
        trading_enabled=settings.trading_enabled,
    )
    typer.echo(json.dumps(result.summary(), indent=2))
    if submit and result.submitted_order_count != 1:
        raise typer.Exit(1)


@app.command()
def reconcile() -> None:
    """Reconcile local records to authoritative paper-broker state without submitting orders."""
    _settings, session, broker = services()
    report = Reconciler(broker, session).reconcile()
    typer.echo(
        json.dumps(
            report.model_dump(mode="json"),
            default=str,
            indent=2,
        )
    )
    if report.issues:
        raise typer.Exit(1)


@app.command()
def halt() -> None:
    settings, _session, broker = services()
    settings.stop_trading_file.touch(exist_ok=True)
    broker.cancel_all_orders()
    audit(
        "trading_halted",
        component="cli",
        environment=settings.trader_environment,
        stop_file=str(settings.stop_trading_file),
    )
    typer.echo(f"Trading halted; kill switch created at {settings.stop_trading_file}")


@app.command()
def portfolio() -> None:
    settings = get_settings()
    session = create_session_factory(settings.trader_database_url, create_schema=False)()
    snap = latest_snapshot(session)
    if not snap:
        raise typer.Exit(1)
    typer.echo(
        json.dumps(
            {"captured_at": snap.captured_at, "equity": snap.equity, "cash": snap.cash},
            default=str,
            indent=2,
        )
    )


@runs_app.command("list")
def list_runs() -> None:
    settings = get_settings()
    session = create_session_factory(settings.trader_database_url, create_schema=False)()
    for run in session.scalars(select(Run).order_by(Run.started_at.desc())):
        typer.echo(f"{run.id} {run.status} {run.run_key}")


@runs_app.command("show")
def show_run(run_id: str) -> None:
    settings = get_settings()
    session = create_session_factory(settings.trader_database_url, create_schema=False)()
    run = session.get(Run, run_id)
    if not run:
        raise typer.Exit(1)
    typer.echo(
        json.dumps(
            {
                "id": run.id,
                "run_key": run.run_key,
                "status": run.status,
                "error": run.error_summary,
                "agents": [
                    {
                        "invocation_id": invocation.id,
                        "role": invocation.role,
                        "step": invocation.step,
                        "attempt": invocation.attempt,
                        "parent_invocation_id": invocation.parent_invocation_id,
                        "status": invocation.status,
                        "request_path": invocation.request_path,
                        "response_path": invocation.response_path,
                        "error": invocation.error_summary,
                    }
                    for invocation in workflow_trail(session, run.id)
                ],
            },
            indent=2,
        )
    )


@events_app.command("list")
def list_events() -> None:
    """List known source-backed market events."""
    _settings, session = database_service()
    records = session.scalars(select(MarketEvent).order_by(MarketEvent.scheduled_at))
    for record in records:
        symbols = ",".join(json.loads(record.symbols_json))
        typer.echo(
            f"{record.id} {record.status} {record.event_type} "
            f"{_persisted_utc(record.scheduled_at).isoformat()} {symbols}"
        )


@events_app.command("add")
def add_event(
    event_type: Annotated[EventType, typer.Option("--event-type")],
    symbols: Annotated[list[str], typer.Option("--symbol")],
    scheduled_at: Annotated[str, typer.Option("--scheduled-at")],
    source: Annotated[str, typer.Option("--source")],
    source_event_id: Annotated[str, typer.Option("--source-event-id")],
    evidence_json: Annotated[str, typer.Option("--evidence-json")],
    confidence: Annotated[float, typer.Option("--confidence")] = 1.0,
    announced_at: Annotated[str | None, typer.Option("--announced-at")] = None,
) -> None:
    """Manually register a sourced event for paper scheduler testing."""
    settings, session = database_service()
    evidence = _json_object(evidence_json, "evidence")
    record = scheduler_service(settings, session).register_market_event(
        MarketEventRequest(
            event_type=event_type,
            symbols=tuple(symbols),
            scheduled_at=_aware_datetime(scheduled_at, "scheduled-at"),
            source=source,
            source_event_id=source_event_id,
            confidence=confidence,
            evidence=evidence,
            announced_at=(
                _aware_datetime(announced_at, "announced-at")
                if announced_at is not None
                else datetime.now(UTC)
            ),
        )
    )
    typer.echo(record.id)


@events_app.command("cancel")
def cancel_event(market_event_id: str) -> None:
    """Cancel an event and all of its unclaimed future runs."""
    settings, session = database_service()
    cancelled = scheduler_service(settings, session).cancel_market_event(market_event_id)
    typer.echo(json.dumps({"market_event_id": market_event_id, "cancelled_runs": cancelled}))


@events_app.command("discover")
def discover_events(
    as_of: Annotated[str | None, typer.Option("--as-of")] = None,
) -> None:
    """Preview strict source-backed candidates without changing scheduler state."""
    settings, _session = database_service()
    config = load_dynamic_runs_config(settings.trader_dynamic_runs_config)
    universe = load_universe_config(settings.trader_universe_config)
    current = _aware_datetime(as_of, "as-of") if as_of is not None else datetime.now(UTC)
    batch = configured_event_provider(config.discovery).discover(
        symbols=frozenset(universe.event_symbols),
        window_start=current,
        window_end=current + timedelta(days=config.discovery.lookahead_days),
        retrieved_at=current,
    )
    typer.echo(
        json.dumps(
            {
                "provider": batch.provider,
                "source_reference": batch.source_reference,
                "content_hash": batch.content_hash,
                "retrieval_mode": batch.retrieval_mode,
                "source_updated_at": batch.source_updated_at,
                "candidates": [event.model_dump(mode="json") for event in batch.events],
                "skipped_outside_window": batch.skipped_outside_window,
                "skipped_outside_universe": batch.skipped_outside_universe,
                "skipped_duplicates": batch.skipped_duplicates,
                "preview_only": True,
            },
            indent=2,
        )
    )


@universe_app.command("scan")
def scan_universe(
    as_of: Annotated[str | None, typer.Option("--as-of")] = None,
) -> None:
    """Preview the broad Alpaca universe and bounded daily research slate."""
    settings, _session, broker = services()
    current = _aware_datetime(as_of, "as-of") if as_of is not None else datetime.now(UTC)
    scan = universe_scanner_service(settings).scan(
        as_of=current,
        portfolio_symbols=tuple(position.symbol for position in broker.get_positions()),
    )
    output = scan.summary()
    output["preview_only"] = True
    typer.echo(json.dumps(output, indent=2))


@research_app.command("plan")
def plan_research(
    as_of: Annotated[str | None, typer.Option("--as-of")] = None,
) -> None:
    """Preview the bounded Alpaca/SEC plan without collecting or persisting evidence."""
    settings, session, broker = services()
    universe = load_universe_config(settings.trader_universe_config)
    load_research_config(settings.trader_research_config)
    current = _aware_datetime(as_of, "as-of") if as_of is not None else datetime.now(UTC)
    positions = broker.get_positions()
    portfolio_symbols = tuple(position.symbol for position in positions)
    scan = universe_scanner_service(settings).scan(
        as_of=current,
        portfolio_symbols=portfolio_symbols,
    )
    preview = research_pipeline_service(settings, session, verify_schema=False).preview(
        scan,
        portfolio_symbols=portfolio_symbols,
        event_symbols=universe.event_symbols,
    )
    typer.echo(json.dumps(preview.summary(), indent=2))


@events_app.command("today")
def events_today() -> None:
    """List persisted events for the current Eastern calendar day."""
    _settings, session = database_service()
    local_date = datetime.now(EASTERN).date()
    start = datetime.combine(local_date, time.min, EASTERN).astimezone(UTC)
    end = datetime.combine(local_date + timedelta(days=1), time.min, EASTERN).astimezone(UTC)
    records = session.scalars(
        select(MarketEvent)
        .where(MarketEvent.scheduled_at >= start, MarketEvent.scheduled_at < end)
        .order_by(MarketEvent.scheduled_at)
    )
    for record in records:
        symbols = ",".join(json.loads(record.symbols_json))
        typer.echo(
            f"{record.id} {record.status} {record.event_type} "
            f"{_persisted_utc(record.scheduled_at).isoformat()} {symbols}"
        )


@schedule_app.command("create")
def create_schedule(
    market_event_id: str,
    scheduled_for: Annotated[str, typer.Option("--scheduled-for")],
    reason: Annotated[str, typer.Option("--reason")],
    payload_json: Annotated[str, typer.Option("--payload-json")] = "{}",
    created_by_run_id: Annotated[str | None, typer.Option("--created-by-run-id")] = None,
) -> None:
    """Create one deterministic future paper run for a known market event."""
    settings, session = database_service()
    payload = _json_object(payload_json, "payload")
    record = scheduler_service(settings, session).schedule(
        ScheduleRequest(
            market_event_id=market_event_id,
            scheduled_for=_aware_datetime(scheduled_for, "scheduled-for"),
            reason=reason,
            payload=payload,
        ),
        created_by_run_id=created_by_run_id,
    )
    typer.echo(record.id)


@schedule_app.command("list")
def list_schedules(
    status: Annotated[str | None, typer.Option("--status")] = None,
) -> None:
    """List scheduled event runs in due-time order."""
    settings, session = database_service()
    records = scheduler_service(settings, session).list_scheduled_runs(status=status)
    for record in records:
        symbols = ",".join(json.loads(record.symbols_json))
        typer.echo(
            f"{record.id} {record.status} {record.event_type} "
            f"{_persisted_utc(record.scheduled_for).isoformat()} {symbols}"
        )


@schedule_app.command("cancel")
def cancel_schedule(scheduled_run_id: str) -> None:
    """Cancel a pending scheduled run."""
    settings, session = database_service()
    scheduler_service(settings, session).cancel(scheduled_run_id)
    typer.echo(scheduled_run_id)


@app.command("scheduler-tick")
def scheduler_tick_command() -> None:
    """Run one stateless scheduler tick, suitable for a once-per-minute systemd timer."""
    settings, session, broker = services()
    load_risk_policy(settings.trader_risk_config, settings.trader_universe_config)
    scheduler = scheduler_service(settings, session)
    report = scheduler.tick(
        lambda record: event_run(
            session,
            broker,
            record,
            settings.trader_raw_data_dir,
            settings.trader_risk_config.read_bytes(),
            settings.trader_universe_config.read_bytes(),
            settings.trader_dynamic_runs_config.read_bytes(),
        )
    )
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    if report.failed:
        raise typer.Exit(1)


@app.command("event-run")
def event_run_command(scheduled_run_id: str) -> None:
    """Execute one named due event run using normal scheduler lease protections."""
    settings, session, broker = services()
    load_risk_policy(settings.trader_risk_config, settings.trader_universe_config)
    scheduler = scheduler_service(settings, session)
    report = scheduler.run_due(
        scheduled_run_id,
        lambda record: event_run(
            session,
            broker,
            record,
            settings.trader_raw_data_dir,
            settings.trader_risk_config.read_bytes(),
            settings.trader_universe_config.read_bytes(),
            settings.trader_dynamic_runs_config.read_bytes(),
        ),
    )
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    if report.failed:
        raise typer.Exit(1)


def _json_object(value: str, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{label} must be a JSON object")
    return cast(dict[str, object], parsed)


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise typer.BadParameter(f"{label} must include a UTC offset")
    return parsed


def _persisted_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
