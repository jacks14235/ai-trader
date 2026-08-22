import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from trader.agent.models import TradeProposal
from trader.persistence.db import create_session_factory
from trader.persistence.models import (
    AgentInvocation,
    Base,
    BrokerOrderRecord,
    Fill,
    ResearchItem,
    ResearchItemQuestion,
    ResearchItemSymbol,
    RiskDecisionRecord,
    Run,
)
from trader.persistence.repositories import (
    PersistenceConflictError,
    associate_agent_invocation_evidence,
    available_research_as_of,
    claim_run,
    get_run_audit_trail,
    persist_research_item,
    persist_risk_decision,
    persist_trade_proposal,
    record_broker_order_event,
    record_fill,
)
from trader.risk.models import NormalizedOrder, RiskDecision


def test_run_window_is_atomic_and_unique(tmp_path):
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    scheduled_for = datetime.now(UTC)

    assert claim_run(session, "daily:key", scheduled_for, "hash") is not None
    assert claim_run(session, "daily:key", scheduled_for, "hash") is None
    with pytest.raises(ValueError, match="only paper-trading"):
        claim_run(session, "live:key", scheduled_for, "hash", mode="live")


def test_proposal_and_risk_decision_retries_are_idempotent(tmp_path):
    session, run = _session_and_run(tmp_path)
    proposal = _proposal()
    decision = _decision(str(proposal.proposal_id))

    first_proposal = persist_trade_proposal(session, run.id, proposal)
    retried_proposal = persist_trade_proposal(session, run.id, proposal)
    first_decision = persist_risk_decision(session, run.id, decision, policy_hash="policy-v1")
    retried_decision = persist_risk_decision(session, run.id, decision, policy_hash="policy-v1")

    assert first_proposal.id == retried_proposal.id
    assert first_decision.id == retried_decision.id
    assert len(session.scalars(select(RiskDecisionRecord)).all()) == 1


def test_idempotency_key_reuse_with_changed_content_fails_closed(tmp_path):
    session, run = _session_and_run(tmp_path)
    proposal = _proposal()
    persist_trade_proposal(session, run.id, proposal)
    changed = proposal.model_copy(update={"rationale": "changed after persistence"})

    with pytest.raises(PersistenceConflictError):
        persist_trade_proposal(session, run.id, changed)


def test_broker_events_and_fills_are_idempotent_and_retrievable(tmp_path):
    session, run = _session_and_run(tmp_path)
    order = BrokerOrderRecord(
        run_id=run.id,
        proposal_id="proposal-1",
        client_order_id="client-1",
        symbol="AAA",
        side="buy",
        status="SUBMITTED",
    )
    session.add(order)
    session.commit()
    occurred_at = datetime.now(UTC)

    first_event = record_broker_order_event(
        session,
        broker_order_record_id=order.id,
        event_key="order-1/accepted",
        event_type="STATUS",
        status="ACCEPTED",
        occurred_at=occurred_at,
    )
    retried_event = record_broker_order_event(
        session,
        broker_order_record_id=order.id,
        event_key="order-1/accepted",
        event_type="STATUS",
        status="ACCEPTED",
        occurred_at=occurred_at,
    )
    first_fill = record_fill(
        session,
        broker_order_record_id=order.id,
        broker_activity_id="activity-1",
        qty=Decimal("1.25"),
        price=Decimal("10.01"),
        side="buy",
        transaction_time=occurred_at,
    )
    retried_fill = record_fill(
        session,
        broker_order_record_id=order.id,
        broker_activity_id="activity-1",
        qty=Decimal("1.25"),
        price=Decimal("10.01"),
        side="buy",
        transaction_time=occurred_at,
    )
    trail = get_run_audit_trail(session, run.id)

    assert first_event.id == retried_event.id
    assert first_fill.id == retried_fill.id
    assert len(trail.broker_order_events) == 1
    assert len(trail.fills) == 1
    assert len(session.scalars(select(Fill)).all()) == 1


