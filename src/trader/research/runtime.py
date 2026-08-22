"""Production wiring for the bounded Alpaca and SEC shadow-research pipeline."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from trader.research.alpaca import AlpacaResearchProvider
from trader.research.collection import BoundedResearchCollector, ResearchProvider
from trader.research.config import ResearchConfig, load_research_config
from trader.research.models import ResearchPlan
from trader.research.planner import ResearchPlanner
from trader.research.sec import (
    SecResearchProvider,
    SecTickerMapResolver,
    SecTickerMapSnapshot,
)
from trader.research.service import ResearchRunResult, ShadowResearchPipeline
from trader.settings import Settings
from trader.universe.models import UniverseScan


class SecTickerResolver(Protocol):
    def resolve(self, *, retrieved_at: datetime | None = None) -> SecTickerMapSnapshot: ...


SecProviderFactory = Callable[[Mapping[str, str]], ResearchProvider]


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
    ) -> None:
        self.session = session
        self.config = config
        self.alpaca_provider = alpaca_provider
        self.sec_resolver = sec_resolver
        self.sec_provider_factory = sec_provider_factory

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
        setup_started = time.monotonic()
        snapshot = self.sec_resolver.resolve()
        setup_elapsed = time.monotonic() - setup_started
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

        collector = BoundedResearchCollector(
            {
                "MARKET_CONTEXT": self.alpaca_provider,
                "COMPANY_NEWS": self.alpaca_provider,
                "SEC_FILINGS": self.sec_provider_factory(snapshot.symbol_to_cik),
            },
            max_questions=_maximum_question_count(self.config),
            max_http_requests=remaining_requests,
            max_documents=self.config.collection.max_total_items,
            max_response_bytes=remaining_bytes,
        )
        pipeline = ShadowResearchPipeline(
            self.session,
            self._planner(snapshot),
            collector,
            self.config,
            reference_payloads={
                "sec/company_tickers.json": snapshot.raw_payload,
                "sec/company_tickers.normalized.json": snapshot.canonical_payload,
            },
            setup_request_count=snapshot.request_count,
            setup_response_bytes=snapshot.response_bytes,
            setup_elapsed_seconds=setup_elapsed,
        )
        return pipeline.run(
            run_id=run_id,
            run_directory=run_directory,
            scan=scan,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
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
        )

    return SecResolvedResearchPipeline(
        session,
        config,
        alpaca_provider,
        resolver,
        sec_provider,
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


def _estimated_collection_requests(plan: ResearchPlan, *, sec_attempts: int) -> int:
    request_cost = {
        "MARKET_CONTEXT": 2,
        "COMPANY_NEWS": 1,
        "SEC_FILINGS": 2 * sec_attempts,
    }
    return sum(request_cost[question.question_type] for question in plan.questions)
