from datetime import UTC, date, datetime
from decimal import Decimal
from math import nan
from pathlib import Path
from uuid import uuid4

import pytest

from trader.agent.briefing import (
    DailyBriefingFacts,
    load_daily_update_template,
    render_daily_update,
)
from trader.agent.models import TradeProposal
from trader.agent.reasoning import DailyDecision, DailyUpdate, DailyUpdateChart
from trader.broker.models import Account, Position
from trader.ledger.models import LedgerWriteSummary, PerformanceMetrics
from trader.risk.models import RiskDecision

PROJECT_ROOT = Path(__file__).parents[2]


def test_template_ships_every_required_slot() -> None:
    template = load_daily_update_template(PROJECT_ROOT / "templates" / "daily_update.html")
    assert "Daily paper briefing" in template
    assert "__DAILY_UPDATE_HEADLINE__" in template


def test_daily_update_refuses_template_token_smuggling() -> None:
    with pytest.raises(ValueError, match="template token"):
        _update(headline="sneak __DAILY_UPDATE_TITLE__ into the page")


def test_daily_update_chart_values_must_be_finite_and_aligned() -> None:
    with pytest.raises(ValueError, match="finite"):
        DailyUpdateChart(
            title="Broken",
            kind="bar",
            caption="A chart that cannot be drawn.",
            labels=("A",),
            values=(nan,),
        )
    with pytest.raises(ValueError, match="same length"):
        DailyUpdateChart(
            title="Broken",
            kind="bar",
            caption="A chart that cannot be drawn.",
            labels=("A", "B"),
            values=(1.0,),
        )


def test_briefing_escapes_html_and_draws_svg_from_post_trade_facts(tmp_path: Path) -> None:
    proposal = TradeProposal(
        proposal_id=uuid4(),
        symbol="SPY",
        action="BUY",
        target_notional_usd="25",
        confidence=0.5,
        time_horizon="days",
        rationale="A small paper slice of the market.",
        evidence_ids=["a" * 64],
        max_acceptable_price="650",
    )
    decision = DailyDecision(
        status="PROPOSE_TRADES",
        market_assessment="Breadth looks acceptable.",
        strongest_counterargument="The bounce may fade.",
        daily_update=_update(
            headline="A tiny first step into the market",
            overview='<script>alert("xss")</script> and **bold** teaching.',
            sections=(
                {
                    "heading": "What is a limit order?",
                    "body": "It is a price ceiling, not a promise that you will buy.",
                },
            ),
            charts=(
                DailyUpdateChart(
                    title="How sure was the trader?",
                    kind="bar",
                    caption="Confidence is a number, not a feeling.",
                    labels=("SPY",),
                    values=(0.5,),
                ),
            ),
            glossary=({"term": "Cash", "definition": "Money not currently in a stock."},),
        ),
        proposals=(proposal,),
    )
    html = render_daily_update(
        load_daily_update_template(),
        DailyBriefingFacts(
            trading_date=date(2026, 8, 22),
            test_rerun=False,
            account=Account(equity="2000", cash="1400", buying_power="1400"),
            positions=(
                Position(symbol="SPY", qty="1", market_value="600", current_price="600"),
            ),
            performance=PerformanceMetrics(
                equity=Decimal("2000"),
                cash=Decimal("1400"),
                peak_equity=Decimal("2000"),
                drawdown_pct=Decimal("0"),
            ),
            equity_curve=(
                (datetime(2026, 8, 21, tzinfo=UTC), Decimal("1900")),
                (datetime(2026, 8, 22, tzinfo=UTC), Decimal("2000")),
            ),
            decision=decision,
            risk_decisions=(
                RiskDecision(
                    proposal_id=str(proposal.proposal_id),
                    approved=True,
                    normalized_order=None,
                    human_explanation="fake approval",
                ),
            ),
            submitted_orders=(),
            execution_enabled=False,
            ledger=LedgerWriteSummary(opened_thesis_ids=("thesis-1",)),
            abandoned_thesis_count=0,
        ),
    )

    assert "A tiny first step into the market" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>bold</strong>" in html
    assert "What is a limit order?" in html
    assert "Risk approved" in html
    assert "Paper orders were not submitted" in html
    assert "How sure was the trader?" in html
    assert "<svg" in html
    assert "Words worth knowing" in html
    assert "__DAILY_UPDATE_" not in html


def test_missing_template_token_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "daily_update.html"
    path.write_text("<html></html>\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing tokens"):
        load_daily_update_template(path)


def _update(**overrides: object) -> DailyUpdate:
    payload: dict[str, object] = {
        "headline": "Cash is still a position",
        "lesson_title": "Waiting is a decision",
        "lesson": "A portfolio that does not trade is still making a choice about risk.",
        "overview": "No idea cleared the evidence bar, so the paper account stays as it is.",
        "next_day_plan": "Look again at the same names only if the evidence changes.",
    }
    payload.update(overrides)
    return DailyUpdate.model_validate(payload)
