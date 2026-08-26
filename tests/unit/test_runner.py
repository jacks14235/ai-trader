import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.models import TradeProposal
from trader.agent.reasoning import DailyDecision, DailyUpdate
from trader.agent.runner import daily_run, paper_test_run_key, run_key
from trader.agent.runtime import DailyReasoningResult
from trader.broker.models import Account, BrokerFill, BrokerOrder, OrderQueryStatus, Position
from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    DailyReport,
    PerformanceSnapshot,
    Run,
    RunEvent,
    Thesis,
    TradeProposalRecord,
)
from trader.persistence.repositories import persist_trade_proposal
from trader.research.artifacts import ResearchArtifact
from trader.research.collection import ResearchCollection
from trader.research.models import ResearchPlan, ResearchRequest
from trader.research.service import ResearchRunResult
from trader.risk.models import RiskDecision
from trader.risk.runtime import DailyRiskExecutionResult
from trader.universe.models import (
    CandidateSignal,
    ResearchCandidate,
    UniverseAsset,
    UniverseScan,
)


class FakeBroker:
    def __init__(
        self,
        open_orders: list[BrokerOrder] | None = None,
        *,
        options_level: int = 0,
        provider_managed_options: bool = False,
    ) -> None:
        self.open_orders = open_orders or []
        self.options_level = options_level
        self.provider_managed_options = provider_managed_options

    def get_account(self) -> Account:
        return Account(
            equity="2000",
            cash="1000",
            buying_power="1000",
            options_level=self.options_level,
        )

    def get_positions(self) -> list[Position]:
        return [Position(symbol="SPY", qty="1", market_value="600", current_price="600")]

    def get_open_orders(self) -> list[BrokerOrder]:
        return self.open_orders

    @property
    def is_paper(self) -> bool:
        return True

    @property
    def paper_options_level_is_provider_managed(self) -> bool:
        return self.provider_managed_options

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]:
        del after, until, limit
        if status == "closed":
            return []
        return self.open_orders

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return next(
            (order for order in self.open_orders if order.client_order_id == client_order_id),
            None,
        )

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]:
        del after, until
        return []


class FakeCandidateScanner:
    def scan(
        self,
        *,
        as_of: datetime,
        portfolio_symbols: tuple[str, ...],
    ) -> UniverseScan:
        assert portfolio_symbols == ("SPY",)
        asset = UniverseAsset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class="us_equity",
            status="active",
            tradable=True,
            exchange="ARCA",
        )
        return UniverseScan(
            as_of=as_of,
            asset_content_hash="a" * 64,
            eligible_assets=(asset,),
            candidates=(
                ResearchCandidate(
                    symbol="SPY",
                    score=19000,
                    asset=asset,
                    signals=(
                        CandidateSignal(source="PORTFOLIO"),
                        CandidateSignal(source="BENCHMARK"),
                    ),
                ),
            ),
            most_active_volume_updated_at=as_of,
            most_active_trades_updated_at=as_of,
            market_movers_updated_at=as_of,
            skipped_screener_symbols=0,
        )


class FakeResearchPipeline:
    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        scan: UniverseScan,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchRunResult:
        del run_id
        assert portfolio_symbols == ("SPY",)
        assert event_symbols == ("SPY",)
        question = ResearchRequest.create(
            symbol="SPY",
            question_type="MARKET_CONTEXT",
            query="What changed in SPY market context?",
            window_start=scan.as_of - timedelta(hours=24),
            window_end=scan.as_of,
            priority=100,
        )
        research_directory = run_directory / "research" / "alpaca"
        research_directory.mkdir(parents=True)
        payload = b"{}"
        (research_directory / "document.json").write_bytes(payload)
        return ResearchRunResult(
            plan=ResearchPlan(
                as_of=scan.as_of,
                candidate_symbols=("SPY",),
                deep_symbols=("SPY",),
                questions=(question,),
            ),
            collection=ResearchCollection(batches=(), request_count=1, response_bytes=2),
            artifacts={
                "document": ResearchArtifact(
                    relative_path="alpaca/document.json",
                    content_hash=hashlib.sha256(payload).hexdigest(),
                    byte_count=2,
                    created=True,
                )
            },
            persisted_research_ids=("research-1",),
            elapsed_seconds=0.01,
        )


