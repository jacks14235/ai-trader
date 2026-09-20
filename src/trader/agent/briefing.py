"""Render the daily trader's briefing into the human-owned HTML template.

The model writes prose and optional chart numbers as part of ``DailyDecision``. This module
never invokes a model: it escapes that text, stamps post-trade facts, and draws SVG from
already-known numbers. A filled page is a teaching artifact, not an execution path.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from html import escape
from math import isfinite
from pathlib import Path

from trader.agent.models import TradeProposal
from trader.agent.reasoning import DailyDecision, DailyUpdate, DailyUpdateChart
from trader.broker.models import Account, BrokerOrder, Position
from trader.ledger.models import LedgerWriteSummary, PerformanceMetrics
from trader.risk.models import RiskDecision

TOKEN_PREFIX = "__DAILY_UPDATE_"
TEMPLATE_TOKENS: frozenset[str] = frozenset(
    {
        f"{TOKEN_PREFIX}TITLE__",
        f"{TOKEN_PREFIX}DATE__",
        f"{TOKEN_PREFIX}HEADLINE__",
        f"{TOKEN_PREFIX}STAMP__",
        f"{TOKEN_PREFIX}EQUITY__",
        f"{TOKEN_PREFIX}CASH__",
        f"{TOKEN_PREFIX}DRAWDOWN__",
        f"{TOKEN_PREFIX}STATUS__",
        f"{TOKEN_PREFIX}LESSON__",
        f"{TOKEN_PREFIX}OVERVIEW__",
        f"{TOKEN_PREFIX}NOTICED__",
        f"{TOKEN_PREFIX}TRADES__",
        f"{TOKEN_PREFIX}PORTFOLIO_CHARTS__",
        f"{TOKEN_PREFIX}SECTIONS__",
        f"{TOKEN_PREFIX}AGENT_CHARTS__",
        f"{TOKEN_PREFIX}NEXT_DAY__",
        f"{TOKEN_PREFIX}GLOSSARY__",
        f"{TOKEN_PREFIX}FOOTER__",
    }
)

DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3] / "templates" / "daily_update.html"
)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_CHART_PALETTE = ("#0f766e", "#b45309", "#9f1239", "#1d4ed8", "#854d0e", "#334155")


@dataclass(frozen=True)
class DailyBriefingFacts:
    """Everything the HTML renderer is allowed to know after the run's trading stages."""

    trading_date: date
    test_rerun: bool
    account: Account
    positions: tuple[Position, ...]
    performance: PerformanceMetrics
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    decision: DailyDecision | None
    risk_decisions: tuple[RiskDecision, ...]
    submitted_orders: tuple[BrokerOrder, ...]
    execution_enabled: bool | None
    ledger: LedgerWriteSummary
    abandoned_thesis_count: int


def default_daily_update_template_path() -> Path:
    return DEFAULT_TEMPLATE_PATH


def load_daily_update_template(path: Path | None = None) -> str:
    """Read the human-owned template and refuse a file that is missing required slots."""
    template_path = path or DEFAULT_TEMPLATE_PATH
    try:
        text = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read daily update template {template_path}: {exc}") from exc
    missing = sorted(token for token in TEMPLATE_TOKENS if token not in text)
    if missing:
        raise ValueError("daily update template is missing tokens: " + ", ".join(missing))
    return text


