"""add durable event scheduler

Revision ID: 8b72d7bb46bd
Revises: dd4d9611c628
Create Date: 2026-08-20 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b72d7bb46bd"
down_revision: str | None = "dd4d9611c628"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add source-backed events, durable runs, and lifecycle history."""
    op.create_table(
        "market_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("symbols_json", sa.Text(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_by_run_id", sa.String(), nullable=True),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_market_events_valid_confidence"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CANCELLED')",
            name=op.f("ck_market_events_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_run_id"],
            ["runs.id"],
            name=op.f("fk_market_events_created_by_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_events")),
        sa.UniqueConstraint("event_key", name=op.f("uq_market_events_event_key")),
        sa.UniqueConstraint(
            "source",
            "source_event_id",
            name=op.f("uq_market_events_source"),
        ),
    )
    with op.batch_alter_table("market_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_market_events_created_by_run_id"),
            ["created_by_run_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_market_events_event_type"), ["event_type"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_market_events_scheduled_at"), ["scheduled_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_market_events_status"), ["status"], unique=False)
        batch_op.create_index(
            "ix_market_events_type_scheduled", ["event_type", "scheduled_at"], unique=False
        )

    op.create_table(
        "scheduled_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schedule_key", sa.String(), nullable=False),
        sa.Column("market_event_id", sa.String(), nullable=False),
        sa.Column("created_by_run_id", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("symbols_json", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'RUNNING', 'COMPLETED', "
            "'FAILED', 'CANCELLED', 'EXPIRED')",
            name=op.f("ck_scheduled_runs_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_run_id"],
            ["runs.id"],
            name=op.f("fk_scheduled_runs_created_by_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["market_event_id"],
            ["market_events.id"],
            name=op.f("fk_scheduled_runs_market_event_id_market_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_scheduled_runs_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_runs")),
        sa.UniqueConstraint("lease_token", name=op.f("uq_scheduled_runs_lease_token")),
        sa.UniqueConstraint("run_id", name=op.f("uq_scheduled_runs_run_id")),
        sa.UniqueConstraint("schedule_key", name=op.f("uq_scheduled_runs_schedule_key")),
    )
    with op.batch_alter_table("scheduled_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_scheduled_runs_created_by_run_id"),
            ["created_by_run_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_runs_event_type"), ["event_type"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_runs_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_runs_market_event_id"),
            ["market_event_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_runs_scheduled_for"), ["scheduled_for"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_scheduled_runs_status"), ["status"], unique=False)
        batch_op.create_index(
            "ix_scheduled_runs_status_due", ["status", "scheduled_for"], unique=False
        )

    op.create_table(
        "scheduled_run_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("scheduled_run_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["scheduled_run_id"],
            ["scheduled_runs.id"],
            name=op.f("fk_scheduled_run_events_scheduled_run_id_scheduled_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_run_events")),
    )
    with op.batch_alter_table("scheduled_run_events", schema=None) as batch_op:
        batch_op.create_index(
            "ix_scheduled_run_events_run_occurred",
            ["scheduled_run_id", "occurred_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_run_events_scheduled_run_id"),
            ["scheduled_run_id"],
            unique=False,
        )


def downgrade() -> None:
    """Remove the durable scheduler tables."""
    with op.batch_alter_table("scheduled_run_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scheduled_run_events_scheduled_run_id"))
        batch_op.drop_index("ix_scheduled_run_events_run_occurred")
    op.drop_table("scheduled_run_events")

    with op.batch_alter_table("scheduled_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_scheduled_runs_status_due")
        batch_op.drop_index(batch_op.f("ix_scheduled_runs_status"))
        batch_op.drop_index(batch_op.f("ix_scheduled_runs_scheduled_for"))
        batch_op.drop_index(batch_op.f("ix_scheduled_runs_market_event_id"))
        batch_op.drop_index(batch_op.f("ix_scheduled_runs_expires_at"))
        batch_op.drop_index(batch_op.f("ix_scheduled_runs_event_type"))
        batch_op.drop_index(batch_op.f("ix_scheduled_runs_created_by_run_id"))
    op.drop_table("scheduled_runs")

    with op.batch_alter_table("market_events", schema=None) as batch_op:
        batch_op.drop_index("ix_market_events_type_scheduled")
        batch_op.drop_index(batch_op.f("ix_market_events_status"))
        batch_op.drop_index(batch_op.f("ix_market_events_scheduled_at"))
        batch_op.drop_index(batch_op.f("ix_market_events_event_type"))
        batch_op.drop_index(batch_op.f("ix_market_events_created_by_run_id"))
    op.drop_table("market_events")