class FakeDailyReasoningPipeline:
    def __init__(self, session: Session | None = None) -> None:
        self.session = session
        self.proposal = TradeProposal(
            symbol="SPY",
            action="BUY",
            target_notional_usd=Decimal("25"),
            confidence=0.8,
            time_horizon="days",
            rationale="bounded runner test",
            invalidation_conditions=["Support fails"],
            evidence_ids=["a" * 64],
            max_acceptable_price=Decimal("650"),
        )

    def run(self, **kwargs: object) -> DailyReasoningResult:
        if self.session is not None:
            persist_trade_proposal(self.session, str(kwargs["run_id"]), self.proposal)
        return DailyReasoningResult(
            invocation_id="invocation",
            decision=DailyDecision(
                status="PROPOSE_TRADES",
                market_assessment="A bounded setup exists.",
                strongest_counterargument="It may reverse.",
                daily_update=DailyUpdate.model_validate(
                    {
                        "headline": "Buying a small slice of the market, slowly",
                        "lesson_title": (
                            "A share is a piece of a business, not a lottery ticket"
                        ),
                        "lesson": (
                            "SPY is a basket of large U.S. companies. Buying a little of it "
                            "is a way to own a diversified slice rather than one story."
                        ),
                        "overview": (
                            "The paper trader proposed a small SPY purchase on today's evidence."
                        ),
                        "next_day_plan": (
                            "Check whether the order was allowed and whether "
                            "the thesis still holds."
                        ),
                        "glossary": [
                            {
                                "term": "Equity",
                                "definition": "The current value of cash plus holdings.",
                            }
                        ],
                    }
                ),
                proposals=(self.proposal,),
            ),
            context_hash="b" * 64,
            prompt_hash="c" * 64,
            evidence_manifest_hash="d" * 64,
        )


class FakeRiskExecutionPipeline:
    def __init__(self) -> None:
        self.allow_execution: bool | None = None

    def run(self, **kwargs: object) -> DailyRiskExecutionResult:
        self.allow_execution = bool(kwargs["allow_execution"])
        proposal = cast(tuple[TradeProposal, ...], kwargs["proposals"])[0]
        return DailyRiskExecutionResult(
            decisions=(
                RiskDecision(
                    proposal_id=str(proposal.proposal_id),
                    approved=True,
                    normalized_order=None,
                    human_explanation="fake approval",
                ),
            ),
            submitted_orders=(),
            policy_hash="e" * 64,
            execution_enabled=self.allow_execution,
            reconciliation=None,
        )


def test_run_key_uses_eastern_trading_date() -> None:
    when = datetime(2026, 8, 21, 2, tzinfo=UTC)

    assert run_key(when) == "daily:2026-08-20:09:30:America/New_York"


def test_run_key_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run_key(datetime(2026, 8, 20))