def test_historical_research_excludes_future_knowledge(tmp_path):
    session, run = _session_and_run(tmp_path)
    cutoff = datetime.now(UTC)
    visible = _research_item(run.id, "visible", cutoff - timedelta(hours=1))
    not_yet_retrieved = _research_item(run.id, "future", cutoff + timedelta(hours=1))
    session.add_all([visible, not_yet_retrieved])
    session.commit()

    results = available_research_as_of(session, cutoff)

    assert [item.id for item in results] == [visible.id]


def test_research_persistence_is_idempotent_and_detects_immutable_conflicts(tmp_path):
    session, run = _session_and_run(tmp_path)
    retrieved_at = datetime.now(UTC)
    arguments = _research_arguments(run.id, "research-1", "same source", retrieved_at)

    first = persist_research_item(session, **arguments)
    retried = persist_research_item(session, **arguments)

    assert first.id == retried.id
    assert session.scalars(select(ResearchItemSymbol)).all()[0].symbol == "AAPL"
    assert session.scalars(select(ResearchItemQuestion)).all()[0].question_id == "q:material"
    changed = {**arguments, "normalized_summary": "rewritten after collection"}
    with pytest.raises(PersistenceConflictError, match="research item"):
        persist_research_item(session, **changed)


def test_same_content_is_run_scoped_and_context_is_many_to_many(tmp_path):
    session, first_run = _session_and_run(tmp_path)
    second_run = Run(
        run_key="daily:second",
        scheduled_for=datetime.now(UTC),
        config_hash="config-v1",
    )
    session.add(second_run)
    session.commit()
    retrieved_at = datetime.now(UTC)
    first_arguments = _research_arguments(first_run.id, "research-1", "shared", retrieved_at)
    first = persist_research_item(session, **first_arguments)

    second_question = {
        **first_arguments,
        "research_id": "alternate-caller-id",
        "symbols": ["MSFT"],
        "research_question_id": "q:industry",
        "research_question": "What changed in the industry?",
    }
    deduplicated = persist_research_item(session, **second_question)
    second_run_arguments = {
        **first_arguments,
        "research_id": "research-2",
        "run_id": second_run.id,
        "raw_artifact_path": "raw/second/shared.json",
    }
    second = persist_research_item(session, **second_run_arguments)

    assert deduplicated.id == first.id
    assert second.id != first.id
    assert first.content_hash == second.content_hash
    assert len(session.scalars(select(ResearchItem)).all()) == 2
    first_symbols = session.scalars(
        select(ResearchItemSymbol.symbol)
        .where(ResearchItemSymbol.research_id == first.id)
        .order_by(ResearchItemSymbol.symbol)
    ).all()
    first_questions = session.scalars(
        select(ResearchItemQuestion.question_id)
        .where(ResearchItemQuestion.research_id == first.id)
        .order_by(ResearchItemQuestion.question_id)
    ).all()
    assert first_symbols == ["AAPL", "MSFT"]
    assert first_questions == ["q:industry", "q:material"]


def test_research_as_of_is_bounded_and_filterable(tmp_path):
    session, run = _session_and_run(tmp_path)
    cutoff = datetime.now(UTC)
    primary = persist_research_item(
        session,
        **_research_arguments(run.id, "primary", "primary", cutoff - timedelta(minutes=2)),
    )
    web_arguments = _research_arguments(
        run.id,
        "web",
        "web",
        cutoff - timedelta(minutes=1),
    )
    web_arguments.update(
        source_tier="WEB",
        source_type="news",
        source_name="Example News",
        provider="web-search",
        provider_item_id="web-1",
        symbols=["MSFT"],
        research_question_id="q:industry",
        research_question="What changed in the industry?",
    )
    persist_research_item(session, **web_arguments)

    results = available_research_as_of(
        session,
        cutoff,
        run_id=run.id,
        symbol="aapl",
        source_tiers=["primary"],
        provider="sec",
        research_question_id="q:material",
        limit=1,
    )

    assert [item.id for item in results] == [primary.id]
    with pytest.raises(ValueError, match="limit"):
        available_research_as_of(session, cutoff, limit=501)
    with pytest.raises(ValueError, match="timezone-aware"):
        available_research_as_of(session, cutoff.replace(tzinfo=None))


