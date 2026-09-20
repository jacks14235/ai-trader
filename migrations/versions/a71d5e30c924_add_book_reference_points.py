"""add deterministic book reference points

Revision ID: a71d5e30c924
Revises: f4c8a12d9e70
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a71d5e30c924"
down_revision: str | None = "f4c8a12d9e70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "book_reference_points",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("evaluation_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("starting_cash", sa.String(), nullable=False),
        sa.Column("equity", sa.String(), nullable=True),
        sa.Column("cash", sa.String(), nullable=True),
        sa.Column("quantity", sa.String(), nullable=True),
        sa.Column("entry_price", sa.String(), nullable=True),
        sa.Column("mark_price", sa.String(), nullable=True),
        sa.Column("commission", sa.String(), nullable=True),
        sa.Column("quote_bid", sa.String(), nullable=True),
        sa.Column("quote_ask", sa.String(), nullable=True),
        sa.Column("quote_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("definition_hash", sa.String(), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('CASH', 'SPY_BUY_HOLD')",
            name=op.f("ck_book_reference_points_valid_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'FAILED')",
            name=op.f("ck_book_reference_points_valid_status"),
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED' AND equity IS NOT NULL AND cash IS NOT NULL "
            "AND error IS NULL) OR "
            "(status = 'FAILED' AND equity IS NULL AND cash IS NULL AND error IS NOT NULL)",
            name=op.f("ck_book_reference_points_consistent_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["book_id"], ["books.id"], name=op.f("fk_book_reference_points_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_book_reference_points_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["book_evaluations.id"],
            name=op.f("fk_book_reference_points_evaluation_id_book_evaluations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book_reference_points")),
        sa.UniqueConstraint(
            "book_id", "run_id", "kind", name=op.f("uq_book_reference_points_book_id")
        ),
    )
    op.create_index(
        op.f("ix_book_reference_points_book_id"), "book_reference_points", ["book_id"]
    )
    op.create_index(
        op.f("ix_book_reference_points_run_id"), "book_reference_points", ["run_id"]
    )
    op.create_index(
        op.f("ix_book_reference_points_evaluation_id"),
        "book_reference_points",
        ["evaluation_id"],
    )
    op.create_index(
        op.f("ix_book_reference_points_definition_hash"),
        "book_reference_points",
        ["definition_hash"],
    )
    op.create_index(
        "ix_book_reference_points_book_as_of",
        "book_reference_points",
        ["book_id", "as_of"],
    )


def downgrade() -> None:
    recorded = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM book_reference_points")
    ).scalar_one()
    if recorded:
        raise RuntimeError(f"cannot downgrade while {recorded} book reference points exist")
    op.drop_table("book_reference_points")
