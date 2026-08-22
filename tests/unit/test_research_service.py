import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    ResearchItem,
    ResearchItemQuestion,
    ResearchItemSymbol,
    Run,
)
from trader.research.collection import BoundedResearchCollector
from trader.research.config import load_research_config
from trader.research.models import (
    ProviderName,
    QuestionType,
    ResearchBatch,
    ResearchDocument,
    ResearchPlan,
    ResearchRequest,
)
from trader.research.service import ResearchPipelineError, ShadowResearchPipeline
from trader.universe.models import (
    CandidateSignal,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)

PROJECT_ROOT = Path(__file__).parents[2]
AS_OF = datetime(2026, 8, 21, 16, tzinfo=UTC)


class StaticPlanner:
    def __init__(self, questions: tuple[ResearchRequest, ...]) -> None:
        self.questions = questions

    def plan(
        self,
        scan: UniverseScan,
        *,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlan:
        assert portfolio_symbols == ("AAPL",)
        assert event_symbols == ("AAPL",)
        return ResearchPlan(
            as_of=scan.as_of,
            candidate_symbols=("AAPL",),
            deep_symbols=("AAPL",),
            questions=self.questions,
        )


class StaticProvider:
    provider_name: ProviderName = "alpaca"
    supported_question_types: frozenset[QuestionType] = frozenset({"COMPANY_NEWS"})

    def __init__(self, *, cost_usd: Decimal = Decimal("0")) -> None:
        self.cost_usd = cost_usd

    def estimated_request_count(self, request: ResearchRequest) -> int:
        return 1

    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch:
        retrieved_at = retrieved_at or datetime.now(UTC)
        raw = b'{"headline":"same article"}'
        document = ResearchDocument.create(
            question_ids=(request.question_id,),
            provider="alpaca",
            source_type="NEWS",
            source_tier="BROKER",
            source_name="Alpaca News",
            provider_item_id="article-1",
            url="https://example.test/article-1",
            author="Reporter",
            published_at=retrieved_at - timedelta(minutes=5),
            retrieved_at=retrieved_at,
            symbols=("AAPL",),
            headline="Same article",
            normalized_text="Complete normalized article text",
            summary="Article summary",
            raw_payload=raw,
            metadata={"source": "fixture"},
            cost_usd=self.cost_usd,
        )
        return ResearchBatch(
            provider="alpaca",
            retrieved_at=retrieved_at,
            question_ids=(request.question_id,),
            documents=(document,),
            request_count=1,
            response_bytes=len(raw),
            cost_usd=self.cost_usd,
        )


class CrossSymbolPlanner:
    def __init__(self, questions: tuple[ResearchRequest, ...]) -> None:
        self.questions = questions

    def plan(
        self,
        scan: UniverseScan,
        *,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlan:
        symbols = tuple(question.symbol for question in self.questions)
        return ResearchPlan(
            as_of=scan.as_of,
            candidate_symbols=symbols,
            deep_symbols=symbols,
            questions=self.questions,
        )


class CrossSymbolProvider(StaticProvider):
    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch:
        retrieved = retrieved_at or datetime.now(UTC)
        raw = b'{"news":[]}'
        document = ResearchDocument.create(
            question_ids=(request.question_id,),
            provider="alpaca",
            source_type="NEWS",
            source_tier="BROKER",
            source_name="Alpaca News",
            provider_item_id=f"news:{request.symbol}",
            url="https://example.test/news",
            author=None,
            published_at=retrieved,
            retrieved_at=retrieved,
            symbols=(request.symbol,),
            headline=f"{request.symbol} news (no results)",
            normalized_text="No matching news items.",
            summary=None,
            raw_payload=raw,
            metadata={"symbol": request.symbol},
            cost_usd=Decimal("0"),
        )
        return ResearchBatch(
            provider="alpaca",
            retrieved_at=retrieved,
            question_ids=(request.question_id,),
            documents=(document,),
            request_count=1,
            response_bytes=len(raw),
            cost_usd=Decimal("0"),
        )


def question(query: str, priority: int) -> ResearchRequest:
    return ResearchRequest.create(
        symbol="AAPL",
        question_type="COMPANY_NEWS",
        query=query,
        window_start=AS_OF - timedelta(days=7),
        window_end=AS_OF,
        priority=priority,
    )


def scan() -> UniverseScan:
    asset = UniverseAsset(
        symbol="AAPL",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )
    return UniverseScan(
        as_of=AS_OF,
        asset_content_hash="a" * 64,
        eligible_assets=(asset,),
        candidates=(
            ResearchCandidate(
                symbol="AAPL",
                score=10_000,
                asset=asset,
                signals=(CandidateSignal(source="PORTFOLIO"),),
            ),
        ),
        most_active_volume_updated_at=AS_OF,
        most_active_trades_updated_at=AS_OF,
        market_movers_updated_at=AS_OF,
        skipped_screener_symbols=0,
    )


def cross_symbol_scan() -> UniverseScan:
    assets = tuple(
        UniverseAsset(
            symbol=symbol,
            asset_class="us_equity",
            status="active",
            tradable=True,
        )
        for symbol in ("AAPL", "MSFT")
    )
    return UniverseScan(
        as_of=AS_OF,
        asset_content_hash="b" * 64,
        eligible_assets=assets,
        candidates=tuple(
            ResearchCandidate(
                symbol=asset.symbol,
                score=100,
                asset=asset,
                signals=(CandidateSignal(source="MOST_ACTIVE_TRADES"),),
            )
            for asset in assets
        ),
        most_active_volume_updated_at=AS_OF,
        most_active_trades_updated_at=AS_OF,
        market_movers_updated_at=AS_OF,
        skipped_screener_symbols=0,
    )
def test_shadow_pipeline_deduplicates_artifact_and_links_all_questions(tmp_path: Path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:research", scheduled_for=AS_OF, config_hash="config")
    session.add(run)
    session.commit()
    questions = (question("What changed?", 90), question("What contradicts the thesis?", 80))
    provider = StaticProvider()
    collector = BoundedResearchCollector(
        {"COMPANY_NEWS": provider},
        max_questions=2,
        max_http_requests=2,
        max_documents=10,
        max_response_bytes=1_000_000,
    )
    pipeline = ShadowResearchPipeline(
        session,
        StaticPlanner(questions),
        collector,
        load_research_config(PROJECT_ROOT / "config" / "research.yaml"),
        reference_payloads={"sec/company_tickers.json": b"map"},
        setup_request_count=1,
        setup_response_bytes=3,
    )

    result = pipeline.run(
        run_id=run.id,
        run_directory=tmp_path / "run",
        scan=scan(),
        portfolio_symbols=("AAPL",),
        event_symbols=("AAPL",),
    )

    assert result.unique_document_count == 1
    assert result.total_request_count == 3
    assert result.collection.request_count == 2
    assert len(result.persisted_research_ids) == 1
    artifact = next(iter(result.artifacts.values()))
    assert (tmp_path / "run" / "research" / artifact.relative_path).read_bytes() == (
        b'{"headline":"same article"}'
    )
    assert (
        tmp_path / "run" / "research" / "sec" / "company_tickers.json"
    ).read_bytes() == b"map"
    collection_manifest = json.loads(
        (tmp_path / "run/research/research_collection.json").read_text()
    )
    assert len(collection_manifest["batches"]) == 2
    assert all(batch["status"] == "COLLECTED" for batch in collection_manifest["batches"])
    assert all(
        batch["documents"][0]["raw_artifact_path"].startswith("research/alpaca/")
        for batch in collection_manifest["batches"]
    )
    item = session.scalar(select(ResearchItem))
    assert item is not None
    assert item.id == hashlib.sha256(f"{run.id}\0{item.content_hash}".encode()).hexdigest()
    links = list(session.scalars(select(ResearchItemQuestion)))
    assert {link.question_id for link in links} == {
        questions[0].question_id,
        questions[1].question_id,
    }


def test_shadow_pipeline_rejects_paid_evidence_before_writing(tmp_path: Path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:paid", scheduled_for=AS_OF, config_hash="config")
    session.add(run)
    session.commit()
    request = question("What changed?", 90)
    provider = StaticProvider(cost_usd=Decimal("0.01"))
    pipeline = ShadowResearchPipeline(
        session,
        StaticPlanner((request,)),
        BoundedResearchCollector(
            {"COMPANY_NEWS": provider},
            max_questions=1,
            max_http_requests=1,
            max_documents=10,
            max_response_bytes=1_000_000,
        ),
        load_research_config(PROJECT_ROOT / "config" / "research.yaml"),
    )

    with pytest.raises(ResearchPipelineError, match="paid-provider budget"):
        pipeline.run(
            run_id=run.id,
            run_directory=tmp_path / "run",
            scan=scan(),
            portfolio_symbols=("AAPL",),
            event_symbols=("AAPL",),
        )

    assert not (tmp_path / "run" / "research").exists()
    assert session.scalar(select(ResearchItem)) is None


def test_shadow_pipeline_coalesces_equal_content_across_symbol_observations(
    tmp_path: Path,
) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:cross-symbol", scheduled_for=AS_OF, config_hash="config")
    session.add(run)
    session.commit()
    questions = tuple(
        ResearchRequest.create(
            symbol=symbol,
            question_type="COMPANY_NEWS",
            query=f"Material company news for {symbol}",
            window_start=AS_OF - timedelta(days=7),
            window_end=AS_OF,
            priority=90,
        )
        for symbol in ("AAPL", "MSFT")
    )
    provider = CrossSymbolProvider()
    pipeline = ShadowResearchPipeline(
        session,
        CrossSymbolPlanner(questions),
        BoundedResearchCollector(
            {"COMPANY_NEWS": provider},
            max_questions=2,
            max_http_requests=2,
            max_documents=10,
            max_response_bytes=1_000_000,
        ),
        load_research_config(PROJECT_ROOT / "config/research.yaml"),
    )

    result = pipeline.run(
        run_id=run.id,
        run_directory=tmp_path / "run",
        scan=cross_symbol_scan(),
    )

    assert len(result.persisted_research_ids) == 1
    item = session.scalar(select(ResearchItem))
    assert item is not None
    assert set(session.scalars(select(ResearchItemSymbol.symbol))) == {"AAPL", "MSFT"}
    assert set(session.scalars(select(ResearchItemQuestion.question_id))) == {
        question.question_id for question in questions
    }
    assert item.metadata_json is not None
    assert len(json.loads(item.metadata_json)["source_observations"]) == 2
