"""place agent invocations in a workflow

Revision ID: a1f4c2d90b57
Revises: c4b3e2a19f70
Create Date: 2026-08-22 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1f4c2d90b57"
down_revision: str | None = "c4b3e2a19f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("research_compactor", "daily_trader", "event_trader", "weekly_strategist")


def upgrade() -> None:
    """Give every invocation a role, a workflow position, and a parent."""
    connection = op.get_bind()
    unmapped = (
        connection.execute(
            sa.text(
                "SELECT DISTINCT purpose FROM agent_invocations WHERE "
                + " AND ".join(f"purpose NOT LIKE '{role}%'" for role in ROLES)
            )
        )
        .scalars()
        .all()
    )
    if unmapped:
        raise RuntimeError(
            "cannot derive a role for existing invocations: " + ", ".join(sorted(unmapped))
        )

    with op.batch_alter_table("agent_invocations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("role", sa.String(), server_default="daily_trader", nullable=False)
        )
        batch_op.add_column(sa.Column("step", sa.String(), server_default="", nullable=False))
        batch_op.add_column(
            sa.Column("attempt", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("parent_invocation_id", sa.String(), nullable=True))

    for role in ROLES:
        connection.execute(
            sa.text("UPDATE agent_invocations SET role = :role WHERE purpose LIKE :prefix"),
            {"role": role, "prefix": f"{role}%"},
        )

    # Remove migration-only defaults now that every existing row carries a real role.
    with op.batch_alter_table("agent_invocations", schema=None) as batch_op:
        batch_op.alter_column(
            "role", existing_type=sa.String(), server_default=None, existing_nullable=False
        )
        batch_op.alter_column(
            "step", existing_type=sa.String(), server_default=None, existing_nullable=False
        )
        batch_op.alter_column(
            "attempt", existing_type=sa.Integer(), server_default=None, existing_nullable=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_agent_invocations_parent_invocation_id_agent_invocations"),
            "agent_invocations",
            ["parent_invocation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_agent_invocations_positive_attempt"),
            "attempt >= 1",
        )
        batch_op.create_unique_constraint(
            "uq_agent_invocations_run_role_step_attempt",
            ["run_id", "role", "step", "attempt"],
        )
        batch_op.create_index(
            "ix_agent_invocations_run_role",
            ["run_id", "role"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_agent_invocations_parent_invocation_id"),
            ["parent_invocation_id"],
            unique=False,
        )


def downgrade() -> None:
    """Restore the flat invocation record, refusing to discard real workflow structure."""
    connection = op.get_bind()
    structured = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM agent_invocations "
            "WHERE step != '' OR attempt != 1 OR parent_invocation_id IS NOT NULL"
        )
    ).scalar_one()
    if structured:
        raise RuntimeError(
            f"cannot downgrade while {structured} invocations record a workflow position"
        )

    with op.batch_alter_table("agent_invocations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_invocations_parent_invocation_id"))
        batch_op.drop_index("ix_agent_invocations_run_role")
        batch_op.drop_constraint(
            "uq_agent_invocations_run_role_step_attempt", type_="unique"
        )
        batch_op.drop_constraint(
            batch_op.f("ck_agent_invocations_positive_attempt"), type_="check"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_agent_invocations_parent_invocation_id_agent_invocations"),
            type_="foreignkey",
        )
        batch_op.drop_column("parent_invocation_id")
        batch_op.drop_column("attempt")
        batch_op.drop_column("step")
        batch_op.drop_column("role")