def render_daily_update(template: str, facts: DailyBriefingFacts) -> str:
    """Fill the template with escaped briefing prose and deterministic post-trade facts."""
    update = facts.decision.daily_update if facts.decision is not None else _fallback_update()
    filled = template
    replacements = {
        f"{TOKEN_PREFIX}TITLE__": escape(
            f"Daily paper briefing — {facts.trading_date.isoformat()}"
        ),
        f"{TOKEN_PREFIX}DATE__": escape(facts.trading_date.isoformat()),
        f"{TOKEN_PREFIX}HEADLINE__": escape(update.headline),
        f"{TOKEN_PREFIX}STAMP__": escape(_stamp(facts)),
        f"{TOKEN_PREFIX}EQUITY__": escape(_money(facts.account.equity)),
        f"{TOKEN_PREFIX}CASH__": escape(_money(facts.account.cash)),
        f"{TOKEN_PREFIX}DRAWDOWN__": escape(f"{facts.performance.drawdown_pct}%"),
        f"{TOKEN_PREFIX}STATUS__": escape(_status_label(facts)),
        f"{TOKEN_PREFIX}LESSON__": _lesson_block(update),
        f"{TOKEN_PREFIX}OVERVIEW__": _section("Today, in plain English", update.overview),
        f"{TOKEN_PREFIX}NOTICED__": _noticed_block(facts.decision),
        f"{TOKEN_PREFIX}TRADES__": _trades_block(facts),
        f"{TOKEN_PREFIX}PORTFOLIO_CHARTS__": _portfolio_charts(facts),
        f"{TOKEN_PREFIX}SECTIONS__": _extra_sections(update),
        f"{TOKEN_PREFIX}AGENT_CHARTS__": _agent_charts(update),
        f"{TOKEN_PREFIX}NEXT_DAY__": _section("What to watch next", update.next_day_plan),
        f"{TOKEN_PREFIX}GLOSSARY__": _glossary_block(update),
        f"{TOKEN_PREFIX}FOOTER__": _prose(
            "This briefing describes a paper-trading run. It is not investment advice, "
            "and nothing on this page can place an order. A separate risk engine decides "
            "whether a proposal is allowed; the model only explains the decision it made."
        ),
    }
    for token, value in replacements.items():
        filled = filled.replace(token, value)
    leftover = sorted(token for token in TEMPLATE_TOKENS if token in filled)
    if leftover:
        raise RuntimeError("daily update template tokens were not replaced: " + ", ".join(leftover))
    if TOKEN_PREFIX in filled:
        raise RuntimeError("rendered daily update still contains a template token marker")
    return filled


def _fallback_update() -> DailyUpdate:
    return DailyUpdate(
        headline="The paper account was checked, but no model wrote the story",
        lesson_title="A trading system can still be useful when it sits still",
        lesson=(
            "Today the daily trader was not invoked, so this page is a factual snapshot "
            "rather than a teaching narrative from the model. That is itself a design "
            "choice: the software will still record the account, refuse unsafe broker "
            "settings, and keep an audit trail even when a model is silent."
        ),
        overview=(
            "No proposals were written. The figures and charts below come from the "
            "broker snapshot and the decision ledger, not from a model."
        ),
        next_day_plan=(
            "If reasoning is enabled for a later run, the daily trader will fill this "
            "same template with a beginner-facing explanation of whatever it decides."
        ),
    )


def _stamp(facts: DailyBriefingFacts) -> str:
    if facts.test_rerun:
        return "Paper · test rerun · not advice"
    return "Paper account · not advice"


def _status_label(facts: DailyBriefingFacts) -> str:
    if facts.decision is None:
        return "No model"
    if facts.decision.status == "NO_ACTION":
        return "No trade"
    return "Proposals"


def _lesson_block(update: DailyUpdate) -> str:
    return (
        '<section class="lesson">'
        f"<h2>{escape(update.lesson_title)}</h2>"
        f"{_prose(update.lesson)}"
        "</section>"
    )


def _noticed_block(decision: DailyDecision | None) -> str:
    if decision is None:
        return ""
    counter = _section("The strongest case against today", decision.strongest_counterargument)
    noticed = _section("What the trader noticed", decision.market_assessment)
    dissent = ""
    if decision.dissent_dispositions:
        lines = [
            f"- `{item.packet_step}/{item.claim_id}` — **{item.resolution.lower()}**: "
            f"{item.rationale}"
            for item in decision.dissent_dispositions
        ]
        dissent = _section("How the manager addressed dissent", "\n".join(lines))
    return noticed + counter + dissent


