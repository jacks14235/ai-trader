import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from trader.research.config import ResearchConfig, load_research_config
from trader.research.models import (
    MAX_RAW_DOCUMENT_BYTES,
    ResearchBatch,
    ResearchDocument,
    ResearchPlan,
    ResearchRequest,
)
from trader.research.planner import ResearchPlanner
from trader.universe.models import (
    CandidateSignal,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)

AS_OF = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)


def config_dict() -> dict[str, object]:
    return {
        "enabled": True,
        "mode": "shadow",
        "admitted_providers": ["alpaca", "sec"],
        "selection": {
            "max_fast_candidates": 10,
            "max_deep_symbols": 8,
            "max_questions_per_symbol": 3,
        },
        "follow_up": {
            "enabled": True,
            "max_rounds": 1,
            "max_questions": 4,
            "max_new_deep_symbols": 2,
            "company_news_days": 30,
            "sec_filing_history_days": 365,
        },
        "collection": {
            "max_items_per_symbol": 10,
            "max_total_requests": 260,
            "max_total_items": 50,
            "max_response_bytes": 1_000_000,
            "max_total_response_bytes": 5_000_000,
            "max_wall_clock_seconds": 60,
            "max_retries_per_request": 2,
            "max_primary_filings_per_symbol": 2,
        },
        "freshness": {
            "market_context_hours": 24,
            "market_history_days": 20,
            "company_news_days": 7,
            "sec_filings_days": 90,
        },
        "paid": {
            "enabled": False,
            "max_per_request_usd": "0",
            "max_per_run_usd": "0",
        },
    }


def config() -> ResearchConfig:
    return ResearchConfig.model_validate(config_dict())


def asset(symbol: str) -> UniverseAsset:
    return UniverseAsset(
        symbol=symbol,
        name=f"{symbol} Inc.",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )


def candidate(
    symbol: str,
    score: int,
    *sources: str,
) -> ResearchCandidate:
    return ResearchCandidate(
        symbol=symbol,
        score=score,
        asset=asset(symbol),
        signals=tuple(CandidateSignal(source=source) for source in sources),
    )


def scan(*candidates: ResearchCandidate, extra_assets: tuple[str, ...] = ()) -> UniverseScan:
    assets = {item.symbol: item.asset for item in candidates}
    assets.update({symbol: asset(symbol) for symbol in extra_assets})
    return UniverseScan(
        as_of=AS_OF,
        asset_content_hash="a" * 64,
        eligible_assets=tuple(sorted(assets.values(), key=lambda item: item.symbol)),
        candidates=tuple(candidates),
        most_active_volume_updated_at=AS_OF,
        most_active_trades_updated_at=AS_OF,
        market_movers_updated_at=AS_OF,
        skipped_screener_symbols=0,
    )


def test_repository_research_policy_loads_with_bounded_shadow_defaults() -> None:
    policy = load_research_config(Path("config/research.yaml"))

    assert policy.enabled is True
    assert policy.mode == "shadow"
    assert set(policy.admitted_providers) == {"alpaca", "sec"}
    assert policy.selection.max_fast_candidates == 50
    assert policy.selection.max_deep_symbols == 10
    assert policy.collection.max_total_requests == 320
    assert policy.paid.max_per_run_usd == 0


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("enabled",), False),
        (("admitted_providers",), ["alpaca"]),
        (("admitted_providers",), ["alpaca", "sec", "web"]),
        (("selection", "max_fast_candidates"), 5.0),
        (("collection", "max_total_requests"), 12),
        (("paid", "max_per_run_usd"), "0.01"),
        (("paid", "max_per_request_usd"), "NaN"),
    ],
)
def test_research_policy_fails_closed(path: tuple[str, ...], value: object) -> None:
    content = config_dict()
    target = content
    for key in path[:-1]:
        target = cast(dict[str, object], target[key])
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        ResearchConfig.model_validate(content)