def test_invocation_evidence_is_ordered_idempotent_and_in_audit_trail(tmp_path):
    session, run = _session_and_run(tmp_path)
    retrieved_at = datetime.now(UTC)
    first = persist_research_item(
        session,
        **_research_arguments(run.id, "research-1", "first", retrieved_at),
    )
    second_arguments = _research_arguments(run.id, "research-2", "second", retrieved_at)
    second_arguments["provider_item_id"] = "provider-second"
    second = persist_research_item(session, **second_arguments)
    invocation = AgentInvocation(
        id="invocation-1",
        run_id=run.id,
        purpose="research synthesis",
        model="not-active",
        provider="test",
        prompt_version="v1",
        request_path="raw/request.json",
    )
    session.add(invocation)
    session.commit()

    first_links = associate_agent_invocation_evidence(
        session,
        agent_invocation_id=invocation.id,
        research_ids=[second.id, first.id],
    )
    retried_links = associate_agent_invocation_evidence(
        session,
        agent_invocation_id=invocation.id,
        research_ids=[second.id, first.id],
    )
    trail = get_run_audit_trail(session, run.id)

    assert [link.id for link in retried_links] == [link.id for link in first_links]
    assert [link.research_id for link in trail.agent_invocation_evidence] == [
        second.id,
        first.id,
    ]
    assert len(trail.research_item_symbols) == 2
    assert len(trail.research_item_questions) == 2
    with pytest.raises(PersistenceConflictError, match="agent invocation evidence"):
        associate_agent_invocation_evidence(
            session,
            agent_invocation_id=invocation.id,
            research_ids=[first.id, second.id],
        )


def test_invocation_evidence_must_come_from_the_same_run(tmp_path):
    session, run = _session_and_run(tmp_path)
    other = Run(
        run_key="daily:other",
        scheduled_for=datetime.now(UTC),
        config_hash="config-v1",
    )
    session.add(other)
    session.commit()
    item = persist_research_item(
        session,
        **_research_arguments(other.id, "research-other", "other", datetime.now(UTC)),
    )
    invocation = AgentInvocation(
        run_id=run.id,
        purpose="research synthesis",
        model="not-active",
        provider="test",
        prompt_version="v1",
        request_path="raw/request.json",
    )
    session.add(invocation)
    session.commit()

    with pytest.raises(ValueError, match="invocation run"):
        associate_agent_invocation_evidence(
            session,
            agent_invocation_id=invocation.id,
            research_ids=[item.id],
        )