def _trades_block(facts: DailyBriefingFacts) -> str:
    if facts.decision is None:
        return _section(
            "What was traded",
            "Nothing was proposed, so nothing was sent to the risk engine.",
        )
    if facts.decision.status == "NO_ACTION":
        abstention = facts.decision.abstention
        assert abstention is not None
        revisit = (
            abstention.reconsider_at.isoformat()
            if abstention.reconsider_at is not None
            else abstention.reconsider_on
        )
        triggers = "\n".join(
            f"- **{trigger.kind.lower()}**: {trigger.description}"
            for trigger in abstention.triggers
        )
        unavailable = (
            "\n\n**Unavailable data:** " + "; ".join(abstention.unavailable_data)
            if abstention.unavailable_data
            else ""
        )
        return _section(
            "What was traded",
            "The daily trader chose not to propose a trade. That is a complete decision, "
            "not a missing one.\n\n"
            f"**Why it sat still:** {abstention.insufficient_evidence}\n\n"
            f"**Classification:** {abstention.classification.lower().replace('_', ' ')}"
            f"{unavailable}\n\n"
            f"**What would change the decision:**\n{triggers}\n\n"
            f"**Reconsider:** {revisit}",
        )
    risk_by_id = {item.proposal_id: item for item in facts.risk_decisions}
    cards = [
        _trade_card(proposal, risk_by_id.get(str(proposal.proposal_id)), facts)
        for proposal in facts.decision.proposals
    ]
    submitted = ""
    if facts.submitted_orders:
        lines = [
            f"- {order.side.upper()} {order.symbol}: {order.qty} shares"
            + (f" at {order.limit_price}" if order.limit_price is not None else "")
            for order in facts.submitted_orders
        ]
        submitted = _section("Paper orders the software submitted", "\n".join(lines))
    elif facts.execution_enabled is False:
        submitted = (
            '<p class="note">Paper submission was not enabled for this run, so an approved '
            "proposal stayed on paper in the audit trail only.</p>"
        )
    return "<h2>What was proposed — and what happened next</h2>" + "".join(cards) + submitted


def _trade_card(
    proposal: TradeProposal,
    risk: RiskDecision | None,
    facts: DailyBriefingFacts,
) -> str:
    action = proposal.action.lower()
    target = _target_text(proposal)
    if risk is None:
        outcome_class = "wait"
        outcome_label = "Not evaluated"
        outcome_detail = (
            "The risk engine did not run, so this remains a proposal rather than a trade."
        )
    elif risk.approved:
        outcome_class = "ok"
        outcome_label = "Risk approved"
        outcome_detail = risk.human_explanation
        if facts.execution_enabled is False:
            outcome_detail += " Paper orders were not submitted."
    else:
        outcome_class = "no"
        outcome_label = "Risk rejected"
        codes = ", ".join(risk.rejection_codes) or "no code"
        outcome_detail = f"{risk.human_explanation} ({codes})"
    return (
        f'<article class="trade">'
        f'<div class="trade-top">'
        f'<span class="badge {escape(action, quote=True)}">{escape(proposal.action)}</span>'
        f"<strong>{escape(proposal.symbol)}</strong>"
        f'<span class="badge {outcome_class}">{escape(outcome_label)}</span>'
        f"</div>"
        f"<p>{escape(target)}</p>"
        f"{_prose(proposal.rationale)}"
        f'<p class="note">{escape(outcome_detail)}</p>'
        "</article>"
    )


def _target_text(proposal: TradeProposal) -> str:
    if proposal.target_notional_usd is not None:
        return f"Requested size: {_money(proposal.target_notional_usd)} of the account."
    if proposal.target_position_pct is not None:
        return (
            "Requested size: "
            f"{proposal.target_position_pct}% of equity as the ending position."
        )
    return "Requested size was not specified."


def _portfolio_charts(facts: DailyBriefingFacts) -> str:
    allocation = _allocation_slices(facts.account, facts.positions)
    figures = [
        _figure(
            "How the account is split today",
            _svg_pie(allocation),
            "Cash is money not in a stock. Everything else is a position's current market value.",
        )
    ]
    if len(facts.equity_curve) >= 1:
        figures.append(
            _figure(
                "Paper equity over recent runs",
                _svg_line(
                    [point[0].date().isoformat() for point in facts.equity_curve],
                    [float(point[1]) for point in facts.equity_curve],
                ),
                "Equity is cash plus the value of holdings. A flat line can be a good day.",
            )
        )
    if facts.decision is not None and facts.decision.proposals:
        approved = sum(item.approved for item in facts.risk_decisions)
        rejected = sum(not item.approved for item in facts.risk_decisions)
        pending = len(facts.decision.proposals) - len(facts.risk_decisions)
        figures.append(
            _figure(
                "What the risk engine did with today's ideas",
                _svg_bar(
                    ("Approved", "Rejected", "Not evaluated"),
                    (float(approved), float(rejected), float(pending)),
                ),
                "Approval is a software decision. It is not the same thing as a fill.",
            )
        )
    ledger_caption = (
        f"Theses opened {len(facts.ledger.opened_thesis_ids)}, "
        f"updated {len(facts.ledger.updated_thesis_ids)}, "
        f"closed on exit {len(facts.ledger.closed_thesis_ids)}, "
        f"closed with no position {facts.abandoned_thesis_count}."
    )
    return (
        "<h2>A picture of the paper portfolio</h2>"
        f'<p class="note">{escape(ledger_caption)}</p>'
        f'<div class="charts">{"".join(figures)}</div>'
    )


