"""add research audit persistence

Revision ID: c4b3e2a19f70
Revises: 8b72d7bb46bd
Create Date: 2026-08-21 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4b3e2a19f70"
down_revision: str | None = "8b72d7bb46bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make research run-scoped and add queryable context/evidence links."""
    with op.batch_alter_table("research_items", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("uq_research_items_content_hash"),
            type_="unique",
        )
        batch_op.add_column(
            sa.Column(
                "source_tier",
                sa.String(),
                server_default="LEGACY",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("provider", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provider_item_id", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "normalized_text",
                sa.Text(),
                server_default="",
                nullable=False,
            )
        )
        batch_op.alter_column(
            "raw_content_path",
            existing_type=sa.Text(),
            new_column_name="raw_artifact_path",
            existing_nullable=True,
        )
        batch_op.add_column(sa.Column("cost_usd", sa.String(), nullable=True))
        batch_op.create_unique_constraint(
            batch_op.f("uq_research_items_run_id"),
            ["run_id", "content_hash"],
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_research_items_valid_source_tier"),
            "source_tier IN ('BROKER', 'PRIMARY', 'WEB', 'PAID', 'SOCIAL', 'LEGACY')",
        )
        batch_op.create_index(
            "ix_research_items_provider_item",
            ["provider", "provider_item_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_research_items_tier_retrieved",
            ["source_tier", "retrieved_at"],
            unique=False,
        )

    # Remove migration-only defaults after existing rows have been populated.
    with op.batch_alter_table("research_items", schema=None) as batch_op:
        batch_op.alter_column(
            "source_tier",
            existing_type=sa.String(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "normalized_text",
            existing_type=sa.Text(),
            server_default=None,
            existing_nullable=False,
        )

    with op.batch_alter_table("agent_invocations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("evidence_manifest_hash", sa.String(), nullable=True))

    op.create_table(
        "research_item_symbols",
        sa.Column("research_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["research_id"],
            ["research_items.id"],
            name=op.f("fk_research_item_symbols_research_id_research_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "research_id",
            "symbol",
            name=op.f("pk_research_item_symbols"),
        ),
    )
    with op.batch_alter_table("research_item_symbols", schema=None) as batch_op:
        batch_op.create_index(
            "ix_research_item_symbols_symbol_research",
            ["symbol", "research_id"],
            unique=False,
        )

    op.create_table(
        "research_item_questions",
        sa.Column("research_id", sa.String(), nullable=False),
        sa.Column("question_id", sa.String(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["research_id"],
            ["research_items.id"],
            name=op.f("fk_research_item_questions_research_id_research_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "research_id",
            "question_id",
            name=op.f("pk_research_item_questions"),
        ),
    )
    with op.batch_alter_table("research_item_questions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_research_item_questions_question_research",
            ["question_id", "research_id"],
            unique=False,
        )

    op.create_table(
        "agent_invocation_evidence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_invocation_id", sa.String(), nullable=False),
        sa.Column("research_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_invocation_id"],
            ["agent_invocations.id"],
            name=op.f(
                "fk_agent_invocation_evidence_agent_invocation_id_agent_invocations"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["research_id"],
            ["research_items.id"],
            name=op.f("fk_agent_invocation_evidence_research_id_research_items"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_invocation_evidence")),
        sa.CheckConstraint(
            "ordinal >= 0",
            name=op.f("ck_agent_invocation_evidence_non_negative_ordinal"),
        ),
        sa.UniqueConstraint(
            "agent_invocation_id",
            "ordinal",
            name="uq_agent_invocation_evidence_invocation_ordinal",
        ),
        sa.UniqueConstraint(
            "agent_invocation_id",
            "research_id",
            name="uq_agent_invocation_evidence_invocation_research",
        ),
    )
    with op.batch_alter_table("agent_invocation_evidence", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_invocation_evidence_invocation_ordinal",
            ["agent_invocation_id", "ordinal"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_agent_invocation_evidence_research_id"),
            ["research_id"],
            unique=False,
        )


def downgrade() -> None:
    """Restore the original globally deduplicated research schema."""
    connection = op.get_bind()
    duplicate_hash = connection.execute(
        sa.text(
            "SELECT content_hash FROM research_items "
            "GROUP BY content_hash HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate_hash is not None:
        raise RuntimeError(
            "cannot downgrade research schema while the same content hash exists in multiple runs"
        )

    with op.batch_alter_table("agent_invocation_evidence", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_invocation_evidence_research_id"))
        batch_op.drop_index("ix_agent_invocation_evidence_invocation_ordinal")
    op.drop_table("agent_invocation_evidence")

    with op.batch_alter_table("research_item_questions", schema=None) as batch_op:
        batch_op.drop_index("ix_research_item_questions_question_research")
    op.drop_table("research_item_questions")

    with op.batch_alter_table("research_item_symbols", schema=None) as batch_op:
        batch_op.drop_index("ix_research_item_symbols_symbol_research")
    op.drop_table("research_item_symbols")

    with op.batch_alter_table("agent_invocations", schema=None) as batch_op:
        batch_op.drop_column("evidence_manifest_hash")

    with op.batch_alter_table("research_items", schema=None) as batch_op:
        batch_op.drop_index("ix_research_items_tier_retrieved")
        batch_op.drop_index("ix_research_items_provider_item")
        batch_op.drop_constraint(
            batch_op.f("ck_research_items_valid_source_tier"),
            type_="check",
        )
        batch_op.drop_constraint(
            batch_op.f("uq_research_items_run_id"),
            type_="unique",
        )
        batch_op.create_unique_constraint(
            batch_op.f("uq_research_items_content_hash"),
            ["content_hash"],
        )
        batch_op.drop_column("cost_usd")
        batch_op.alter_column(
            "raw_artifact_path",
            existing_type=sa.Text(),
            new_column_name="raw_content_path",
            existing_nullable=True,
        )
        batch_op.drop_column("normalized_text")
        batch_op.drop_column("provider_item_id")
        batch_op.drop_column("provider")
        batch_op.drop_column("source_tier")