def test_initial_migration_matches_metadata_and_is_idempotent(tmp_path):
    database_path = tmp_path / "migrated.sqlite"
    database_url = f"sqlite:///{database_path}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    inspector = inspect(create_engine(database_url))
    expected_tables = set(Base.metadata.tables)
    assert set(inspector.get_table_names()) == expected_tables | {"alembic_version"}
    unique_column_sets = {
        frozenset(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("broker_orders")
    }
    assert frozenset({"client_order_id"}) in unique_column_sets
    assert frozenset({"broker_order_id"}) in unique_column_sets
    research_unique_column_sets = {
        frozenset(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("research_items")
    }
    assert frozenset({"run_id", "content_hash"}) in research_unique_column_sets
    assert frozenset({"content_hash"}) not in research_unique_column_sets
    session = create_session_factory(database_url, create_schema=False)()
    assert session.scalar(select(Run)) is None


def test_research_migration_preserves_legacy_rows_and_downgrades(tmp_path):
    database_path = tmp_path / "legacy.sqlite"
    database_url = f"sqlite:///{database_path}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "8b72d7bb46bd")
    engine = create_engine(database_url)
    timestamp = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs "
                "(id, run_key, mode, scheduled_for, started_at, status, config_hash) "
                "VALUES (:id, :key, 'paper', :scheduled, :started, 'COMPLETED', 'hash')"
            ),
            {
                "id": "legacy-run",
                "key": "legacy:key",
                "scheduled": timestamp,
                "started": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO research_items "
                "(id, run_id, source_type, source_name, published_at, retrieved_at, "
                "headline, normalized_summary, content_hash, raw_content_path) "
                "VALUES ('legacy-item', 'legacy-run', 'news', 'legacy', :published, "
                ":retrieved, 'headline', 'summary', 'legacy-hash', 'raw/legacy.json')"
            ),
            {"published": timestamp, "retrieved": timestamp},
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        migrated = connection.execute(
            text(
                "SELECT source_tier, normalized_text, raw_artifact_path "
                "FROM research_items WHERE id = 'legacy-item'"
            )
        ).one()
    assert tuple(migrated) == ("LEGACY", "", "raw/legacy.json")

    command.downgrade(config, "8b72d7bb46bd")
    inspector = inspect(engine)
    assert "raw_content_path" in {
        column["name"] for column in inspector.get_columns("research_items")
    }
    assert "research_item_symbols" not in inspector.get_table_names()


def _session_and_run(tmp_path):
    session = create_session_factory(f"sqlite:///{tmp_path}/db.sqlite")()
    run = Run(
        run_key="daily:key",
        scheduled_for=datetime.now(UTC),
        config_hash="config-v1",
    )
    session.add(run)
    session.commit()
    return session, run


def _proposal() -> TradeProposal:
    return TradeProposal(
        symbol="AAA",
        action="BUY",
        target_notional_usd=Decimal("100"),
        confidence=0.8,
        time_horizon="months",
        rationale="evidence-based",
        max_acceptable_price=Decimal("10.10"),
    )


def _decision(proposal_id: str) -> RiskDecision:
    return RiskDecision(
        proposal_id=proposal_id,
        approved=True,
        normalized_order=NormalizedOrder(
            symbol="AAA",
            side="buy",
            qty=Decimal("10"),
            limit_price=Decimal("10"),
            notional=Decimal("100"),
        ),
        human_explanation="within all paper risk limits",
    )


def _research_item(run_id: str, item_id: str, retrieved_at: datetime) -> ResearchItem:
    return ResearchItem(
        id=item_id,
        run_id=run_id,
        source_type="news",
        source_name="test source",
        published_at=retrieved_at - timedelta(minutes=1),
        retrieved_at=retrieved_at,
        headline=item_id,
        normalized_summary="summary",
        content_hash=f"hash-{item_id}",
    )


def _research_arguments(
    run_id: str,
    research_id: str,
    content: str,
    retrieved_at: datetime,
) -> dict[str, object]:
    return {
        "research_id": research_id,
        "run_id": run_id,
        "symbols": ["AAPL"],
        "source_tier": "PRIMARY",
        "source_type": "filing",
        "source_name": "SEC EDGAR",
        "provider": "sec",
        "provider_item_id": f"provider-{content}",
        "research_question_id": "q:material",
        "research_question": "What material information changed?",
        "raw_artifact_path": f"raw/{run_id}/{content}.json",
        "normalized_summary": f"Summary for {content}",
        "normalized_text": f"Normalized text for {content}",
        "published_at": retrieved_at - timedelta(minutes=1),
        "retrieved_at": retrieved_at,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "headline": f"Headline for {content}",
        "url": f"https://example.test/{content}",
        "author": "Issuer",
        "metadata": {"form": "8-K"},
        "cost_usd": Decimal("0.01"),
    }
