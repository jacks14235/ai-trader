import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from trader.agent.codex_cli import InvocationResponse
from trader.agent.config import load_agent_config
from trader.agent.research_planner import AgentFollowUpResearchPlanner
from trader.persistence.db import create_session_factory
from trader.persistence.models import AgentInvocation, Run
from trader.persistence.repositories import persist_research_item
from trader.research.collection import ResearchCollection
from trader.research.config import load_research_config
from trader.research.models import ResearchPlan, ResearchRequest
from trader.research.selection import DeepSelectionAssessment
from trader.research.service import ResearchRunResult
from trader.universe.models import (
    CandidateSignal,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)

PROJECT_ROOT = Path(__file__).parents[2]
AS_OF = datetime(2026, 9, 24, 19, 15, tzinfo=UTC)


class StaticProvider:
    provider_name = "test"

    def invoke(self, **kwargs: object) -> InvocationResponse:
        assert "SEC_FILING_HISTORY" in str(kwargs["prompt"])
        response = {
            "requests": [
                {
                    "symbol": "AAPL",
                    "question_type": "SEC_FILING_HISTORY",
                    "gap": "Prior segment margins and management guidance are missing.",
                    "decision_relevance": (
                        "The baseline would show whether the reported improvement is unusual."
                    ),
                }
            ],
            "no_follow_up_reason": None,
        }
        return InvocationResponse(json.dumps(response), "stdout", "")


def test_follow_up_planner_persists_audited_invocation_and_builds_bounded_request(
    tmp_path: Path,
) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(run_key="daily:follow-up", scheduled_for=AS_OF, config_hash="config")
    session.add(run)
    session.commit()
    content = "AAPL filed a quarterly report with current operating results."
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    research_id = hashlib.sha256(f"{run.id}\0{content_hash}".encode()).hexdigest()
    initial_question = ResearchRequest.create(
        symbol="AAPL",
        question_type="SEC_FILINGS",
        query="Recent SEC filings and material disclosures for AAPL",
        window_start=AS_OF - timedelta(days=90),
        window_end=AS_OF,
        priority=80,
    )
    persist_research_item(
        session,
        research_id=research_id,
        run_id=run.id,
        symbols=("AAPL",),
        source_tier="PRIMARY",
        source_type="REGULATORY_FILING",
        source_name="SEC EDGAR primary filing document",
        provider="sec",
        provider_item_id="filing:aapl",
        research_question_id=initial_question.question_id,
        research_question=initial_question.query,
        raw_artifact_path="research/sec/aapl.html",
        normalized_summary="AAPL quarterly filing",
        normalized_text=content,
        published_at=AS_OF - timedelta(days=1),
        retrieved_at=AS_OF,
        content_hash=content_hash,
        headline="AAPL 10-Q",
    )
    asset = UniverseAsset(
        symbol="AAPL",
        name="Apple Inc.",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )
    scan = UniverseScan(
        as_of=AS_OF,
        asset_content_hash="a" * 64,
        eligible_assets=(asset,),
        candidates=(
            ResearchCandidate(
                symbol="AAPL",
                score=100,
                asset=asset,
                signals=(CandidateSignal(source="MOST_ACTIVE_TRADES"),),
            ),
        ),
        most_active_volume_updated_at=AS_OF,
        most_active_trades_updated_at=AS_OF,
        market_movers_updated_at=AS_OF,
        skipped_screener_symbols=0,
    )
    research = ResearchRunResult(
        plan=ResearchPlan(
            as_of=AS_OF,
            candidate_symbols=("AAPL",),
            deep_symbols=("AAPL",),
            questions=(initial_question,),
        ),
        collection=ResearchCollection(batches=(), request_count=1, response_bytes=1),
        artifacts={},
        persisted_research_ids=(research_id,),
        elapsed_seconds=0.1,
        deep_selection=(
            DeepSelectionAssessment(
                symbol="AAPL",
                selected=True,
                pinned=False,
                price="200",
                average_daily_dollar_volume="100000000",
                reason_codes=("POLICY_SCREEN_PASSED",),
            ),
        ),
    )
    agents = load_agent_config(PROJECT_ROOT / "config/agents.yaml")
    research_config = load_research_config(PROJECT_ROOT / "config/research.yaml")
    planner = AgentFollowUpResearchPlanner(
        session,
        agents,
        research_config,
        prompt=(PROJECT_ROOT / "prompts/research_planner.md").read_text(),
        provider=StaticProvider(),
        sec_symbols=frozenset({"AAPL"}),
    )

    result = planner.run(
        run_id=run.id,
        run_directory=tmp_path / "run",
        scan=scan,
        research=research,
    )

    assert len(result.questions) == 1
    question = result.questions[0]
    assert question.question_type == "SEC_FILING_HISTORY"
    assert question.window_start == AS_OF - timedelta(days=365)
    assert question.window_end == AS_OF - timedelta(days=90)
    invocation = session.scalar(select(AgentInvocation))
    assert invocation is not None
    assert invocation.role == "research_planner"
    assert invocation.status == "COMPLETED"
    assert invocation.evidence_manifest_hash is not None
