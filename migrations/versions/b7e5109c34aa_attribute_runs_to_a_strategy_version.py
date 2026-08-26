"""attribute runs to a strategy version

Revision ID: b7e5109c34aa
Revises: a1f4c2d90b57
Create Date: 2026-08-22 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e5109c34aa"
down_revision: str | None = "a1f4c2d90b57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record which strategy version each run reasoned under, and hash each version."""
    with op.batch_alter_table("strategies", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("content_hash", sa.String(), server_default="", nullable=False)
        )
        batch_op.create_index(
            batch_op.f("ix_strategies_content_hash"), ["content_hash"], unique=True
        )

    with op.batch_alter_table("strategies", schema=None) as batch_op:
        batch_op.alter_column(
            "content_hash",
            existing_type=sa.String(),
            server_default=None,
            existing_nullable=False,
        )

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("strategy_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_runs_strategy_id_strategies"),
            "strategies",
            ["strategy_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(batch_op.f("ix_runs_strategy_id"), ["strategy_id"], unique=False)


def downgrade() -> None:
    """Drop strategy attribution, refusing to discard versions runs already reference."""
    connection = op.get_bind()
    attributed = connection.execute(
        sa.text("SELECT COUNT(*) FROM runs WHERE strategy_id IS NOT NULL")
    ).scalar_one()
    if attributed:
        raise RuntimeError(
            f"cannot downgrade while {attributed} runs are attributed to a strategy version"
        )

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_strategy_id"))
        batch_op.drop_constraint(batch_op.f("fk_runs_strategy_id_strategies"), type_="foreignkey")
        batch_op.drop_column("strategy_id")

    with op.batch_alter_table("strategies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_strategies_content_hash"))
        batch_op.drop_column("content_hash")
