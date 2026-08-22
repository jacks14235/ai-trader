"""Audit-oriented relational schema for paper-trading runs."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    """Return an aware UTC timestamp for persisted audit events."""
    return datetime.now(UTC)


def uid() -> str:
    """Return a string UUID suitable for SQLite and PostgreSQL."""
    return str(uuid4())


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (CheckConstraint("mode = 'paper'", name="paper_mode_only"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_key: Mapped[str] = mapped_column(String, unique=True)
    mode: Mapped[str] = mapped_column(String, default="paper")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, default="STARTED", index=True)
    git_commit_sha: Mapped[str | None] = mapped_column(String)
    config_hash: Mapped[str] = mapped_column(String)
    agent_model: Mapped[str | None] = mapped_column(String)
    prompt_version: Mapped[str | None] = mapped_column(String)
    raw_artifact_path: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_occurred", "run_id", "occurred_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    detail: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)


class MarketEvent(Base):
    """A source-backed market event that may justify a future agent run."""

    __tablename__ = "market_events"
    __table_args__ = (
        UniqueConstraint("source", "source_event_id"),
        CheckConstraint("status IN ('ACTIVE', 'CANCELLED')", name="valid_status"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="valid_confidence"),
        Index("ix_market_events_type_scheduled", "event_type", "scheduled_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    event_key: Mapped[str] = mapped_column(String, unique=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    symbols_json: Mapped[str] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String)
    source_event_id: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", index=True)
    created_by_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"),
        index=True,
    )
    announced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScheduledRun(Base):
    """A durable, policy-approved request to start a future event run."""

    __tablename__ = "scheduled_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'RUNNING', 'COMPLETED', "
            "'FAILED', 'CANCELLED', 'EXPIRED')",
            name="valid_status",
        ),
        Index("ix_scheduled_runs_status_due", "status", "scheduled_for"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    schedule_key: Mapped[str] = mapped_column(String, unique=True)
    market_event_id: Mapped[str] = mapped_column(
        ForeignKey("market_events.id", ondelete="CASCADE"),
        index=True,
    )
    created_by_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"),
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"),
        unique=True,
    )
    event_type: Mapped[str] = mapped_column(String, index=True)
    symbols_json: Mapped[str] = mapped_column(Text)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="PENDING", index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String, unique=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScheduledRunEvent(Base):
    """Append-only lifecycle history for a scheduled run."""

    __tablename__ = "scheduled_run_events"
    __table_args__ = (
        Index("ix_scheduled_run_events_run_occurred", "scheduled_run_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    scheduled_run_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_runs.id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    detail: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_portfolio_snapshots_run_captured", "run_id", "captured_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    snapshot_type: Mapped[str] = mapped_column(String, default="CURRENT")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    equity: Mapped[str] = mapped_column(String)
    cash: Mapped[str] = mapped_column(String)
    buying_power: Mapped[str] = mapped_column(String)
    raw_json: Mapped[str] = mapped_column(Text)


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_snapshot_id", "symbol"),
        Index("ix_position_snapshots_symbol_snapshot", "symbol", "portfolio_snapshot_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    portfolio_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"),
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String, index=True)
    qty: Mapped[str] = mapped_column(String)
    market_value: Mapped[str] = mapped_column(String)
    current_price: Mapped[str] = mapped_column(String)
    average_entry_price: Mapped[str | None] = mapped_column(String)
    unrealized_pl: Mapped[str | None] = mapped_column(String)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "symbol", "quote_at"),
        Index("ix_market_snapshots_symbol_quote", "symbol", "quote_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    quote_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    bid: Mapped[str | None] = mapped_column(String)
    ask: Mapped[str | None] = mapped_column(String)
    last: Mapped[str | None] = mapped_column(String)
    average_daily_dollar_volume: Mapped[str | None] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    raw_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String, index=True)


class ResearchItem(Base):
    """One immutable, run-scoped copy of a collected research source."""

    __tablename__ = "research_items"
    __table_args__ = (
        UniqueConstraint("run_id", "content_hash"),
        CheckConstraint(
            "source_tier IN ('BROKER', 'PRIMARY', 'WEB', 'PAID', 'SOCIAL', 'LEGACY')",
            name="valid_source_tier",
        ),
        Index("ix_research_items_run_retrieved", "run_id", "retrieved_at"),
        Index("ix_research_items_published", "published_at"),
        Index("ix_research_items_provider_item", "provider", "provider_item_id"),
        Index("ix_research_items_tier_retrieved", "source_tier", "retrieved_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    source_tier: Mapped[str] = mapped_column(String, default="LEGACY")
    source_type: Mapped[str] = mapped_column(String)
    source_name: Mapped[str] = mapped_column(String)
    provider: Mapped[str | None] = mapped_column(String)
    provider_item_id: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    headline: Mapped[str] = mapped_column(Text)
    normalized_summary: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String)
    raw_artifact_path: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[str | None] = mapped_column(String)


class ResearchItemSymbol(Base):
    """Queryable symbol association for a run-scoped research item."""

    __tablename__ = "research_item_symbols"
    __table_args__ = (Index("ix_research_item_symbols_symbol_research", "symbol", "research_id"),)

    research_id: Mapped[str] = mapped_column(
        ForeignKey("research_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ResearchItemQuestion(Base):
    """A research question for which a collected item was evidence."""

    __tablename__ = "research_item_questions"
    __table_args__ = (
        Index("ix_research_item_questions_question_research", "question_id", "research_id"),
    )

    research_id: Mapped[str] = mapped_column(
        ForeignKey("research_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    question_id: Mapped[str] = mapped_column(String, primary_key=True)
    question_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    markdown_path: Mapped[str | None] = mapped_column(Text)


class Thesis(Base):
    __tablename__ = "theses"
    __table_args__ = (Index("ix_theses_symbol_status", "symbol", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    symbol: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    confidence: Mapped[float] = mapped_column(Float)
    invalidation_json: Mapped[str] = mapped_column(Text, default="[]")
    markdown_path: Mapped[str | None] = mapped_column(Text)


class ThesisStrategyLink(Base):
    __tablename__ = "thesis_strategy_links"

    thesis_id: Mapped[str] = mapped_column(
        ForeignKey("theses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    strategy_id: Mapped[str] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ThesisEvidence(Base):
    __tablename__ = "thesis_evidence"
    __table_args__ = (UniqueConstraint("thesis_id", "research_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    thesis_id: Mapped[str] = mapped_column(
        ForeignKey("theses.id", ondelete="CASCADE"),
        index=True,
    )
    research_id: Mapped[str] = mapped_column(
        ForeignKey("research_items.id", ondelete="CASCADE"),
        index=True,
    )
    relationship: Mapped[str] = mapped_column(String)
    agent_explanation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeChange(Base):
    __tablename__ = "knowledge_changes"
    __table_args__ = (Index("ix_knowledge_changes_entity_created", "entity_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String, index=True)
    change_type: Mapped[str] = mapped_column(String)
    before_text: Mapped[str] = mapped_column(Text)
    after_text: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    evidence_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentInvocation(Base):
    __tablename__ = "agent_invocations"
    __table_args__ = (Index("ix_agent_invocations_run_started", "run_id", "started_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    request_path: Mapped[str] = mapped_column(Text)
    response_path: Mapped[str | None] = mapped_column(Text)
    input_token_count: Mapped[int | None] = mapped_column()
    output_token_count: Mapped[int | None] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, default="STARTED")
    error_summary: Mapped[str | None] = mapped_column(Text)
    evidence_manifest_hash: Mapped[str | None] = mapped_column(String)


class AgentInvocationEvidence(Base):
    """The exact ordered evidence list supplied to one model invocation."""

    __tablename__ = "agent_invocation_evidence"
    __table_args__ = (
        UniqueConstraint(
            "agent_invocation_id",
            "ordinal",
            name="uq_agent_invocation_evidence_invocation_ordinal",
        ),
        UniqueConstraint(
            "agent_invocation_id",
            "research_id",
            name="uq_agent_invocation_evidence_invocation_research",
        ),
        CheckConstraint("ordinal >= 0", name="non_negative_ordinal"),
        Index(
            "ix_agent_invocation_evidence_invocation_ordinal",
            "agent_invocation_id",
            "ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    agent_invocation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_invocations.id", ondelete="CASCADE"),
    )
    research_id: Mapped[str] = mapped_column(
        ForeignKey("research_items.id", ondelete="RESTRICT"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TradeProposalRecord(Base):
    __tablename__ = "trade_proposals"
    __table_args__ = (
        Index("ix_trade_proposals_run_created", "run_id", "created_at"),
        Index("ix_trade_proposals_symbol_created", "symbol", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    agent_invocation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_invocations.id", ondelete="SET NULL"),
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    requested_notional: Mapped[str | None] = mapped_column(String)
    target_position_pct: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    thesis_id: Mapped[str | None] = mapped_column(
        ForeignKey("theses.id", ondelete="SET NULL"),
        index=True,
    )
    raw_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RiskDecisionRecord(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (
        UniqueConstraint("run_id", "proposal_id"),
        Index("ix_risk_decisions_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    # Logical reference retained for compatibility with pre-proposal execution records.
    proposal_id: Mapped[str] = mapped_column(String, index=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    reason_codes_json: Mapped[str] = mapped_column(Text)
    normalized_order_json: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    policy_hash: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BrokerOrderRecord(Base):
    __tablename__ = "broker_orders"
    __table_args__ = (
        Index("ix_broker_orders_run_status", "run_id", "status"),
        Index("ix_broker_orders_proposal", "proposal_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    # Logical reference retained because the executor may predate proposal persistence.
    proposal_id: Mapped[str] = mapped_column(String)
    client_order_id: Mapped[str] = mapped_column(String, unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String, unique=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    order_type: Mapped[str] = mapped_column(String, default="limit")
    qty: Mapped[str | None] = mapped_column(String)
    notional: Mapped[str | None] = mapped_column(String)
    limit_price: Mapped[str | None] = mapped_column(String)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String)
    raw_submission_path: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BrokerOrderEvent(Base):
    __tablename__ = "broker_order_events"
    __table_args__ = (
        UniqueConstraint("event_key"),
        Index("ix_broker_order_events_order_occurred", "broker_order_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    broker_order_record_id: Mapped[str] = mapped_column(
        "broker_order_id",
        ForeignKey("broker_orders.id", ondelete="CASCADE"),
    )
    event_key: Mapped[str] = mapped_column(String)
    broker_event_id: Mapped[str | None] = mapped_column(String, unique=True)
    event_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    raw_event_path: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("broker_activity_id"),
        Index("ix_fills_order_transaction", "broker_order_id", "transaction_time"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    broker_order_record_id: Mapped[str] = mapped_column(
        "broker_order_id",
        ForeignKey("broker_orders.id", ondelete="CASCADE"),
    )
    broker_activity_id: Mapped[str] = mapped_column(String)
    qty: Mapped[str] = mapped_column(String)
    price: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    transaction_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    commission: Mapped[str | None] = mapped_column(String)
    raw_activity_path: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("run_id", "version"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(default=1)
    report_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "period"),
        Index("ix_performance_snapshots_captured", "captured_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"),
        index=True,
    )
    period: Mapped[str] = mapped_column(String, default="daily")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    equity: Mapped[str] = mapped_column(String)
    cash: Mapped[str | None] = mapped_column(String)
    pnl: Mapped[str | None] = mapped_column(String)
    return_pct: Mapped[str | None] = mapped_column(String)
    drawdown_pct: Mapped[str | None] = mapped_column(String)
    benchmark_symbol: Mapped[str | None] = mapped_column(String)
    benchmark_return_pct: Mapped[str | None] = mapped_column(String)
    raw_json: Mapped[str | None] = mapped_column(Text)