def test_research_policy_rejects_unknown_fields() -> None:
    content = config_dict()
    content["unbounded_web"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ResearchConfig.model_validate(content)


def test_planner_prioritizes_holdings_events_and_multi_signal_candidates() -> None:
    universe = scan(
        candidate("HIGH", 5_000, "TOP_GAINER"),
        candidate("MULTI", 500, "MOST_ACTIVE_VOLUME", "MOST_ACTIVE_TRADES"),
        candidate("EVENT", 10, "EXPLORATION"),
        candidate("HOLD", 5, "PORTFOLIO"),
        candidate("LOW", 1, "EXPLORATION"),
        candidate("DROP", 2_000, "TOP_GAINER"),
        candidate("X1", 100, "EXPLORATION"),
        candidate("X2", 90, "EXPLORATION"),
        candidate("X3", 80, "EXPLORATION"),
        candidate("X4", 70, "EXPLORATION"),
        candidate("X5", 60, "EXPLORATION"),
    )

    plan = ResearchPlanner(
        config(),
        sec_symbols=frozenset({"HOLD", "EVENT", "MULTI"}),
    ).plan(
        universe,
        portfolio_symbols=("HOLD",),
        event_symbols=("EVENT",),
    )

    assert plan.candidate_symbols == (
        "HOLD",
        "EVENT",
        "MULTI",
        "HIGH",
        "DROP",
        "X1",
        "X2",
        "X3",
        "X4",
        "X5",
    )
    assert plan.deep_symbols == (
        "HOLD",
        "EVENT",
        "MULTI",
        "HIGH",
        "DROP",
        "X1",
        "X2",
        "X3",
    )
    assert len(plan.questions) == 21
    assert sum(question.question_type == "MARKET_CONTEXT" for question in plan.questions) == 10
    assert sum(question.question_type == "COMPANY_NEWS" for question in plan.questions) == 8
    assert sum(question.question_type == "SEC_FILINGS" for question in plan.questions) == 3
    hold_questions = [question for question in plan.questions if question.symbol == "HOLD"]
    assert [question.priority for question in hold_questions] == [100, 99, 98]
    assert all(question.window_end == AS_OF for question in plan.questions)


def test_planner_adds_eligible_event_symbol_that_was_not_a_scan_candidate() -> None:
    universe = scan(
        candidate("AAA", 100, "MOST_ACTIVE_VOLUME"),
        candidate("BBB", 90, "MOST_ACTIVE_TRADES"),
        extra_assets=("EVENT",),
    )

    plan = ResearchPlanner(config()).plan(universe, event_symbols=("event",))

    assert plan.candidate_symbols[0] == "EVENT"
    assert plan.deep_symbols[0] == "EVENT"


def test_planner_is_deterministic_and_question_ids_cover_normalized_fields() -> None:
    universe = scan(
        candidate("AAA", 100, "MOST_ACTIVE_VOLUME"),
        candidate("BBB", 90, "MOST_ACTIVE_TRADES"),
    )
    planner = ResearchPlanner(config(), sec_symbols=frozenset({"AAA", "BBB"}))

    first = planner.plan(universe)
    second = planner.plan(universe)

    assert first == second
    assert first.questions[0].question_id == second.questions[0].question_id
    altered = first.questions[0].model_copy(update={"priority": 1})
    with pytest.raises(ValidationError, match="question_id"):
        ResearchRequest.model_validate(altered.model_dump())


def test_planner_rejects_duplicates_missing_symbols_and_future_scan_data() -> None:
    universe = scan(candidate("AAA", 100, "MOST_ACTIVE_VOLUME"))

    with pytest.raises(ValueError, match="duplicates"):
        ResearchPlanner(config()).plan(
            universe,
            portfolio_symbols=("AAA", "aaa"),
        )
    with pytest.raises(ValueError, match="absent"):
        ResearchPlanner(config()).plan(universe, event_symbols=("MISSING",))

    future_scan = universe.model_copy(
        update={"market_movers_updated_at": AS_OF + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="future"):
        ResearchPlanner(config()).plan(future_scan)


def test_planner_skips_sec_questions_for_symbols_without_a_cik_mapping() -> None:
    universe = scan(
        candidate("SPY", 100, "BENCHMARK"),
        candidate("AAPL", 90, "MOST_ACTIVE_TRADES"),
    )

    plan = ResearchPlanner(config(), sec_symbols=frozenset({"AAPL"})).plan(universe)

    spy_types = {
        question.question_type for question in plan.questions if question.symbol == "SPY"
    }
    aapl_types = {
        question.question_type for question in plan.questions if question.symbol == "AAPL"
    }
    assert spy_types == {"MARKET_CONTEXT", "COMPANY_NEWS"}
    assert aapl_types == {"MARKET_CONTEXT", "COMPANY_NEWS", "SEC_FILINGS"}


def question() -> ResearchRequest:
    return ResearchRequest.create(
        symbol="AAPL",
        question_type="COMPANY_NEWS",
        query="Material company news for AAPL",
        window_start=AS_OF - timedelta(days=7),
        window_end=AS_OF,
        priority=90,
    )


def document(
    *,
    raw_payload: bytes = b'{"headline":"Example"}',
    published_at: datetime | None = None,
    retrieved_at: datetime = AS_OF,
    provider_item_id: str = "item-1",
    cost_usd: Decimal = Decimal("0"),
) -> ResearchDocument:
    request = question()
    return ResearchDocument.create(
        question_ids=(request.question_id,),
        provider="alpaca",
        source_type="NEWS",
        source_tier="BROKER",
        source_name="alpaca-news",
        provider_item_id=provider_item_id,
        url="https://example.com/item-1",
        author=None,
        published_at=published_at or (AS_OF - timedelta(minutes=5)),
        retrieved_at=retrieved_at,
        symbols=("AAPL",),
        headline="Example headline",
        normalized_text="Example normalized body",
        summary=None,
        raw_payload=raw_payload,
        metadata={"provider_rank": 1},
        cost_usd=cost_usd,
    )


def test_research_request_rejects_naive_and_inconsistent_windows() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ResearchRequest.create(
            symbol="AAPL",
            question_type="MARKET_CONTEXT",
            query="market context",
            window_start=datetime(2026, 8, 20),
            window_end=AS_OF,
            priority=50,
        )
    with pytest.raises(ValidationError, match="precede"):
        ResearchRequest.create(
            symbol="AAPL",
            question_type="MARKET_CONTEXT",
            query="market context",
            window_start=AS_OF,
            window_end=AS_OF,
            priority=50,
        )


def test_research_document_validates_hash_source_time_cost_and_size() -> None:
    item = document()
    assert item.content_hash == hashlib.sha256(item.raw_payload).hexdigest()
    assert len(item.research_id) == 64

    with pytest.raises(ValidationError, match="published after retrieval"):
        document(published_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(ValidationError):
        document(cost_usd=Decimal("NaN"))
    with pytest.raises(ValidationError, match="too_long"):
        document(raw_payload=b"x" * (MAX_RAW_DOCUMENT_BYTES + 1))

    invalid_source = item.model_dump()
    invalid_source["provider"] = "sec"
    with pytest.raises(ValidationError, match="SEC documents"):
        ResearchDocument.model_validate(invalid_source)

    invalid_hash = item.model_dump()
    invalid_hash["content_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="content_hash"):
        ResearchDocument.model_validate(invalid_hash)


def test_research_batch_allows_empty_results_and_rejects_bad_counts() -> None:
    request = question()
    empty = ResearchBatch(
        provider="alpaca",
        retrieved_at=AS_OF,
        question_ids=(request.question_id,),
        documents=(),
        request_count=1,
        response_bytes=0,
        cost_usd=Decimal("0"),
    )
    assert empty.documents == ()

    item = document()
    with pytest.raises(ValidationError, match="duplicate documents"):
        ResearchBatch(
            provider="alpaca",
            retrieved_at=AS_OF,
            question_ids=(request.question_id,),
            documents=(item, item),
            request_count=1,
            response_bytes=2 * len(item.raw_payload),
            cost_usd=Decimal("0"),
        )
    with pytest.raises(ValidationError, match="smaller"):
        ResearchBatch(
            provider="alpaca",
            retrieved_at=AS_OF,
            question_ids=(request.question_id,),
            documents=(item,),
            request_count=1,
            response_bytes=len(item.raw_payload) - 1,
            cost_usd=Decimal("0"),
        )


def test_research_plan_rejects_future_questions_and_unknown_question_symbols() -> None:
    request = question()
    with pytest.raises(ValidationError, match="future"):
        ResearchPlan(
            as_of=AS_OF - timedelta(seconds=1),
            candidate_symbols=("AAPL",),
            deep_symbols=("AAPL",),
            questions=(request,),
        )
    with pytest.raises(ValidationError, match="candidate"):
        ResearchPlan(
            as_of=AS_OF,
            candidate_symbols=("MSFT",),
            deep_symbols=(),
            questions=(request,),
        )
