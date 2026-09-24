"""Production wiring for the bounded Alpaca and SEC shadow-research pipeline."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from trader.research.alpaca import AlpacaResearchProvider
from trader.research.collection import (
    BoundedResearchCollector,
    ResearchCollection,
    ResearchProvider,
)
from trader.research.config import ResearchConfig, load_research_config
from trader.research.models import QuestionType, ResearchPlan, ResearchQuestion
from trader.research.planner import ResearchPlanner
from trader.research.sec import (
    SecResearchProvider,
    SecTickerMapResolver,
    SecTickerMapSnapshot,
)
from trader.research.selection import select_deep_symbols
from trader.research.service import (
    ResearchPipelineError,
    ResearchRunResult,
    ShadowResearchPipeline,
    extend_research_result,
)
from trader.risk.config import load_risk_config
from trader.settings import Settings
from trader.universe.models import UniverseScan


class SecTickerResolver(Protocol):
    def resolve(self, *, retrieved_at: datetime | None = None) -> SecTickerMapSnapshot: ...


SecProviderFactory = Callable[[Mapping[str, str]], ResearchProvider]


class FollowUpResearchPlanner(Protocol):
    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        scan: UniverseScan,
        research: ResearchRunResult,
    ) -> FollowUpPlanningOutcome: ...


class FollowUpPlanningOutcome(Protocol):
    invocation_id: str
    questions: tuple[ResearchQuestion, ...]


FollowUpPlannerFactory = Callable[[frozenset[str]], FollowUpResearchPlanner]


@dataclass(frozen=True)
class ResearchPlanPreview:
    """A read-only plan preview plus the reference data used to produce it."""

    plan: ResearchPlan
    sec_ticker_map: SecTickerMapSnapshot
    estimated_collection_requests: int

    @property
    def estimated_total_requests(self) -> int:
        return self.sec_ticker_map.request_count + self.estimated_collection_requests

    def summary(self) -> dict[str, object]:
        return {
            "mode": "shadow",
            "sec_ticker_map": self.sec_ticker_map.summary(),
            "candidate_symbols": list(self.plan.candidate_symbols),
            "deep_symbols": list(self.plan.deep_symbols),
            "questions": [question.model_dump(mode="json") for question in self.plan.questions],
            "estimated_collection_requests": self.estimated_collection_requests,
            "estimated_total_requests": self.estimated_total_requests,
            "preview_only": True,
        }


class SecResolvedResearchPipeline:
    """Resolve official CIK data, then run the configured shadow pipeline."""

    def __init__(
        self,
        session: Session,
        config: ResearchConfig,
        alpaca_provider: ResearchProvider,
        sec_resolver: SecTickerResolver,
        sec_provider_factory: SecProviderFactory,
        *,
        min_price: Decimal = Decimal("5"),
        min_average_daily_dollar_volume: Decimal = Decimal("5000000"),
        follow_up_planner_factory: FollowUpPlannerFactory | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.alpaca_provider = alpaca_provider
        self.sec_resolver = sec_resolver
        self.sec_provider_factory = sec_provider_factory
        self.min_price = min_price
        self.min_average_daily_dollar_volume = min_average_daily_dollar_volume
        self.follow_up_planner_factory = follow_up_planner_factory

    def preview(
        self,
        scan: UniverseScan,
        *,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlanPreview:
        snapshot = self.sec_resolver.resolve()
        planner = self._planner(snapshot)
        plan = planner.plan(
            scan,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
        )
        return ResearchPlanPreview(
            plan=plan,
            sec_ticker_map=snapshot,
            estimated_collection_requests=_estimated_collection_requests(
                plan,
                sec_attempts=self.config.collection.max_retries_per_request + 1,
                primary_documents=(
                    self.config.collection.max_primary_filings_per_symbol
                ),
            ),
        )

    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        scan: UniverseScan,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchRunResult:
        overall_started = time.monotonic()
        snapshot = self.sec_resolver.resolve()
        remaining_requests = (
            self.config.collection.max_total_requests - snapshot.request_count
        )
        remaining_bytes = (
            self.config.collection.max_total_response_bytes - snapshot.response_bytes
        )
        if remaining_requests < 1:
            raise RuntimeError("SEC ticker-map lookup exhausted the research request budget")
        if remaining_bytes < 1:
            raise RuntimeError("SEC ticker-map lookup exhausted the research response-byte budget")

        sec_provider = self.sec_provider_factory(snapshot.symbol_to_cik)
        providers: dict[QuestionType, ResearchProvider] = {
            "MARKET_CONTEXT": self.alpaca_provider,
            "COMPANY_NEWS": self.alpaca_provider,
            "SEC_FILINGS": sec_provider,
            "SEC_FILING_HISTORY": sec_provider,
        }
        planner = self._planner(snapshot)
        fast_plan = planner.fast_plan(
            scan,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
        )
        fast_collector = BoundedResearchCollector(
            providers,
            max_questions=_maximum_question_count(self.config),
            max_http_requests=remaining_requests,
            max_documents=self.config.collection.max_total_items,
            max_response_bytes=remaining_bytes,
        )
        fast_collection = fast_collector.collect(fast_plan.questions)
        selection = select_deep_symbols(
            ordered_symbols=fast_plan.candidate_symbols,
            assets={asset.symbol: asset for asset in scan.eligible_assets},
            fast_collection=fast_collection,
            pinned_symbols=frozenset(portfolio_symbols).union(event_symbols),
            max_symbols=self.config.selection.max_deep_symbols,
            min_price=self.min_price,
            min_average_daily_dollar_volume=self.min_average_daily_dollar_volume,
        )
        plan = planner.plan_with_deep_symbols(
            scan,
            deep_symbols=selection.selected_symbols,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
        )
        fast_ids = {question.question_id for question in fast_plan.questions}
        deep_questions = tuple(
            question for question in plan.questions if question.question_id not in fast_ids
        )
        deep_collection = _collect_remaining(
            deep_questions,
            providers=providers,
            config=self.config,
            setup_request_count=snapshot.request_count,
            setup_response_bytes=snapshot.response_bytes,
            prior=fast_collection,
        )
        collection = _combine_collections(fast_collection, deep_collection)
        pipeline = ShadowResearchPipeline(
            self.session,
            _StaticPlanBuilder(plan),
            _PrecollectedCollector(collection),
            self.config,
            reference_payloads={
                "sec/company_tickers.json": snapshot.raw_payload,
                "sec/company_tickers.normalized.json": snapshot.canonical_payload,
            },
            setup_request_count=snapshot.request_count,
            setup_response_bytes=snapshot.response_bytes,
            setup_elapsed_seconds=time.monotonic() - overall_started,
        )
        result = pipeline.run(
            run_id=run_id,
            run_directory=run_directory,
            scan=scan,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
        )
        result = replace(result, deep_selection=selection.assessments)
        if self.follow_up_planner_factory is None or not self.config.follow_up.enabled:
            return result
        follow_up_planner = self.follow_up_planner_factory(
            frozenset(snapshot.symbol_to_cik)
        )
        planned = follow_up_planner.run(
            run_id=run_id,
            run_directory=run_directory,
            scan=scan,
            research=result,
        )
        questions = tuple(planned.questions)
        invocation_id = planned.invocation_id
        requested_count = len(questions)
        questions = _truncate_to_request_budget(
            questions,
            providers=providers,
            remaining_requests=(
                self.config.collection.max_total_requests
                - result.total_request_count
            ),
        )
        elapsed = time.monotonic() - overall_started
        if elapsed > self.config.collection.max_wall_clock_seconds:
            raise ResearchPipelineError(
                "research follow-up planning exceeded the run wall-clock budget"
            )
        if not questions:
            return replace(
                result,
                elapsed_seconds=elapsed,
                follow_up_invocation_id=invocation_id,
                follow_up_requested_count=requested_count,
            )
        follow_up_collection = _collect_remaining(
            questions,
            providers=providers,
            config=self.config,
            setup_request_count=result.setup_request_count,
            setup_response_bytes=result.setup_response_bytes,
            prior=result.collection,
        )
        new_deep_symbols = tuple(
            dict.fromkeys(
                question.symbol
                for question in questions
                if question.symbol not in result.plan.deep_symbols
            )
        )
        return extend_research_result(
            self.session,
            run_id=run_id,
            run_directory=run_directory,
            base=result,
            questions=questions,
            collection=follow_up_collection,
            config=self.config,
            new_deep_symbols=new_deep_symbols,
            follow_up_invocation_id=invocation_id,
            requested_count=requested_count,
            elapsed_seconds=time.monotonic() - overall_started,
        )

    def _planner(self, snapshot: SecTickerMapSnapshot) -> ResearchPlanner:
        return ResearchPlanner(
            self.config,
            sec_symbols=frozenset(snapshot.symbol_to_cik),
        )


def configured_research_pipeline(
    settings: Settings,
    session: Session,
    *,
    verify_schema: bool = True,
) -> SecResolvedResearchPipeline:
    """Build production research components without performing network I/O."""

    if verify_schema:
        verify_research_schema(session)
    config = load_research_config(settings.trader_research_config)
    risk = load_risk_config(settings.trader_risk_config)
    key, secret = settings.require_broker_credentials()
    user_agent = settings.require_sec_user_agent()
    max_attempts = config.collection.max_retries_per_request + 1
    alpaca_provider = AlpacaResearchProvider(
        key,
        secret,
        max_news_items=min(50, config.collection.max_items_per_symbol),
    )
    resolver = SecTickerMapResolver(
        user_agent=user_agent,
        max_response_bytes=config.collection.max_response_bytes,
        max_attempts=max_attempts,
    )

    def sec_provider(symbol_to_cik: Mapping[str, str]) -> ResearchProvider:
        return SecResearchProvider(
            user_agent=user_agent,
            symbol_to_cik=symbol_to_cik,
            max_response_bytes=config.collection.max_response_bytes,
            max_attempts=max_attempts,
            max_filings=min(100, config.collection.max_items_per_symbol),
            max_primary_documents=config.collection.max_primary_filings_per_symbol,
        )

    follow_up_factory: FollowUpPlannerFactory | None = None
    if settings.trader_reasoning_enabled and config.follow_up.enabled:
        from trader.agent.codex_cli import CodexCLIProvider
        from trader.agent.config import load_agent_config, resolved_prompt_path
        from trader.agent.research_planner import AgentFollowUpResearchPlanner

        agent_config = load_agent_config(settings.trader_agents_config)
        follow_up_role = agent_config.roles["research_planner"]
        follow_up_prompt = resolved_prompt_path(
            settings.trader_agents_config, follow_up_role
        ).read_text(encoding="utf-8")

        def build_follow_up(sec_symbols: frozenset[str]) -> FollowUpResearchPlanner:
            return cast(
                FollowUpResearchPlanner,
                AgentFollowUpResearchPlanner(
                    session,
                    agent_config,
                    config,
                    prompt=follow_up_prompt,
                    provider=CodexCLIProvider(),
                    sec_symbols=sec_symbols,
                ),
            )

        follow_up_factory = build_follow_up

    return SecResolvedResearchPipeline(
        session,
        config,
        alpaca_provider,
        resolver,
        sec_provider,
        min_price=risk.liquidity.min_price_usd,
        min_average_daily_dollar_volume=(
            risk.liquidity.min_average_daily_dollar_volume_usd
        ),
        follow_up_planner_factory=follow_up_factory,
    )


def verify_research_schema(session: Session) -> None:
    """Reject an outdated database before a daily run performs broker/network reads."""

    inspector = inspect(session.get_bind())
    required_tables = {
        "research_items",
        "research_item_symbols",
        "research_item_questions",
        "agent_invocation_evidence",
    }
    missing_tables = sorted(required_tables.difference(inspector.get_table_names()))
    research_columns = (
        {column["name"] for column in inspector.get_columns("research_items")}
        if "research_items" not in missing_tables
        else set()
    )
    required_columns = {
        "source_tier",
        "provider",
        "provider_item_id",
        "raw_artifact_path",
        "normalized_text",
    }
    missing_columns = sorted(required_columns.difference(research_columns))
    if missing_tables or missing_columns:
        detail = []
        if missing_tables:
            detail.append("missing tables: " + ", ".join(missing_tables))
        if missing_columns:
            detail.append("missing research_items columns: " + ", ".join(missing_columns))
        raise RuntimeError(
            "research database schema is outdated; run `uv run alembic "
            "-x database_url=sqlite:///data/paper/trader.db upgrade head` ("
            + "; ".join(detail)
            + ")"
        )


def _maximum_question_count(config: ResearchConfig) -> int:
    return config.selection.max_fast_candidates + (
        config.selection.max_deep_symbols
        * (config.selection.max_questions_per_symbol - 1)
    )


def _estimated_collection_requests(
    plan: ResearchPlan,
    *,
    sec_attempts: int,
    primary_documents: int,
) -> int:
    request_cost = {
        "MARKET_CONTEXT": 2,
        "COMPANY_NEWS": 1,
        "SEC_FILINGS": (2 + (3 * primary_documents)) * sec_attempts,
        "SEC_FILING_HISTORY": (1 + (3 * primary_documents)) * sec_attempts,
    }
    return sum(request_cost[question.question_type] for question in plan.questions)


class _StaticPlanBuilder:
    def __init__(self, plan: ResearchPlan) -> None:
        self._plan = plan

    def plan(
        self,
        scan: UniverseScan,
        *,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlan:
        del scan, portfolio_symbols, event_symbols
        return self._plan


class _PrecollectedCollector:
    def __init__(self, collection: ResearchCollection) -> None:
        self._collection = collection

    def collect(
        self,
        requests: Sequence[ResearchQuestion],
    ) -> ResearchCollection:
        del requests
        return self._collection


def _combine_collections(
    first: ResearchCollection,
    second: ResearchCollection,
) -> ResearchCollection:
    return ResearchCollection(
        batches=first.batches + second.batches,
        request_count=first.request_count + second.request_count,
        response_bytes=first.response_bytes + second.response_bytes,
    )


def _collect_remaining(
    questions: tuple[ResearchQuestion, ...],
    *,
    providers: Mapping[QuestionType, ResearchProvider],
    config: ResearchConfig,
    setup_request_count: int,
    setup_response_bytes: int,
    prior: ResearchCollection,
) -> ResearchCollection:
    if not questions:
        return ResearchCollection(batches=(), request_count=0, response_bytes=0)
    remaining_requests = (
        config.collection.max_total_requests
        - setup_request_count
        - prior.request_count
    )
    remaining_bytes = (
        config.collection.max_total_response_bytes
        - setup_response_bytes
        - prior.response_bytes
    )
    used_documents = len({document.research_id for document in prior.documents})
    remaining_documents = config.collection.max_total_items - used_documents
    if remaining_requests < 1 or remaining_bytes < 1 or remaining_documents < 1:
        raise RuntimeError("initial research exhausted the configured follow-up budget")
    collector = BoundedResearchCollector(
        providers,
        max_questions=max(1, len(questions)),
        max_http_requests=remaining_requests,
        max_documents=remaining_documents,
        max_response_bytes=remaining_bytes,
    )
    return collector.collect(questions)


def _truncate_to_request_budget(
    questions: tuple[ResearchQuestion, ...],
    *,
    providers: Mapping[QuestionType, ResearchProvider],
    remaining_requests: int,
) -> tuple[ResearchQuestion, ...]:
    selected: list[ResearchQuestion] = []
    used = 0
    for question in questions:
        provider = providers[question.question_type]
        estimate = provider.estimated_request_count(question)
        if used + estimate > remaining_requests:
            continue
        selected.append(question)
        used += estimate
    return tuple(selected)
