"""add complete agent invocation usage attribution

Revision ID: b83e219fc641
Revises: a71d5e30c924
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b83e219fc641"
down_revision: str | None = "a71d5e30c924"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_invocations", sa.Column("book_id", sa.String(), nullable=True))
    op.add_column(
        "agent_invocations", sa.Column("book_evaluation_id", sa.String(), nullable=True)
    )
    op.add_column("agent_invocations", sa.Column("model_profile", sa.String(), nullable=True))
    op.add_column("agent_invocations", sa.Column("reasoning_effort", sa.String(), nullable=True))
    op.add_column(
        "agent_invocations", sa.Column("cached_input_token_count", sa.Integer(), nullable=True)
    )
    op.add_column(
        "agent_invocations", sa.Column("reasoning_output_token_count", sa.Integer(), nullable=True)
    )
    op.add_column(
        "agent_invocations", sa.Column("total_token_count", sa.Integer(), nullable=True)
    )
    op.add_column("agent_invocations", sa.Column("usage_json", sa.Text(), nullable=True))
    op.add_column("agent_invocations", sa.Column("cost_usd", sa.String(), nullable=True))
    op.add_column(
        "agent_invocations",
        sa.Column(
            "cost_source",
            sa.String(),
            nullable=False,
            server_default="NOT_REPORTED",
        ),
    )
    op.add_column("agent_invocations", sa.Column("pricing_json", sa.Text(), nullable=True))
    with op.batch_alter_table("agent_invocations") as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_agent_invocations_book_id_books"),
            "books",
            ["book_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(op.f("ix_agent_invocations_book_id"), "agent_invocations", ["book_id"])
    op.create_index(
        op.f("ix_agent_invocations_book_evaluation_id"),
        "agent_invocations",
        ["book_evaluation_id"],
    )
    op.get_bind().execute(
        sa.text(
            "UPDATE agent_invocations SET total_token_count = "
            "input_token_count + output_token_count "
            "WHERE input_token_count IS NOT NULL AND output_token_count IS NOT NULL"
        )
    )
    # Profiles introduced the namespaced step before explicit invocation scope. Recover every
    # historical packet and manager call whose book evaluation still exists.
    op.get_bind().execute(
        sa.text(
            "UPDATE agent_invocations AS ai SET "
            "book_id = (SELECT be.book_id FROM book_evaluations AS be "
            "JOIN books AS b ON b.id = be.book_id "
            "WHERE be.run_id = ai.run_id AND ai.step LIKE "
            "'book_' || replace(b.id, '-', '') || '_' || replace(b.name, '-', '_') || '_%' "
            "LIMIT 1), "
            "book_evaluation_id = (SELECT be.id FROM book_evaluations AS be "
            "JOIN books AS b ON b.id = be.book_id "
            "WHERE be.run_id = ai.run_id AND ai.step LIKE "
            "'book_' || replace(b.id, '-', '') || '_' || replace(b.name, '-', '_') || '_%' "
            "LIMIT 1) "
            "WHERE EXISTS (SELECT 1 FROM book_evaluations AS be "
            "JOIN books AS b ON b.id = be.book_id "
            "WHERE be.run_id = ai.run_id AND ai.step LIKE "
            "'book_' || replace(b.id, '-', '') || '_' || replace(b.name, '-', '_') || '_%')"
        )
    )


def downgrade() -> None:
    detailed = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM agent_invocations WHERE "
            "book_id IS NOT NULL OR model_profile IS NOT NULL OR reasoning_effort IS NOT NULL OR "
            "cached_input_token_count IS NOT NULL OR reasoning_output_token_count IS NOT NULL OR "
            "total_token_count IS NOT NULL OR usage_json IS NOT NULL OR cost_usd IS NOT NULL OR "
            "pricing_json IS NOT NULL"
        )
    ).scalar_one()
    if detailed:
        raise RuntimeError(
            f"cannot downgrade while {detailed} detailed agent usage records exist"
        )
    op.drop_index(op.f("ix_agent_invocations_book_evaluation_id"), "agent_invocations")
    op.drop_index(op.f("ix_agent_invocations_book_id"), "agent_invocations")
    with op.batch_alter_table("agent_invocations") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_agent_invocations_book_id_books"), type_="foreignkey"
        )
        for column in (
            "pricing_json",
            "cost_source",
            "cost_usd",
            "usage_json",
            "total_token_count",
            "reasoning_output_token_count",
            "cached_input_token_count",
            "reasoning_effort",
            "model_profile",
            "book_evaluation_id",
            "book_id",
        ):
            batch_op.drop_column(column)