def _allocation_slices(
    account: Account, positions: tuple[Position, ...]
) -> tuple[tuple[str, float], ...]:
    slices = [("Cash", float(account.cash))]
    slices.extend((position.symbol, float(position.market_value)) for position in positions)
    positive = tuple((label, value) for label, value in slices if isfinite(value) and value > 0)
    if positive:
        return positive
    if isfinite(float(account.equity)) and account.equity > 0:
        return (("Equity", float(account.equity)),)
    return (("Empty", 1.0),)


def _extra_sections(update: DailyUpdate) -> str:
    if not update.sections:
        return ""
    blocks = [_section(item.heading, item.body) for item in update.sections]
    return "".join(blocks)


def _agent_charts(update: DailyUpdate) -> str:
    if not update.charts:
        return ""
    figures = [
        _figure(chart.title, _chart_svg(chart), chart.caption) for chart in update.charts
    ]
    return (
        "<h2>Charts the trader drew from today's record</h2>"
        f'<div class="charts">{"".join(figures)}</div>'
    )


def _glossary_block(update: DailyUpdate) -> str:
    if not update.glossary:
        return ""
    items = "".join(
        f"<div><dt>{escape(entry.term)}</dt><dd>{escape(entry.definition)}</dd></div>"
        for entry in update.glossary
    )
    return f"<h2>Words worth knowing</h2><dl class=\"glossary\">{items}</dl>"


def _section(heading: str, body: str) -> str:
    return f"<section><h2>{escape(heading)}</h2>{_prose(body)}</section>"


def _figure(title: str, svg: str, caption: str) -> str:
    return (
        f'<figure class="chart"><h3>{escape(title)}</h3>{svg}'
        f"<figcaption>{escape(caption)}</figcaption></figure>"
    )


def _chart_svg(chart: DailyUpdateChart) -> str:
    labels = list(chart.labels)
    values = [float(value) for value in chart.values]
    if chart.kind == "line":
        return _svg_line(labels, values)
    return _svg_bar(tuple(labels), tuple(values))


def _svg_bar(labels: tuple[str, ...], values: tuple[float, ...]) -> str:
    width, height, pad_left, pad_bottom = 640, 260, 48, 48
    pad_top, pad_right = 16, 16
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom
    peak = max(values) if values and max(values) > 0 else 1.0
    bar_w = chart_w / max(len(values), 1)
    bars: list[str] = []
    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        bar_h = 0.0 if value <= 0 or not isfinite(value) else chart_h * (value / peak)
        x = pad_left + index * bar_w + bar_w * 0.15
        y = pad_top + chart_h - bar_h
        w = bar_w * 0.7
        color = _CHART_PALETTE[index % len(_CHART_PALETTE)]
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{bar_h:.1f}" '
            f'fill="{color}"></rect>'
            f'<text x="{x + w / 2:.1f}" y="{height - 14}" text-anchor="middle" '
            f'font-size="11" fill="#6b6258">{escape(_short_label(label))}</text>'
            f'<text x="{x + w / 2:.1f}" y="{max(pad_top + 12, y - 6):.1f}" '
            f'text-anchor="middle" font-size="11" fill="#1f1b16">{escape(_n(value))}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape("Bar chart", quote=True)}">'
        f'<rect width="{width}" height="{height}" fill="#fffaf2"></rect>'
        + "".join(bars)
        + "</svg>"
    )


