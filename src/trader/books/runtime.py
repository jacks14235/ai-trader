"""Evaluate strategy variants against their own simulated books.

Each active book runs the daily trader under its own strategy document, against its own cash and
positions, and settles through the deterministic simulator. It reuses the run's candidate slate and
research. Shared inputs make comparisons auditable, but do not alone establish causality: model
sampling, portfolio state and execution timing can also change outcomes.

Two boundaries hold this apart from the live line:

- This pipeline accepts a `MarketDataSource`, never a `Broker`. It can read quotes and cannot
  submit, cancel, or reconcile anything.
- Every proposal, risk decision, fill, and snapshot it writes carries a `book_id`. Readers of the
  live record filter those out, so a variant's activity can never be mistaken for the portfolio's.

Authorization is not reimplemented. A book's proposals go through the same `risk.engine.evaluate`
as the portfolio's, against a context built from the book's own state, so a variant cannot escape
the human-owned risk policy by being simulated.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import cache, partial
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.catalog import PipelineCatalog, load_pipeline_catalog
from trader.agent.codex_cli import CodexCLIProvider, StructuredReasoningProvider
from trader.agent.config import (
    AgentConfig,
    AgentRoleConfig,
    load_agent_config,
    resolved_prompt_path,
)
from trader.agent.invocation import canonical_json, resolve_role
from trader.agent.models import TradeProposal
from trader.agent.packets import NamedPacket
from trader.agent.pipeline import ProfileRunResult, run_profile
from trader.agent.profile_context import (
    BookAgentContext,
    ResearchBundle,
    load_research_bundle,
    project_book_context,
    project_research_context,
)
from trader.agent.prompts import compose_prompt
from trader.agent.reasoning import (
    DailyDecision,
)
from trader.books.experiments import (
    begin_book_evaluation,
    fail_book_evaluation,
    finish_book_evaluation,
)
from trader.books.models import (
    BookRunSummary,
    BookState,
    FillAssumptions,
    MarketDataSource,
    ReferencePointSummary,
    SimulatedFillResult,
)
from trader.books.references import (
    record_cash_reference,
    record_spy_reference,
    record_spy_reference_failure,
)
from trader.books.service import (
    book_fills,
    list_books,
    load_book_state,
    normalize_book_name,
    persist_simulated_fill,
    read_operating_note,
    sync_strategy_document,
)
from trader.books.simulator import apply_fill, simulate_fill
from trader.broker.models import MarketClock, Quote
from trader.ledger.history import load_latest_book_wait, load_recent_decisions
from trader.ledger.models import (
    PriorWaitDecision,
    WaitingDecisionMemory,
    WaitTriggerAssessment,
)
from trader.ledger.service import record_performance_snapshot
from trader.persistence.models import (
    AgentInvocation,
    Book,
    BookEvaluation,
    BookExperimentPhase,
    BookReferencePoint,
    PerformanceSnapshot,
    SimulatedFill,
    TradeProposalRecord,
)
from trader.persistence.repositories import (
    persist_agent_decision,
    persist_risk_decision,
    persist_trade_proposal,
)
from trader.research.service import ResearchRunResult
from trader.risk.config import RiskConfig, load_risk_config
from trader.risk.engine import evaluate
from trader.risk.models import RiskContext, RiskDecision
from trader.risk.runtime import policy_hash
from trader.settings import Settings
from trader.universe.models import UniverseScan

CENT = Decimal("0.01")
EASTERN = ZoneInfo("America/New_York")


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
        catalog: PipelineCatalog | None = None,
        project_root: Path | None = None,
        catalog_bytes: bytes | None = None,
    ) -> None:
        self.session = session
        # Research packets are a book-only source; leave the incumbent role/context unchanged.
        daily_role = config.roles["daily_trader"]
        sources = tuple(dict.fromkeys((*daily_role.context_sources, "research_packets")))
        self.config = config.model_copy(
            update={
                "roles": {
                    **config.roles,
                    "daily_trader": daily_role.model_copy(update={"context_sources": sources}),
                }
            }
        )
        self.project_root = (project_root or Path.cwd()).resolve()
        self.catalog = catalog or load_pipeline_catalog(
            self.project_root / "config/pipelines.yaml",
            self.config,
            project_root=self.project_root,
        )
        self.catalog_bytes = catalog_bytes
        self.market_data = market_data
        self.risk_config = risk_config
        self.prompt = prompt
        self.portfolio_policy = portfolio_policy
        self.provider = provider
        self.assumptions = assumptions or FillAssumptions()
        # Fail on a disabled or unresolvable manager before any book claims an evaluation.
        resolve_role(self.config, "daily_trader")

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
        directory.mkdir(parents=True, exist_ok=False)
        if self.catalog_bytes is not None:
            (directory / "pipelines_config.yaml").write_bytes(self.catalog_bytes)

        bundle: ResearchBundle | None = None
        evidence_error: Exception | None = None
        if books:
            try:
                bundle = load_research_bundle(
                    self.session,
                    run_id=run_id,
                    as_of=as_of,
                    scan=scan,
                    research=research,
                )
                (directory / "research_bundle.json").write_text(
                    canonical_json(bundle) + "\n",
                    encoding="utf-8",
                )
            except Exception as error:
                evidence_error = error
        for book in books:
            try:
                if evidence_error is not None:
                    raise evidence_error
                assert bundle is not None
                summary = self._evaluate(
                    book=book,
                    run_id=run_id,
                    run_directory=run_directory,
                    directory=directory / book.name,
                    as_of=as_of,
                    bundle=bundle,
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
        bundle: ResearchBundle,
        allowed_symbols: frozenset[str],
    ) -> BookRunSummary:
        # Claim artifacts before file sync, phase creation or invocation; never overwrite a run.
        directory.mkdir(parents=True, exist_ok=False)
        strategy, document_changed = sync_strategy_document(
            self.session,
            book,
            as_of=as_of,
            project_root=self.project_root,
        )
        note = read_operating_note(
            Path(book.operating_note_path) if book.operating_note_path else None,
            project_root=self.project_root,
        )
        if book.process_profile not in self.catalog.profiles:
            raise ValueError(f"unknown process profile: {book.process_profile}")
        prompts = self._prompts(book.process_profile)
        configuration = self._configuration(book, strategy, note, prompts)
        evaluation = begin_book_evaluation(
            self.session,
            book=book,
            run_id=run_id,
            as_of=as_of,
            configuration=configuration,
            inputs={
                "research_bundle_hash": bundle.content_hash,
                "admitted_evidence_ids": list(bundle.admitted_evidence_ids),
                "allowed_symbols": sorted(allowed_symbols),
                "catalog_hash": _hash(self.catalog_bytes or canonical_json(self.catalog).encode()),
                # Code identity belongs to the evaluation, not to the experiment phase: an
                # unrelated edit elsewhere in the package must not split a book's phase history
                # and destroy the comparability the phase exists to provide.
                "implementation_hash": _implementation_hash(),
            },
        )
        try:
            (directory / "strategy.md").write_text(strategy, encoding="utf-8")
            (directory / "operating_note.md").write_text(note, encoding="utf-8")
            _write_json(
                directory / "experiment.json",
                {
                    "evaluation_id": evaluation.id,
                    "phase_id": evaluation.phase_id,
                    "configuration": configuration,
                    "inputs": json.loads(evaluation.manifest_json),
                },
            )
            summary = self._evaluate_claimed(
                book=book,
                run_id=run_id,
                run_directory=run_directory,
                directory=directory,
                as_of=as_of,
                bundle=bundle,
                allowed_symbols=allowed_symbols,
                strategy=strategy,
                note=note,
                prompts=prompts,
                evaluation=evaluation,
                document_changed=document_changed,
            )
        except Exception as error:
            self.session.rollback()
            fail_book_evaluation(self.session, evaluation, error=str(error) or type(error).__name__)
            _write_json(directory / "failure.json", {"status": "FAILED", "error": str(error)})
            raise
        return summary

    def _evaluate_claimed(
        self,
        *,
        book: Book,
        run_id: str,
        run_directory: Path,
        directory: Path,
        as_of: datetime,
        bundle: ResearchBundle,
        allowed_symbols: frozenset[str],
        strategy: str,
        note: str,
        prompts: dict[str, str],
        evaluation: BookEvaluation,
        document_changed: bool,
    ) -> BookRunSummary:
        state = load_book_state(self.session, book, as_of=as_of)
        prices, context_as_of = self._prices(
            tuple(position.symbol for position in state.positions),
            minimum_as_of=bundle.as_of,
        )
        prior_wait = load_latest_book_wait(
            self.session,
            book_id=book.id,
            exclude_run_id=run_id,
            as_of=context_as_of,
        )
        waiting_decisions, wait_as_of = self._waiting_memory(
            prior_wait,
            bundle=bundle,
            minimum_as_of=context_as_of,
        )
        if wait_as_of > context_as_of:
            prices, context_as_of = self._prices(
                tuple(position.symbol for position in state.positions),
                minimum_as_of=wait_as_of,
            )
        context_bundle = bundle.model_copy(update={"as_of": context_as_of})
        account = state.account(prices)
        positions = state.broker_positions(prices)
        memory = load_recent_decisions(
            self.session,
            exclude_run_id=run_id,
            as_of=context_as_of,
            book_id=book.id,
        )

        def daily_context(
            role: AgentRoleConfig,
            packets: tuple[NamedPacket, ...],
        ) -> BookAgentContext:
            return project_book_context(
                context_bundle,
                role,
                packets,
                strategy=strategy,
                portfolio_policy=self.portfolio_policy,
                account=account,
                positions=positions,
                recent_decisions=memory,
                waiting_decisions=waiting_decisions,
            )

        result = self._invoke(
            book=book,
            evaluation_id=evaluation.id,
            run_id=run_id,
            run_directory=run_directory,
            directory=directory,
            bundle=context_bundle,
            note=note,
            prompts=prompts,
            daily_context=daily_context,
        )
        decision, invocation_id = result.terminal.output, result.terminal.invocation_id
        proposals = decision.proposals
        risk_decisions, decision_quotes, execution_as_of = self._authorize(
            run_id=run_id,
            state=state,
            proposals=proposals,
            allowed_symbols=allowed_symbols,
            as_of=context_as_of,
        )

        fills, state = self._settle(
            book=book,
            run_id=run_id,
            state=state,
            decisions=risk_decisions,
            quotes=decision_quotes,
            as_of=execution_as_of,
        )

        final_prices, performance_as_of = self._prices(
            tuple(position.symbol for position in state.positions),
            minimum_as_of=execution_as_of,
        )
        account = state.account(final_prices)
        record_performance_snapshot(
            self.session,
            run_id=run_id,
            account=account,
            as_of=performance_as_of,
            book_id=book.id,
        )
        reference_points = [
            record_cash_reference(
                self.session,
                book=book,
                evaluation=evaluation,
                run_id=run_id,
                as_of=performance_as_of,
                assumptions=self.assumptions,
            )
        ]
        try:
            spy_quote = self._quote("SPY")
            if spy_quote is None:
                raise ValueError("no SPY quote is available")
            _validate_valuation_quote(
                "SPY",
                spy_quote,
                as_of=performance_as_of,
                max_age_seconds=self.assumptions.max_quote_age_seconds,
            )
            reference_points.append(
                record_spy_reference(
                    self.session,
                    book=book,
                    evaluation=evaluation,
                    run_id=run_id,
                    as_of=performance_as_of,
                    quote=spy_quote,
                    assumptions=self.assumptions,
                )
            )
        except ValueError as error:
            reference_points.append(
                record_spy_reference_failure(
                    self.session,
                    book=book,
                    evaluation=evaluation,
                    run_id=run_id,
                    as_of=performance_as_of,
                    assumptions=self.assumptions,
                    error=str(error),
                )
            )

        summary = BookRunSummary(
            book_id=book.id,
            name=book.name,
            run_id=run_id,
            invocation_id=invocation_id,
            process_profile=book.process_profile,
            experiment_phase_id=evaluation.phase_id,
            evaluation_id=evaluation.id,
            invocation_trail=result.trail,
            decision_status=decision.status,
            abstention_classification=(
                None if decision.abstention is None else decision.abstention.classification
            ),
            dissent_disposition_count=len(decision.dissent_dispositions),
            proposal_count=len(proposals),
            approved_count=sum(item.approved for item in risk_decisions),
            rejected_count=sum(not item.approved for item in risk_decisions),
            filled_count=sum(item.filled for item in fills),
            unfilled_outcomes=tuple(sorted({item.outcome for item in fills if not item.filled})),
            equity=account.equity,
            cash=account.cash,
            position_count=len(state.positions),
            references=tuple(
                ReferencePointSummary(
                    kind=item.kind,
                    status=item.status,
                    equity=None if item.equity is None else Decimal(item.equity),
                    cash=None if item.cash is None else Decimal(item.cash),
                    error=item.error,
                )
                for item in reference_points
            ),
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
        finish_book_evaluation(
            self.session,
            evaluation,
            terminal_invocation_id=invocation_id,
        )
        return summary

    def _waiting_memory(
        self,
        prior: PriorWaitDecision | None,
        *,
        bundle: ResearchBundle,
        minimum_as_of: datetime,
    ) -> tuple[tuple[WaitingDecisionMemory, ...], datetime]:
        """Assess the latest wait without letting semantic model text authorize a reopen."""
        cutoff = _aware_utc(minimum_as_of, label="wait assessment cutoff")
        if prior is None:
            return (), cutoff
        current_hashes = {item.research_id: item.content_hash for item in bundle.documents}
        prior_hashes = set(prior.prior_evidence_content_hashes)
        new_evidence = tuple(
            sorted(
                research_id
                for research_id, content_hash in current_hashes.items()
                if content_hash not in prior_hashes
            )
        )
        price_symbols = tuple(
            sorted({item.symbol for item in prior.triggers if item.symbol is not None})
        )
        quotes: dict[str, Quote | None] = {
            symbol: self._quote(symbol) for symbol in price_symbols
        }
        if price_symbols:
            cutoff = max(cutoff, _clock_time(self.market_data.get_clock()))
        assessments: list[WaitTriggerAssessment] = []
        for trigger in prior.triggers:
            if trigger.kind == "PRICE":
                assert trigger.symbol is not None
                quote = quotes[trigger.symbol]
                if quote is None:
                    assessments.append(
                        WaitTriggerAssessment(
                            trigger_id=trigger.trigger_id,
                            kind=trigger.kind,
                            description=trigger.description,
                            status="UNRESOLVED",
                            reason="No valid current quote was available.",
                            symbol=trigger.symbol,
                            comparison=trigger.comparison,
                            target_price=trigger.target_price,
                        )
                    )
                    continue
                try:
                    _validate_valuation_quote(
                        trigger.symbol,
                        quote,
                        as_of=cutoff,
                        max_age_seconds=self.assumptions.max_quote_age_seconds,
                    )
                except ValueError as exc:
                    assessments.append(
                        WaitTriggerAssessment(
                            trigger_id=trigger.trigger_id,
                            kind=trigger.kind,
                            description=trigger.description,
                            status="UNRESOLVED",
                            reason=str(exc),
                            symbol=trigger.symbol,
                            comparison=trigger.comparison,
                            target_price=trigger.target_price,
                        )
                    )
                    continue
                current = (quote.bid + quote.ask) / Decimal("2")
                assert trigger.target_price is not None and trigger.comparison is not None
                satisfied = (
                    current <= trigger.target_price
                    if trigger.comparison == "AT_OR_BELOW"
                    else current >= trigger.target_price
                )
                assessments.append(
                    WaitTriggerAssessment(
                        trigger_id=trigger.trigger_id,
                        kind=trigger.kind,
                        description=trigger.description,
                        status="SATISFIED" if satisfied else "UNSATISFIED",
                        reason=(
                            f"Midpoint {current} {'meets' if satisfied else 'does not meet'} "
                            f"{trigger.comparison} {trigger.target_price}."
                        ),
                        symbol=trigger.symbol,
                        current_price=current,
                        comparison=trigger.comparison,
                        target_price=trigger.target_price,
                    )
                )
            elif trigger.kind == "EVIDENCE":
                assessments.append(
                    WaitTriggerAssessment(
                        trigger_id=trigger.trigger_id,
                        kind=trigger.kind,
                        description=trigger.description,
                        status="SATISFIED" if new_evidence else "UNSATISFIED",
                        reason=(
                            f"{len(new_evidence)} newly admitted source content item(s)."
                            if new_evidence
                            else "No newly admitted source content was found."
                        ),
                    )
                )
            else:
                assessments.append(
                    WaitTriggerAssessment(
                        trigger_id=trigger.trigger_id,
                        kind=trigger.kind,
                        description=trigger.description,
                        status="UNRESOLVED",
                        reason="Named events are not machine-resolved in daily book context.",
                    )
                )
        review_due = prior.reconsider_at is not None and cutoff >= prior.reconsider_at
        reopenable = review_due or bool(new_evidence) or any(
            item.status == "SATISFIED" for item in assessments
        )
        return (
            (
                WaitingDecisionMemory(
                    decision_id=prior.decision_id,
                    invocation_id=prior.invocation_id,
                    run_id=prior.run_id,
                    scheduled_for=prior.scheduled_for,
                    classification=prior.classification,
                    insufficient_evidence=prior.insufficient_evidence,
                    unavailable_data=prior.unavailable_data,
                    reconsider_at=prior.reconsider_at,
                    reconsider_on=prior.reconsider_on,
                    scope_symbols=prior.scope_symbols,
                    review_due=review_due,
                    new_evidence_ids=new_evidence,
                    trigger_assessments=tuple(assessments),
                    reopenable=reopenable,
                ),
            ),
            cutoff,
        )

    def _invoke(
        self,
        *,
        book: Book,
        evaluation_id: str,
        run_id: str,
        run_directory: Path,
        directory: Path,
        bundle: ResearchBundle,
        note: str,
        prompts: dict[str, str],
        daily_context: Callable[[AgentRoleConfig, tuple[NamedPacket, ...]], BookAgentContext],
    ) -> ProfileRunResult:
        def persist(decision: DailyDecision, invocation_id: str) -> None:
            persist_agent_decision(
                self.session,
                run_id,
                invocation_id,
                decision,
                book_id=book.id,
                book_evaluation_id=evaluation_id,
                commit=False,
            )
            for proposal in decision.proposals:
                persist_trade_proposal(
                    self.session,
                    run_id,
                    proposal,
                    agent_invocation_id=invocation_id,
                    book_id=book.id,
                    commit=False,
                )

        return run_profile(
            self.session,
            self.config,
            self.catalog,
            book.process_profile,
            run_id=run_id,
            run_directory=run_directory,
            artifact_directory=directory / "profile",
            provider=self.provider,
            step_prefix=_step_name(book),
            skeleton_prompts=prompts,
            operating_note=note,
            assemble_research_context=partial(project_research_context, bundle),
            assemble_daily_context=daily_context,
            on_decision=persist,
            book_id=book.id,
            book_evaluation_id=evaluation_id,
        )

    def _prompts(self, profile_name: str) -> dict[str, str]:
        prompts: dict[str, str] = {}
        for step in self.catalog.profiles[profile_name].steps:
            role, _ = resolve_role(self.config, step.role)
            path = (self.project_root / (step.prompt or role.prompt)).resolve()
            if not path.is_relative_to(self.project_root / "prompts") or not path.is_file():
                raise ValueError(f"profile prompt is not a file inside project/prompts: {path}")
            prompts[step.step] = (
                self.prompt
                if step.role == "daily_trader" and step.prompt is None
                else path.read_text(encoding="utf-8")
            )
        return prompts

    def _configuration(
        self,
        book: Book,
        strategy: str,
        note: str,
        prompts: dict[str, str],
    ) -> dict[str, object]:
        profile = self.catalog.profiles[book.process_profile]
        roles = {step.role for step in profile.steps}
        return {
            "execution": "simulated",
            "schema_version": 1,
            "starting_cash": book.starting_cash,
            "profile_name": book.process_profile,
            "profile": profile.model_dump(mode="json"),
            "strategy_hash": _hash(strategy.encode()),
            "operating_note_hash": _hash(note.encode()),
            "portfolio_policy_hash": _hash(self.portfolio_policy.encode()),
            "prompts": {
                name: _hash(compose_prompt(text, note).encode()) for name, text in prompts.items()
            },
            "roles": {
                name: self.config.roles[name].model_dump(mode="json") for name in sorted(roles)
            },
            "models": {
                name: self.config.model_profiles[self.config.roles[name].profile].model_dump(
                    mode="json"
                )
                for name in sorted(roles)
            },
            "model_identity_pinned": all(
                self.config.model_profiles[self.config.roles[name].profile].model is not None
                for name in roles
            ),
            "provider": self.provider.provider_name,
            "risk_config": self.risk_config.model_dump(mode="json"),
            "fill_assumptions": self.assumptions.model_dump(mode="json"),
        }

    def _authorize(
        self,
        *,
        run_id: str,
        state: BookState,
        proposals: tuple[TradeProposal, ...],
        allowed_symbols: frozenset[str],
        as_of: datetime,
    ) -> tuple[tuple[RiskDecision, ...], dict[str, Quote], datetime]:
        if not proposals:
            return (), {}, as_of
        # A book's own holdings are admissible to it exactly as the portfolio's holdings are
        # admissible to the live line. Without this a variant cannot exit a position once its
        # symbol leaves the shared slate, and its curve measures the slate rather than its process.
        allowed_symbols = allowed_symbols | {position.symbol for position in state.positions}
        symbols = tuple(sorted({proposal.symbol for proposal in proposals}))
        quotes = {symbol: self.market_data.get_quote(symbol) for symbol in symbols}
        assets = {symbol: self.market_data.get_asset(symbol) for symbol in symbols}
        clock = self.market_data.get_clock()
        market_as_of = max(_aware_utc(as_of, label="book decision cutoff"), _clock_time(clock))

        prices, valuation_as_of = self._prices(
            tuple(position.symbol for position in state.positions),
            minimum_as_of=market_as_of,
        )
        market_as_of = max(market_as_of, valuation_as_of)
        orders_today, exposure_today, trades_today = self._activity(state.book_id, market_as_of)
        risk_context = RiskContext(
            as_of=market_as_of,
            account=state.account(prices),
            positions=list(state.broker_positions(prices)),
            open_orders=[],
            quotes=quotes,
            assets=assets,
            proposals=list(proposals),
            policy=self.risk_config.to_policy(allowed_symbols=allowed_symbols),
            daily_drawdown_pct=Decimal("0"),
            weekly_drawdown_pct=Decimal("0"),
            peak_drawdown_pct=self._peak_drawdown(state, prices, as_of=market_as_of),
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
        return decisions, quotes, market_as_of

    def _settle(
        self,
        *,
        book: Book,
        run_id: str,
        state: BookState,
        decisions: tuple[RiskDecision, ...],
        quotes: dict[str, Quote],
        as_of: datetime,
    ) -> tuple[tuple[SimulatedFillResult, ...], BookState]:
        results: list[SimulatedFillResult] = []
        for decision in decisions:
            if not decision.approved or decision.normalized_order is None:
                continue
            symbol = decision.normalized_order.symbol
            quote = quotes.get(symbol)
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

    def _prices(
        self,
        symbols: tuple[str, ...],
        *,
        minimum_as_of: datetime,
    ) -> tuple[dict[str, Decimal], datetime]:
        """Mark holdings from valid fresh quotes at a causal, broker-reported cutoff."""
        cutoff = _aware_utc(minimum_as_of, label="valuation cutoff")
        unique_symbols = tuple(sorted(set(symbols)))
        if not unique_symbols:
            return {}, cutoff
        quotes: dict[str, Quote] = {}
        for symbol in unique_symbols:
            quote = self._quote(symbol)
            if quote is None:
                raise ValueError(f"cannot price {symbol}: no quote is available")
            quotes[symbol] = quote
        cutoff = max(cutoff, _clock_time(self.market_data.get_clock()))
        prices: dict[str, Decimal] = {}
        for symbol, quote in quotes.items():
            _validate_valuation_quote(
                symbol,
                quote,
                as_of=cutoff,
                max_age_seconds=self.assumptions.max_quote_age_seconds,
            )
            prices[symbol] = ((quote.bid + quote.ask) / Decimal("2")).quantize(
                CENT,
                rounding=ROUND_HALF_UP,
            )
        return prices, cutoff

    def _quote(self, symbol: str) -> Quote | None:
        try:
            return self.market_data.get_quote(symbol)
        except Exception:  # noqa: BLE001 - an unpriceable symbol refuses to fill, see simulator
            return None

    def _activity(self, book_id: str, as_of: datetime) -> tuple[int, Decimal, dict[str, int]]:
        """Count what this book already did today, so per-day risk limits bind for a book too.

        The trading day is Eastern, matching the live line. A UTC day would roll over at 20:00
        Eastern and silently reset a book's per-day counters partway through a long book stage.
        """
        local_date = as_of.astimezone(EASTERN).date()
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
            if moment.astimezone(EASTERN).date() != local_date:
                continue
            count += 1
            trades[record.symbol.upper()] += 1
            if record.side.lower() == "buy":
                exposure += Decimal(record.qty) * Decimal(record.price)
        return count, exposure, dict(trades)

    def _peak_drawdown(
        self,
        state: BookState,
        prices: dict[str, Decimal],
        *,
        as_of: datetime,
    ) -> Decimal:
        """Measure the book's drawdown against its own equity peak, not the portfolio's."""
        equities = [
            Decimal(record.equity)
            for record in self.session.scalars(
                select(PerformanceSnapshot).where(
                    PerformanceSnapshot.book_id == state.book_id,
                    PerformanceSnapshot.period == "daily",
                    PerformanceSnapshot.captured_at < as_of,
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
    invocations = session.scalars(
        select(AgentInvocation)
        .where(AgentInvocation.book_id == book.id)
        .order_by(AgentInvocation.started_at, AgentInvocation.id)
    ).all()
    by_model: dict[str, dict[str, int]] = {}
    for invocation in invocations:
        totals = by_model.setdefault(
            invocation.model,
            {
                "invocation_count": 0,
                "cached_input_reported_count": 0,
                "reasoning_output_reported_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
            },
        )
        totals["invocation_count"] += 1
        totals["cached_input_reported_count"] += (
            invocation.cached_input_token_count is not None
        )
        totals["reasoning_output_reported_count"] += (
            invocation.reasoning_output_token_count is not None
        )
        totals["input_tokens"] += invocation.input_token_count or 0
        totals["cached_input_tokens"] += invocation.cached_input_token_count or 0
        totals["output_tokens"] += invocation.output_token_count or 0
        totals["reasoning_output_tokens"] += invocation.reasoning_output_token_count or 0
        totals["total_tokens"] += (
            invocation.total_token_count
            if invocation.total_token_count is not None
            else (invocation.input_token_count or 0) + (invocation.output_token_count or 0)
        )
    return {
        "book": book.name,
        "status": book.status,
        "process_profile": book.process_profile,
        "operating_note_path": book.operating_note_path,
        "experiment_phases": [
            {
                "id": phase.id,
                "ordinal": phase.ordinal,
                "configuration_hash": phase.configuration_hash,
                "configuration": json.loads(phase.manifest_json),
            }
            for phase in session.scalars(
                select(BookExperimentPhase)
                .where(BookExperimentPhase.book_id == book.id)
                .order_by(BookExperimentPhase.ordinal)
            )
        ],
        "evaluations": [
            {
                "id": item.id,
                "run_id": item.run_id,
                "phase_id": item.phase_id,
                "as_of": item.as_of.isoformat(),
                "status": item.status,
                "terminal_invocation_id": item.terminal_invocation_id,
                "error": item.error,
            }
            for item in session.scalars(
                select(BookEvaluation)
                .where(BookEvaluation.book_id == book.id)
                .order_by(BookEvaluation.as_of, BookEvaluation.id)
            )
        ],
        "strategy_document_path": book.strategy_document_path,
        "strategy_content_hash": book.strategy_content_hash,
        "starting_cash": book.starting_cash,
        "proposal_count": len(proposals),
        "fill_count": len(fills),
        "model_usage": {
            "invocation_count": len(invocations),
            "usage_reported_count": sum(
                item.input_token_count is not None and item.output_token_count is not None
                for item in invocations
            ),
            "by_model": by_model,
            "invocations": [
                {
                    "invocation_id": item.id,
                    "run_id": item.run_id,
                    "evaluation_id": item.book_evaluation_id,
                    "role": item.role,
                    "step": item.step,
                    "model": item.model,
                    "model_profile": item.model_profile,
                    "reasoning_effort": item.reasoning_effort,
                    "status": item.status,
                    "started_at": item.started_at.isoformat(),
                    "completed_at": (
                        None if item.completed_at is None else item.completed_at.isoformat()
                    ),
                    "usage": None if item.usage_json is None else json.loads(item.usage_json),
                    "cost_usd": item.cost_usd,
                    "cost_source": item.cost_source,
                    "pricing": (
                        None if item.pricing_json is None else json.loads(item.pricing_json)
                    ),
                }
                for item in invocations
            ],
        },
        "references": [
            {
                "run_id": point.run_id,
                "evaluation_id": point.evaluation_id,
                "kind": point.kind,
                "symbol": point.symbol,
                "as_of": point.as_of.isoformat(),
                "status": point.status,
                "equity": point.equity,
                "cash": point.cash,
                "quantity": point.quantity,
                "entry_price": point.entry_price,
                "mark_price": point.mark_price,
                "error": point.error,
            }
            for point in session.scalars(
                select(BookReferencePoint)
                .join(BookEvaluation, BookEvaluation.id == BookReferencePoint.evaluation_id)
                .where(
                    BookReferencePoint.book_id == book.id,
                    BookEvaluation.status == "COMPLETED",
                )
                .order_by(BookReferencePoint.as_of, BookReferencePoint.kind)
            )
        ],
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


class BookEvaluationService(Protocol):
    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        as_of: datetime,
        scan: UniverseScan,
        research: ResearchRunResult,
        allowed_symbols: frozenset[str],
    ) -> BookEvaluationResult: ...


class _ConfiguredBookEvaluation:
    """Resolve book configuration inside the isolated book stage, after incumbent execution."""

    def __init__(
        self,
        settings: Settings,
        session: Session,
        market_data: MarketDataSource,
        provider: StructuredReasoningProvider | None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.market_data = market_data
        self.provider = provider

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
        try:
            settings = self.settings
            config = load_agent_config(settings.trader_agents_config)
            if not config.automatic_daily_run:
                raise ValueError("automatic book reasoning is disabled")
            root = settings.trader_agents_config.resolve().parent.parent
            catalog_bytes = settings.trader_pipelines_config.read_bytes()
            catalog = load_pipeline_catalog(
                settings.trader_pipelines_config,
                config,
                project_root=root,
            )
            if catalog_bytes != settings.trader_pipelines_config.read_bytes():
                raise ValueError("pipeline catalog changed during configuration load")
            prompt = resolved_prompt_path(
                settings.trader_agents_config, config.roles["daily_trader"]
            )
            evaluator = BookEvaluationPipeline(
                self.session,
                config,
                self.market_data,
                load_risk_config(settings.trader_risk_config),
                prompt=prompt.read_text(encoding="utf-8"),
                portfolio_policy=settings.trader_portfolio_policy.read_text(encoding="utf-8"),
                provider=self.provider or CodexCLIProvider(),
                project_root=root,
                catalog=catalog,
                catalog_bytes=catalog_bytes,
            )
            return evaluator.run(
                run_id=run_id,
                run_directory=run_directory,
                as_of=as_of,
                scan=scan,
                research=research,
                allowed_symbols=allowed_symbols,
            )
        except Exception as error:
            self.session.rollback()
            result = BookEvaluationResult(
                summaries=(),
                failures=tuple(
                    BookFailure(book_name=book.name, reason=str(error))
                    for book in list_books(self.session, status="active")
                ),
            )
            # Preserve prior audit if this was a duplicate call. A new run records the failure.
            summary_path = run_directory / "books_summary.json"
            if not summary_path.exists():
                _write_json(summary_path, result.summary())
            return result


def configured_book_evaluation_pipeline(
    settings: Settings,
    session: Session,
    market_data: MarketDataSource,
    *,
    provider: StructuredReasoningProvider | None = None,
) -> BookEvaluationService | None:
    """Keep optional book-specific configuration out of incumbent run construction."""
    if not settings.trader_reasoning_enabled or not list_books(session, status="active"):
        return None
    return _ConfiguredBookEvaluation(settings, session, market_data, provider)


def _step_name(book: Book) -> str:
    """A stable book UUID disambiguates slugs that overlap with catalog step names."""
    slug = normalize_book_name(book.name).replace("-", "_")
    return "book_" + book.id.replace("-", "") + "_" + slug + "_"


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, default=str, indent=2, sort_keys=True) + "\n")


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _clock_time(clock: MarketClock) -> datetime:
    return _aware_utc(clock.timestamp, label="market clock timestamp")


def _validate_valuation_quote(
    symbol: str,
    quote: Quote,
    *,
    as_of: datetime,
    max_age_seconds: int,
) -> None:
    if (
        quote.symbol.upper().strip() != symbol
        or not isinstance(quote.bid, Decimal)
        or not isinstance(quote.ask, Decimal)
        or not quote.bid.is_finite()
        or not quote.ask.is_finite()
        or quote.bid <= 0
        or quote.ask <= 0
        or quote.ask < quote.bid
    ):
        raise ValueError(f"cannot price {symbol}: invalid quote")
    quote_at = _aware_utc(quote.timestamp, label=f"{symbol} quote timestamp")
    age = (as_of - quote_at).total_seconds()
    if age < 0:
        raise ValueError(f"cannot price {symbol}: quote postdates the valuation cutoff")
    if age > max_age_seconds:
        raise ValueError(
            f"cannot price {symbol}: quote is {int(age)}s old, beyond the "
            f"{max_age_seconds}s cap"
        )


@cache
def _implementation_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(
            path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0"
        )
    return digest.hexdigest()
