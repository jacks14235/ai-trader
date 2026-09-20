"""Add queryable abstention and dissent decisions.

Revision ID: f4c8a12d9e70
Revises: e2f7a901bc43
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c8a12d9e70"
down_revision: str | None = "e2f7a901bc43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_decisions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("agent_invocation_id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=True),
        sa.Column("book_evaluation_id", sa.String(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("abstention_classification", sa.String(), nullable=True),
        sa.Column("abstention_json", sa.Text(), nullable=True),
        sa.Column("dissent_dispositions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["agent_invocation_id"], ["agent_invocations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["book_evaluation_id"], ["book_evaluations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("agent_invocation_id"),
        sa.UniqueConstraint("run_id", "book_id"),
        sa.CheckConstraint(
            "status IN ('NO_ACTION', 'PROPOSE_TRADES')", name="valid_status"
        ),
        sa.CheckConstraint(
            "(status = 'NO_ACTION' AND abstention_json IS NOT NULL) OR "
            "(status = 'PROPOSE_TRADES' AND abstention_json IS NULL)",
            name="consistent_abstention",
        ),
        sa.CheckConstraint(
            "(book_id IS NULL AND book_evaluation_id IS NULL) OR "
            "(book_id IS NOT NULL AND book_evaluation_id IS NOT NULL)",
            name="consistent_book_scope",
        ),
    )
    for column in (
        "run_id",
        "agent_invocation_id",
        "book_id",
        "book_evaluation_id",
        "status",
    ):
        op.create_index(f"ix_agent_decisions_{column}", "agent_decisions", [column])
    op.create_index(
        "ix_agent_decisions_abstention_classification",
        "agent_decisions",
        ["abstention_classification"],
    )
    op.create_index(
        "ix_agent_decisions_run_created",
        "agent_decisions",
        ["run_id", "created_at"],
    )
    op.create_index(
        "uq_agent_decisions_live_run",
        "agent_decisions",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("book_id IS NULL"),
        postgresql_where=sa.text("book_id IS NULL"),
    )


def downgrade() -> None:
    recorded = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM agent_decisions")).scalar_one()
    if recorded:
        raise RuntimeError(f"cannot downgrade while {recorded} agent decisions are recorded")
    op.drop_table("agent_decisions")
