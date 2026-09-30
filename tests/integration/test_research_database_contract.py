"""Production research boundaries exercised against the real Alembic schema."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import get_args

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trader.agent.runner import daily_run
from trader.broker.models import Account, BrokerFill, BrokerOrder, OrderQueryStatus, Position
from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    Base,
    BrokerOrderRecord,
    DailyReport,
    ResearchItem,
    ResearchItemQuestion,
    Run,
    RunEvent,
)
from trader.persistence.repositories import available_research_as_of, persist_research_item
from trader.research.config import load_research_config
from trader.research.models import (
    ProviderName,
    QuestionType,
    ResearchBatch,
    ResearchDocument,
    ResearchRequest,
    SourceTier,
    SourceType,
)
from trader.research.runtime import SecResolvedResearchPipeline, verify_research_schema
from trader.research.sec import SEC_COMPANY_TICKERS_URL, SecTickerMapSnapshot
from trader.universe.models import CandidateSignal, ResearchCandidate, UniverseAsset, UniverseScan

ROOT = Path(__file__).parents[2]
AS_OF = datetime(2026, 9, 28, 19, 15, tzinfo=UTC)


def _migrated_database(tmp_path: Path, revision: str = "head") -> str:
    url = f"sqlite:///{tmp_path}/trader.sqlite"
    # A bare Alembic config avoids replacing pytest's process-wide log handlers.
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, revision)
    return url


def _document(
    request: ResearchRequest,
    *,
    provider: ProviderName,
    source_name: str,
    source_type: SourceType,
    source_tier: SourceTier,
    payload: bytes,
) -> ResearchDocument:
    return ResearchDocument.create(
        question_ids=(request.question_id,),
        provider=provider,
        source_type=source_type,
        source_tier=source_tier,
        source_name=source_name,
        provider_item_id=f"{source_name}:AAPL",
        url=None,
        author=None,
        published_at=AS_OF - timedelta(minutes=1),
        retrieved_at=AS_OF,
        symbols=("AAPL",),
        headline=f"AAPL {source_name}",
        normalized_text=payload.decode(),
        summary=None,
        raw_payload=payload,
    )


class FixtureResolver:
    def resolve(self, *, retrieved_at: datetime | None = None) -> SecTickerMapSnapshot:
        assert retrieved_at is None
        raw = b'{"0":{"ticker":"AAPL","cik_str":320193,"title":"Apple Inc."}}'
        canonical = b'{"AAPL":"0000320193"}'
        return SecTickerMapSnapshot(
            source_reference=SEC_COMPANY_TICKERS_URL,
            retrieved_at=AS_OF,
            symbol_to_cik=MappingProxyType({"AAPL": "0000320193"}),
            raw_payload=raw,
            raw_content_hash=hashlib.sha256(raw).hexdigest(),
            canonical_payload=canonical,
            canonical_content_hash=hashlib.sha256(canonical).hexdigest(),
            request_count=1,
            response_bytes=len(raw),
        )


class FixtureProvider:
    def __init__(self, provider_name: ProviderName) -> None:
        self.provider_name = provider_name
        self.supported_question_types: frozenset[QuestionType] = (
            frozenset({"MARKET_CONTEXT", "COMPANY_NEWS", "VALUATION_FACTS"})
            if provider_name == "alpaca"
            else frozenset({"SEC_FILINGS", "SEC_FILING_HISTORY"})
        )

    def estimated_request_count(self, request: ResearchRequest) -> int:
        return 2 if request.question_type == "MARKET_CONTEXT" else 1

    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch:
        assert retrieved_at is None
        if request.question_type == "MARKET_CONTEXT":
            payloads = (
                (
                    "Alpaca stock snapshot",
                    json.dumps(
                        {"AAPL": {"latestTrade": {"p": 25, "t": AS_OF.isoformat()}}}
                    ).encode(),
                ),
                (
                    "Alpaca adjusted daily bars",
                    b'{"AAPL":[{"c":25,"v":300000,"t":"2026-09-25T04:00:00Z"}]}',
                ),
            )
            documents = tuple(
                _document(
                    request,
                    provider="alpaca",
                    source_name=name,
                    source_type="MARKET_DATA",
                    source_tier="BROKER",
                    payload=payload,
                )
                for name, payload in payloads
            )
        elif request.question_type == "COMPANY_NEWS":
            documents = (
                _document(
                    request,
                    provider="alpaca",
                    source_name="Alpaca News",
                    source_type="NEWS",
                    source_tier="BROKER",
                    payload=b'{"headline":"AAPL filing update"}',
                ),
            )
        elif request.question_type == "VALUATION_FACTS":
            documents = (
                _document(
                    request,
                    provider="alpaca",
                    source_name="Alpaca adjusted monthly bars",
                    source_type="MARKET_DATA",
                    source_tier="BROKER",
                    payload=b'{"AAPL":[{"c":20,"t":"2026-08-01T04:00:00Z"}]}',
                ),
            )
        else:
            assert request.question_type == "SEC_FILINGS"
            documents = (
                _document(
                    request,
                    provider="sec",
                    source_name="SEC XBRL company facts",
                    source_type="REGULATORY_FILING",
                    source_tier="PRIMARY",
                    payload=b'{"facts":{}}',
                ),
            )
        return ResearchBatch(
            provider=self.provider_name,
            retrieved_at=AS_OF,
            question_ids=(request.question_id,),
            documents=documents,
            request_count=len(documents),
            response_bytes=sum(len(document.raw_payload) for document in documents),
            cost_usd=Decimal("0"),
        )


def _scan() -> UniverseScan:
    asset = UniverseAsset(
        symbol="AAPL", name="Apple Inc.", asset_class="us_equity", status="active", tradable=True
    )
    return UniverseScan(
        as_of=AS_OF,
        asset_content_hash="a" * 64,
        eligible_assets=(asset,),
        candidates=(
            ResearchCandidate(
                symbol="AAPL",
                score=100,
                asset=asset,
                signals=(CandidateSignal(source="PORTFOLIO"),),
            ),
        ),
        most_active_volume_updated_at=AS_OF,
        most_active_trades_updated_at=AS_OF,
        market_movers_updated_at=AS_OF,
        skipped_screener_symbols=0,
    )


class FixtureBroker:
    is_paper = True
    paper_options_level_is_provider_managed = False

    def get_account(self) -> Account:
        return Account(equity="2000", cash="2000", buying_power="2000", options_level=0)

    def get_positions(self) -> list[Position]:
        return []

    def get_open_orders(self) -> list[BrokerOrder]:
        return []

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
        self, *, after: datetime | None = None, until: datetime | None = None
    ) -> list[BrokerFill]:
        del after, until
        return []


class FixtureScanner:
    def scan(
        self, *, as_of: datetime, portfolio_symbols: tuple[str, ...]
    ) -> UniverseScan:
        assert as_of == AS_OF
        assert portfolio_symbols == ()
        return _scan()


def _runtime(session: Session) -> SecResolvedResearchPipeline:
    return SecResolvedResearchPipeline(
        session,
        load_research_config(ROOT / "config/research.yaml"),
        FixtureProvider("alpaca"),
        FixtureResolver(),
        lambda _mapping: FixtureProvider("sec"),
    )


def test_staged_research_persists_computed_valuation_on_migrated_database(
    tmp_path: Path,
) -> None:
    url = _migrated_database(tmp_path)
    engine = create_engine(url)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []

    with create_session_factory(url, create_schema=False)() as session:
        verify_research_schema(session)
        run = Run(run_key="daily:integration", scheduled_for=AS_OF, config_hash="config")
        session.add(run)
        session.commit()
        runtime = _runtime(session)
        result = runtime.run(
            run_id=run.id,
            run_directory=tmp_path / "run",
            scan=_scan(),
            portfolio_symbols=("AAPL",),
        )

        derived = session.scalars(
            select(ResearchItem).where(ResearchItem.source_tier == "DERIVED")
        ).one()
        assert result.plan.deep_symbols == ("AAPL",)
        assert derived.provider == "computed"
        assert derived.source_type == "DERIVED_FACTS"
        assert derived.id in result.persisted_research_ids
        assert json.loads(derived.normalized_text)["current_metrics"]["price"] == {
            "status": "available", "value": "25"
        }
        computed_document = next(
            doc for doc in result.collection.documents if doc.provider == "computed"
        )
        assert (tmp_path / "run" / derived.raw_artifact_path).read_bytes() == (
            computed_document.raw_payload
        )
        assert any(
            link.research_id == derived.id
            for link in session.scalars(select(ResearchItemQuestion)).all()
        )
        assert derived.id in {
            item.id
            for item in available_research_as_of(
                session, AS_OF + timedelta(minutes=1), run_id=run.id
            )
        }


def test_daily_run_completes_through_report_with_migrated_research(
    tmp_path: Path,
) -> None:
    url = _migrated_database(tmp_path)
    with create_session_factory(url, create_schema=False)() as session:
        verify_research_schema(session)
        run_id = daily_run(
            session,
            FixtureBroker(),
            tmp_path / "raw",
            b"mode: paper\n",
            scheduled_for=AS_OF,
            candidate_scanner=FixtureScanner(),
            research_pipeline=_runtime(session),
        )

        run = session.get(Run, run_id)
        assert run is not None and run.status == "COMPLETED"
        assert session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.stage == "COLLECT_SHADOW_RESEARCH"
            )
        ) is not None
        assert session.scalar(
            select(DailyReport).where(DailyReport.run_id == run_id)
        ) is not None
        assert session.scalar(select(BrokerOrderRecord)) is None
        derived = session.scalars(
            select(ResearchItem).where(
                ResearchItem.run_id == run_id, ResearchItem.source_tier == "DERIVED"
            )
        ).one()
        manifest = json.loads(
            (tmp_path / "raw/paper/runs" / run_id / "manifest.json").read_text()
        )
        assert derived.raw_artifact_path in manifest


def test_research_preflight_rejects_outdated_revision_before_collection(
    tmp_path: Path,
) -> None:
    url = _migrated_database(tmp_path, revision="b83e219fc641")
    with (
        create_session_factory(url, create_schema=False)() as session,
        pytest.raises(RuntimeError, match="database revisions.*b83e219fc641"),
    ):
        verify_research_schema(session)


def test_every_admitted_research_tier_persists_on_migrated_database(tmp_path: Path) -> None:
    url = _migrated_database(tmp_path)
    with create_session_factory(url, create_schema=False)() as session:
        run = Run(run_key="daily:source-tiers", scheduled_for=AS_OF, config_hash="config")
        session.add(run)
        session.commit()
        for tier in get_args(SourceTier.__value__):
            item = persist_research_item(
                session,
                research_id=f"tier-{tier}",
                run_id=run.id,
                symbols=["AAPL"],
                source_tier=tier,
                source_type="MARKET_DATA",
                source_name="Tier contract fixture",
                provider="fixture",
                provider_item_id=f"tier-{tier}",
                research_question_id="q:tier-contract",
                research_question="Can this admitted tier be persisted?",
                raw_artifact_path=f"research/fixture/{tier}.json",
                normalized_summary=f"Fixture for {tier}",
                normalized_text=f"Fixture for {tier}",
                published_at=AS_OF,
                retrieved_at=AS_OF,
                content_hash=hashlib.sha256(tier.encode()).hexdigest(),
                headline=f"Fixture for {tier}",
            )
            assert item.source_tier == tier