def test_paper_test_run_key_is_explicit_and_rejects_naive_time() -> None:
    nonce = UUID("12345678-1234-5678-1234-567812345678")
    when = datetime(2026, 8, 21, 2, tzinfo=UTC)

    assert paper_test_run_key(when, nonce) == (
        "daily-test:2026-08-20:12345678-1234-5678-1234-567812345678"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        paper_test_run_key(datetime(2026, 8, 20), nonce)


def test_daily_run_persists_no_action_artifacts(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    config = b"mode: paper\n"
    when = datetime(2026, 8, 20, 19, 15, tzinfo=UTC)

    universe = b"allowed_symbols: [SPY]\nbenchmark_symbols: [SPY]\n"
    dynamic_runs = b"enabled: true\n"
    run_id = daily_run(
        session,
        FakeBroker(),
        tmp_path / "raw",
        config,
        when,
        universe_config_bytes=universe,
        dynamic_runs_config_bytes=dynamic_runs,
    )

    run = session.get(Run, run_id)
    assert run is not None
    assert run.status == "COMPLETED"
    assert run.config_hash == hashlib.sha256(
        config + b"\0" + universe + b"\0" + dynamic_runs
    ).hexdigest()

    directory = tmp_path / "raw" / "paper" / "runs" / run_id
    assert run.raw_artifact_path == str(directory.resolve())
    manifest = json.loads((directory / "manifest.json").read_text())
    assert set(manifest) == {
        "account_before.json",
        "daily_report.md",
        "daily_update.html",
        "dynamic_runs_config.yaml",
        "ledger_summary.json",
        "open_orders_before.json",
        "positions_before.json",
        "reconciliation_before.json",
        "risk_config.yaml",
        "universe_config.yaml",
    }
    assert "NO_ACTION" in (directory / "daily_report.md").read_text()
    html = (directory / "daily_update.html").read_text()
    assert "The paper account was checked, but no model wrote the story" in html
    assert "<svg" in html
    assert "Beginner-facing HTML" in (directory / "daily_report.md").read_text()
    assert json.loads((directory / "open_orders_before.json").read_text()) == []

    report_record = session.scalar(select(DailyReport).where(DailyReport.run_id == run_id))
    assert report_record is not None
    assert report_record.report_path == "daily_report.md"
    assert report_record.content_hash == hashlib.sha256(
        (directory / "daily_report.md").read_bytes()
    ).hexdigest()
    assert "NO_ACTION" in (report_record.summary or "")

    performance = session.scalar(
        select(PerformanceSnapshot).where(PerformanceSnapshot.run_id == run_id)
    )
    assert performance is not None
    assert performance.equity == "2000"
    assert performance.pnl is None
    assert performance.drawdown_pct == "0"

    stages = list(
        session.scalars(
            select(RunEvent.stage)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.occurred_at)
        )
    )
    assert stages[-1] == "WRITE_DAILY_REPORT"


def test_daily_run_refuses_duplicate_window(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    raw_root = tmp_path / "raw"
    when = datetime(2026, 8, 20, 19, 15, tzinfo=UTC)
    daily_run(session, FakeBroker(), raw_root, b"mode: paper\n", when)

    with pytest.raises(RuntimeError, match="already claimed"):
        daily_run(session, FakeBroker(), raw_root, b"mode: paper\n", when)


def test_daily_test_reruns_preserve_normal_window_and_are_unique(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    raw_root = tmp_path / "raw"
    when = datetime(2026, 8, 20, 19, 15, tzinfo=UTC)
    normal_id = daily_run(session, FakeBroker(), raw_root, b"mode: paper\n", when)

    first_test_id = daily_run(
        session,
        FakeBroker(),
        raw_root,
        b"mode: paper\n",
        when,
        test_rerun=True,
    )
    second_test_id = daily_run(
        session,
        FakeBroker(),
        raw_root,
        b"mode: paper\n",
        when,
        test_rerun=True,
    )

    runs = list(session.scalars(select(Run).order_by(Run.run_key)))
    assert {run.id for run in runs} == {normal_id, first_test_id, second_test_id}
    test_runs = [run for run in runs if run.run_key.startswith("daily-test:")]
    assert len(test_runs) == 2
    assert test_runs[0].run_key != test_runs[1].run_key
    assert all(run.status == "COMPLETED" for run in runs)
    assert session.scalar(
        select(RunEvent).where(
            RunEvent.run_id == first_test_id,
            RunEvent.stage == "TEST_RERUN",
        )
    ) is not None
    report = (
        raw_root / "paper" / "runs" / first_test_id / "daily_report.md"
    ).read_text()
    assert "Daily Test Rerun" in report


def test_daily_run_persists_candidate_universe_artifacts(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    when = datetime(2026, 8, 21, 19, 15, tzinfo=UTC)

    run_id = daily_run(
        session,
        FakeBroker(),
        tmp_path / "raw",
        b"mode: paper\n",
        when,
        candidate_scanner=FakeCandidateScanner(),
    )

    directory = tmp_path / "raw" / "paper" / "runs" / run_id
    assets = json.loads((directory / "eligible_assets.json").read_text())
    summary = json.loads((directory / "candidate_scan.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    assert assets[0]["symbol"] == "SPY"
    assert summary["eligible_asset_count"] == 1
    assert summary["candidate_count"] == 1
    assert summary["candidates"][0]["signals"][0]["source"] == "PORTFOLIO"
    assert "eligible_assets.json" in manifest
    assert "candidate_scan.json" in manifest
    assert "Candidate universe (shadow mode)" in (directory / "daily_report.md").read_text()

    scan_event = session.scalar(
        select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.stage == "SCAN_CANDIDATE_UNIVERSE",
        )
    )
    assert scan_event is not None
    assert json.loads(scan_event.metadata_json or "{}")["asset_content_hash"] == "a" * 64


def test_daily_run_persists_recursive_shadow_research_artifacts(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    when = datetime(2026, 8, 22, 19, 15, tzinfo=UTC)

    run_id = daily_run(
        session,
        FakeBroker(),
        tmp_path / "raw",
        b"mode: paper\n",
        when,
        research_config_bytes=b"enabled: true\nmode: shadow\n",
        candidate_scanner=FakeCandidateScanner(),
        research_pipeline=FakeResearchPipeline(),
        research_event_symbols=("SPY",),
    )

    directory = tmp_path / "raw" / "paper" / "runs" / run_id
    manifest = json.loads((directory / "manifest.json").read_text())
    summary = json.loads((directory / "research_summary.json").read_text())
    assert "research/alpaca/document.json" in manifest
    assert "research_config.yaml" in manifest
    assert summary["unique_document_count"] == 1
    assert summary["persisted_research_ids"] == ["research-1"]
    assert "Research collection (shadow mode)" in (directory / "daily_report.md").read_text()
    assert session.scalar(
        select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.stage == "COLLECT_SHADOW_RESEARCH",
        )
    ) is not None


def test_daily_run_fails_closed_on_open_order(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    order = BrokerOrder(
        id="order-1",
        client_order_id="client-1",
        symbol="SPY",
        side="buy",
        status="accepted",
        qty=Decimal("1"),
        limit_price=Decimal("500"),
    )

    with pytest.raises(RuntimeError, match="reconciliation reported issues"):
        daily_run(session, FakeBroker([order]), tmp_path / "raw", b"mode: paper\n")

    run = session.scalar(select(Run))
    assert run is not None
    assert run.status == "FAILED"
    assert "UNTRACKED_REMOTE_ORDER" in (run.error_summary or "")


def test_daily_run_allows_audited_provider_managed_paper_options_level(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    broker = FakeBroker(options_level=3, provider_managed_options=True)

    run_id = daily_run(session, broker, tmp_path / "raw", b"mode: paper\n")

    run = session.get(Run, run_id)
    assert run is not None
    assert run.status == "COMPLETED"
    configuration_event = session.scalar(
        select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.stage == "VERIFY_BROKER_CONFIGURATION",
        )
    )
    assert configuration_event is not None
    assert "provider-managed options level 3" in (configuration_event.detail or "")
    report = (
        tmp_path / "raw" / "paper" / "runs" / run_id / "daily_report.md"
    ).read_text()
    assert "Broker safety exception" in report


def test_daily_run_rejects_unmanaged_options_capability(tmp_path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    broker = FakeBroker(options_level=3, provider_managed_options=False)

    with pytest.raises(RuntimeError, match="less restrictive"):
        daily_run(session, broker, tmp_path / "raw", b"mode: paper\n")


def test_test_rerun_can_evaluate_but_never_enables_execution(tmp_path: Path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    risk_pipeline = FakeRiskExecutionPipeline()

    run_id = daily_run(
        session,
        FakeBroker(),
        tmp_path / "raw",
        b"mode: paper\n",
        datetime(2026, 8, 22, 14, tzinfo=UTC),
        candidate_scanner=FakeCandidateScanner(),
        research_pipeline=FakeResearchPipeline(),
        reasoning_pipeline=FakeDailyReasoningPipeline(),
        risk_execution_pipeline=risk_pipeline,
        research_event_symbols=("SPY",),
        test_rerun=True,
    )

    assert risk_pipeline.allow_execution is False
    assert session.scalar(
        select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.stage == "TEST_RERUN_EXECUTION_BLOCKED",
        )
    ) is not None
    report = (tmp_path / "raw" / "paper" / "runs" / run_id / "daily_report.md").read_text()
    assert "Paper orders submitted: 0" in report


def test_daily_run_opens_a_thesis_for_every_authorized_proposal(tmp_path: Path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.db")()
    reasoning_pipeline = FakeDailyReasoningPipeline(session)

    run_id = daily_run(
        session,
        FakeBroker(),
        tmp_path / "raw",
        b"mode: paper\n",
        datetime(2026, 8, 22, 14, tzinfo=UTC),
        candidate_scanner=FakeCandidateScanner(),
        research_pipeline=FakeResearchPipeline(),
        reasoning_pipeline=reasoning_pipeline,
        risk_execution_pipeline=FakeRiskExecutionPipeline(),
        research_event_symbols=("SPY",),
    )

    thesis = session.scalar(select(Thesis))
    assert thesis is not None
    assert thesis.symbol == "SPY"
    assert thesis.status == "active"
    assert thesis.confidence == pytest.approx(0.8)
    record = session.get(
        TradeProposalRecord,
        str(reasoning_pipeline.proposal.proposal_id),
    )
    assert record is not None
    assert record.thesis_id == thesis.id

    ledger_event = session.scalar(
        select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.stage == "RECORD_DECISION_LEDGER",
        )
    )
    assert ledger_event is not None
    metadata = json.loads(ledger_event.metadata_json or "{}")
    assert metadata["opened_theses"] == [thesis.id]
    # The cited ID is not a persisted research row in this run, so no link may be forged.
    assert metadata["linked_evidence_count"] == 0
    assert metadata["unlinked_evidence_count"] == 1

    directory = tmp_path / "raw" / "paper" / "runs" / run_id
    assert "Theses opened: 1" in (directory / "daily_report.md").read_text()
    assert json.loads((directory / "ledger_summary.json").read_text()) == metadata
    html = (directory / "daily_update.html").read_text()
    assert "Buying a small slice of the market, slowly" in html
    assert "Risk approved" in html
    assert "A share is a piece of a business" in html
    assert "&lt;script" not in html
