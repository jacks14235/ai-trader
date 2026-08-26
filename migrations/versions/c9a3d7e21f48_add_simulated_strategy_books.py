"""add simulated strategy books

Revision ID: c9a3d7e21f48
Revises: b7e5109c34aa
Create Date: 2026-08-22 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a3d7e21f48"
down_revision: str | None = "b7e5109c34aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add shadow books, their simulated fills, and the book dimension on existing records."""
    op.create_table(
        "books",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("strategy_document_path", sa.Text(), nullable=False),
        sa.Column("strategy_content_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("starting_cash", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('active','paused','retired')", name="book_status_valid"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_books_status"), "books", ["status"])
    op.create_index(
        op.f("ix_books_strategy_content_hash"),
        "books",
        ["strategy_content_hash"],
    )

    op.create_table(
        "simulated_fills",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("proposal_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("qty", sa.String(), nullable=False),
        sa.Column("price", sa.String(), nullable=False),
        sa.Column("commission", sa.String(), nullable=False, server_default="0"),
        sa.Column("quote_bid", sa.String(), nullable=True),
        sa.Column("quote_ask", sa.String(), nullable=True),
        sa.Column("quote_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assumptions_json", sa.Text(), nullable=False),
        sa.Column("transaction_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id", "proposal_id"),
    )
    op.create_index(op.f("ix_simulated_fills_book_id"), "simulated_fills", ["book_id"])
    op.create_index(op.f("ix_simulated_fills_run_id"), "simulated_fills", ["run_id"])
    op.create_index(op.f("ix_simulated_fills_proposal_id"), "simulated_fills", ["proposal_id"])
    op.create_index(op.f("ix_simulated_fills_symbol"), "simulated_fills", ["symbol"])
    op.create_index(
        "ix_simulated_fills_book_time",
        "simulated_fills",
        ["book_id", "transaction_time"],
    )

    with op.batch_alter_table("trade_proposals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("book_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_trade_proposals_book_id_books"),
            "books",
            ["book_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(op.f("ix_trade_proposals_book_id"), ["book_id"])

    # The live account previously owned one snapshot per run and period; books need their own.
    with op.batch_alter_table("performance_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("book_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_performance_snapshots_book_id_books"),
            "books",
            ["book_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(op.f("ix_performance_snapshots_book_id"), ["book_id"])
        # The project's naming convention keys a unique constraint on its first column, so the
        # widened constraint keeps the same name and only gains the book dimension.
        batch_op.drop_constraint("uq_performance_snapshots_run_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_performance_snapshots_run_id",
            ["run_id", "period", "book_id"],
        )

    # SQLite treats NULLs as distinct in UNIQUE, so two live snapshots of one run
    # would otherwise both be admitted. This partial index restores the live-line guard.
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_performance_snapshots_live_run_period "
            "ON performance_snapshots (run_id, period) WHERE book_id IS NULL"
        )
    )


def downgrade() -> None:
    """Remove books, refusing to discard simulated history that already exists."""
    connection = op.get_bind()
    recorded = connection.execute(sa.text("SELECT COUNT(*) FROM books")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"cannot downgrade while {recorded} simulated books hold their own history"
        )

    op.execute(sa.text("DROP INDEX IF EXISTS uq_performance_snapshots_live_run_period"))

    with op.batch_alter_table("performance_snapshots", schema=None) as batch_op:
        batch_op.drop_constraint("uq_performance_snapshots_run_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_performance_snapshots_run_id",
            ["run_id", "period"],
        )
        batch_op.drop_index(op.f("ix_performance_snapshots_book_id"))
        batch_op.drop_constraint(
            batch_op.f("fk_performance_snapshots_book_id_books"),
            type_="foreignkey",
        )
        batch_op.drop_column("book_id")

    with op.batch_alter_table("trade_proposals", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_trade_proposals_book_id"))
        batch_op.drop_constraint(
            batch_op.f("fk_trade_proposals_book_id_books"),
            type_="foreignkey",
        )
        batch_op.drop_column("book_id")

    op.drop_table("simulated_fills")
    op.drop_table("books")
