import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from trader.persistence.db import create_session_factory
from trader.persistence.models import Run
from trader.research.config import load_research_config
from trader.research.models import (
    ProviderName,
    QuestionType,
    ResearchBatch,
    ResearchDocument,
    ResearchRequest,
)
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
        if request.question_type in {"COMPANY_NEWS", "VALUATION_FACTS"}:
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


class PolicyMarketProvider(EmptyProvider):
    def __init__(self) -> None:
        super().__init__(
            "alpaca",
            frozenset({"MARKET_CONTEXT", "COMPANY_NEWS", "VALUATION_FACTS"}),
        )

    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch:
        retrieved = retrieved_at or AS_OF
        if request.question_type == "VALUATION_FACTS":
            return super().collect(request, retrieved_at=retrieved)
        if request.question_type == "COMPANY_NEWS":
            return ResearchBatch(
                provider="alpaca",
                retrieved_at=retrieved,
                question_ids=(request.question_id,),
                documents=(),
                request_count=1,
                response_bytes=0,
            )
        price = 200 if request.symbol == "AAPL" else 600
        snapshot = json.dumps(
            {request.symbol: {"latestTrade": {"p": price, "t": AS_OF.isoformat()}}},
            separators=(",", ":"),
        ).encode()
        bars = json.dumps(
            {
                request.symbol: [
                    {"c": price, "v": 100_000, "t": "2026-08-20T04:00:00Z"}
                ]
            },
            separators=(",", ":"),
        ).encode()
        documents = tuple(
            ResearchDocument.create(
                question_ids=(request.question_id,),
                provider="alpaca",
                source_type="MARKET_DATA",
                source_tier="BROKER",
                source_name=source_name,
                provider_item_id=f"{kind}:{request.symbol}",
                url=f"https://data.alpaca.markets/{kind}",
                author=None,
                published_at=retrieved,
                retrieved_at=retrieved,
                symbols=(request.symbol,),
                headline=f"{request.symbol} {kind}",
                normalized_text=payload.decode(),
                summary=None,
                raw_payload=payload,
            )
            for source_name, kind, payload in (
                ("Alpaca stock snapshot", "snapshot", snapshot),
                ("Alpaca adjusted daily bars", "bars", bars),
            )
        )
        return ResearchBatch(
            provider="alpaca",
            retrieved_at=retrieved,
            question_ids=(request.question_id,),
            documents=documents,
            request_count=2,
            response_bytes=sum(len(item.raw_payload) for item in documents),
        )


class StaticFollowUpPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **kwargs: object) -> object:
        self.calls += 1
        research = kwargs["research"]
        assert hasattr(research, "plan")
        question = ResearchRequest.create(
            symbol="AAPL",
            question_type="SEC_FILING_HISTORY",
            query="Resolve AAPL's prior operating baseline",
            window_start=AS_OF.replace(year=2025),
            window_end=AS_OF,
            priority=80,
        )

        class Result:
            invocation_id = "follow-up-invocation"
            questions = (question,)

        return Result()


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
        frozenset({"MARKET_CONTEXT", "COMPANY_NEWS", "VALUATION_FACTS"}),
    )
    sec = EmptyProvider("sec", frozenset({"SEC_FILINGS", "SEC_FILING_HISTORY"}))
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
    assert preview.estimated_total_requests == 24
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
    assert aapl_types == {
        "MARKET_CONTEXT",
        "COMPANY_NEWS",
        "SEC_FILINGS",
        "VALUATION_FACTS",
    }
    assert spy_types == {"MARKET_CONTEXT", "COMPANY_NEWS"}


def test_runtime_run_writes_both_sec_reference_artifacts(tmp_path: Path) -> None:
    resolver = StaticResolver()
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:runtime", scheduled_for=AS_OF, config_hash="config")
    session.add(run)
    session.commit()
    alpaca = EmptyProvider(
        "alpaca",
        frozenset({"MARKET_CONTEXT", "COMPANY_NEWS", "VALUATION_FACTS"}),
    )
    sec = EmptyProvider("sec", frozenset({"SEC_FILINGS", "SEC_FILING_HISTORY"}))
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
    assert result.total_request_count == 5
    assert result.reference_artifacts is not None
    assert set(result.reference_artifacts) == {
        "research_collection.json",
        "sec/company_tickers.json",
        "sec/company_tickers.normalized.json",
    }
    assert (tmp_path / "run/research/sec/company_tickers.json").read_bytes().startswith(
        b'{"0"'
    )


def test_runtime_screens_before_deep_collection_and_appends_one_follow_up_round(
    tmp_path: Path,
) -> None:
    resolver = StaticResolver()
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:staged", scheduled_for=AS_OF, config_hash="config")
    session.add(run)
    session.commit()
    sec = EmptyProvider("sec", frozenset({"SEC_FILINGS", "SEC_FILING_HISTORY"}))
    follow_up = StaticFollowUpPlanner()
    runtime = SecResolvedResearchPipeline(
        session,
        load_research_config(PROJECT_ROOT / "config/research.yaml"),
        PolicyMarketProvider(),
        resolver,
        lambda _mapping: sec,
        follow_up_planner_factory=lambda _symbols: follow_up,  # type: ignore[arg-type]
    )

    result = runtime.run(
        run_id=run.id,
        run_directory=tmp_path / "run",
        scan=_scan(),
    )

    assert result.plan.deep_symbols == ("AAPL", "SPY")
    assert {item.symbol for item in result.deep_selection if item.selected} == {
        "AAPL",
        "SPY",
    }
    assert any(
        question.question_type == "SEC_FILING_HISTORY"
        for question in result.plan.questions
    )
    assert result.follow_up_invocation_id == "follow-up-invocation"
    assert result.follow_up_requested_count == 1
    assert result.follow_up_granted_count == 1
    assert follow_up.calls == 1
    assert result.reference_artifacts is not None
    assert "research_follow_up_collection.json" in result.reference_artifacts


def test_research_schema_preflight_rejects_an_unmigrated_database(tmp_path: Path) -> None:
    session = create_session_factory(
        f"sqlite:///{tmp_path}/legacy.sqlite",
        create_schema=False,
    )()

    with pytest.raises(RuntimeError, match="alembic.*upgrade head"):
        verify_research_schema(session)