def _svg_line(labels: list[str], values: list[float]) -> str:
    width, height, pad = 640, 260, 40
    chart_w = width - pad * 2
    chart_h = height - pad * 2
    finite = [value for value in values if isfinite(value)]
    low = min(finite) if finite else 0.0
    high = max(finite) if finite else 1.0
    if high == low:
        high = low + 1.0
    count = max(len(values), 1)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = pad + (chart_w * index / max(count - 1, 1))
        y = pad + chart_h - ((value - low) / (high - low) * chart_h)
        points.append((x, y))
    if not points:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{escape("Line chart", quote=True)}">'
            f'<rect width="{width}" height="{height}" fill="#fffaf2"></rect>'
            "</svg>"
        )
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = (
        f"{pad:.1f},{pad + chart_h:.1f} "
        + polyline
        + f" {points[-1][0]:.1f},{pad + chart_h:.1f}"
    )
    start_label = escape(_short_label(labels[0])) if labels else ""
    end_label = escape(_short_label(labels[-1])) if labels else ""
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape("Line chart", quote=True)}">'
        f'<rect width="{width}" height="{height}" fill="#fffaf2"></rect>'
        f'<polygon points="{area}" fill="#0f766e22"></polygon>'
        f'<polyline points="{polyline}" fill="none" stroke="#0f766e" stroke-width="3"></polyline>'
        f'<text x="{pad}" y="{height - 12}" font-size="11" fill="#6b6258">{start_label}</text>'
        f'<text x="{width - pad}" y="{height - 12}" text-anchor="end" font-size="11" '
        f'fill="#6b6258">{end_label}</text>'
        "</svg>"
    )


def _svg_pie(slices: tuple[tuple[str, float], ...]) -> str:
    width, height = 640, 280
    cx, cy, radius = 150, 140, 96
    total = sum(value for _, value in slices) or 1.0
    angle = 0.0
    paths: list[str] = []
    legend: list[str] = []
    for index, (label, value) in enumerate(slices):
        sweep = 360.0 * (value / total)
        color = _CHART_PALETTE[index % len(_CHART_PALETTE)]
        paths.append(_slice_path(cx, cy, radius, angle, sweep, color))
        angle += sweep
        legend_y = 48 + index * 28
        legend.append(
            f'<rect x="290" y="{legend_y}" width="14" height="14" fill="{color}"></rect>'
            f'<text x="314" y="{legend_y + 12}" font-size="14" fill="#1f1b16">'
            f"{escape(label)} · {escape(_n(value))}</text>"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape("Allocation chart", quote=True)}">'
        f'<rect width="{width}" height="{height}" fill="#fffaf2"></rect>'
        + "".join(paths)
        + "".join(legend)
        + "</svg>"
    )


def _slice_path(cx: float, cy: float, radius: float, start: float, sweep: float, color: str) -> str:
    if sweep >= 359.999:
        return (
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{color}"></circle>'
        )
    end = start + sweep
    large = 1 if sweep > 180 else 0
    x1, y1 = _polar(cx, cy, radius, start)
    x2, y2 = _polar(cx, cy, radius, end)
    return (
        f'<path d="M {cx:.1f},{cy:.1f} L {x1:.1f},{y1:.1f} '
        f'A {radius:.1f},{radius:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{color}"></path>'
    )


def _polar(cx: float, cy: float, radius: float, angle_deg: float) -> tuple[float, float]:
    radians = math.radians(angle_deg - 90)
    return cx + radius * math.cos(radians), cy + radius * math.sin(radians)


def _prose(value: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", value.strip()) if part.strip()]
    rendered: list[str] = []
    for paragraph in paragraphs:
        escaped = escape(paragraph)
        escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
        escaped = _CODE.sub(r"<code>\1</code>", escaped)
        escaped = escaped.replace("\n", "<br>\n")
        rendered.append(f"<p>{escaped}</p>")
    return "".join(rendered) or "<p></p>"


def _money(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    return f"${quantized:,.2f}"


def _n(value: float) -> str:
    if abs(value) >= 100 or value == int(value):
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _short_label(value: str) -> str:
    stripped = value.strip()
    if len(stripped) <= 14:
        return stripped
    return stripped[:13] + "…"
