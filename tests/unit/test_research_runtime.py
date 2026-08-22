import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from trader.persistence.db import create_session_factory
from trader.persistence.models import Run
from trader.research.config import load_research_config
from trader.research.models import ProviderName, QuestionType, ResearchBatch, ResearchRequest
from trader.research.runtime import SecResolvedResearchPipeline, verify_research_schema
from trader.research.sec import SEC_COMPANY_TICKERS_URL, SecTickerMapSnapshot
from trader.universe.models import (
    CandidateSignal,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)

AS_OF = datetime(2026, 8, 21, 16, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).parents[2]


class StaticResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, *, retrieved_at: datetime | None = None) -> SecTickerMapSnapshot:
        self.calls += 1
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


class EmptyProvider:
    def __init__(
        self,
        provider_name: ProviderName,
        supported_question_types: frozenset[QuestionType],
    ) -> None:
        self.provider_name = provider_name
        self.supported_question_types = supported_question_types

    def estimated_request_count(self, request: ResearchRequest) -> int:
        if request.question_type == "MARKET_CONTEXT":
            return 2
        if request.question_type == "COMPANY_NEWS":
            return 1
        return 6

    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch:
        retrieved_at = retrieved_at or datetime.now(UTC)
        return ResearchBatch(
            provider=self.provider_name,
            retrieved_at=retrieved_at,
            question_ids=(request.question_id,),
            documents=(),
            request_count=(
                2 if request.question_type == "MARKET_CONTEXT" else 1
            ),
            response_bytes=0,
            cost_usd=Decimal("0"),
        )


def _scan() -> UniverseScan:
    aapl = UniverseAsset(
        symbol="AAPL",
        name="Apple Inc.",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )
    spy = UniverseAsset(
        symbol="SPY",
        name="SPDR S&P 500 ETF",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )
    return UniverseScan(
        as_of=AS_OF,
        asset_content_hash="a" * 64,
        eligible_assets=(aapl, spy),
        candidates=(
            ResearchCandidate(
                symbol="AAPL",
                score=100,
                asset=aapl,
                signals=(CandidateSignal(source="MOST_ACTIVE_TRADES"),),
            ),
            ResearchCandidate(
                symbol="SPY",
                score=90,
                asset=spy,
                signals=(CandidateSignal(source="BENCHMARK"),),
            ),
        ),
        most_active_volume_updated_at=AS_OF,
        most_active_trades_updated_at=AS_OF,
        market_movers_updated_at=AS_OF,
        skipped_screener_symbols=0,
    )


def test_runtime_preview_resolves_sec_map_and_omits_unmapped_sec_questions(
    tmp_path: Path,
) -> None:
    resolver = StaticResolver()
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    alpaca = EmptyProvider(
        "alpaca",
        frozenset({"MARKET_CONTEXT", "COMPANY_NEWS"}),
    )
    sec = EmptyProvider("sec", frozenset({"SEC_FILINGS"}))
    runtime = SecResolvedResearchPipeline(
        session,
        load_research_config(PROJECT_ROOT / "config/research.yaml"),
        alpaca,
        resolver,
        lambda _mapping: sec,
    )

    assert resolver.calls == 0
    preview = runtime.preview(_scan())

    assert resolver.calls == 1
    assert preview.estimated_total_requests == 13
    aapl_types = {
        question.question_type
        for question in preview.plan.questions
        if question.symbol == "AAPL"
    }
    spy_types = {
        question.question_type
        for question in preview.plan.questions
        if question.symbol == "SPY"
    }
    assert aapl_types == {"MARKET_CONTEXT", "COMPANY_NEWS", "SEC_FILINGS"}
    assert spy_types == {"MARKET_CONTEXT", "COMPANY_NEWS"}


def test_runtime_run_writes_both_sec_reference_artifacts(tmp_path: Path) -> None:
    resolver = StaticResolver()
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:runtime", scheduled_for=AS_OF, config_hash="config")
    session.add(run)
    session.commit()
    alpaca = EmptyProvider(
        "alpaca",
        frozenset({"MARKET_CONTEXT", "COMPANY_NEWS"}),
    )
    sec = EmptyProvider("sec", frozenset({"SEC_FILINGS"}))
    runtime = SecResolvedResearchPipeline(
        session,
        load_research_config(PROJECT_ROOT / "config/research.yaml"),
        alpaca,
        resolver,
        lambda _mapping: sec,
    )

    result = runtime.run(
        run_id=run.id,
        run_directory=tmp_path / "run",
        scan=_scan(),
    )

    assert resolver.calls == 1
    assert result.total_request_count == 8
    assert result.reference_artifacts is not None
    assert set(result.reference_artifacts) == {
        "research_collection.json",
        "sec/company_tickers.json",
        "sec/company_tickers.normalized.json",
    }
    assert (tmp_path / "run/research/sec/company_tickers.json").read_bytes().startswith(
        b'{"0"'
    )


def test_research_schema_preflight_rejects_an_unmigrated_database(tmp_path: Path) -> None:
    session = create_session_factory(
        f"sqlite:///{tmp_path}/legacy.sqlite",
        create_schema=False,
    )()

    with pytest.raises(RuntimeError, match="alembic.*upgrade head"):
        verify_research_schema(session)
