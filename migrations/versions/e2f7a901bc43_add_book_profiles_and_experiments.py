"""Add simulated book profiles and immutable experiment/evaluation history.

Revision ID: e2f7a901bc43
Revises: c9a3d7e21f48
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f7a901bc43"
down_revision: str | None = "c9a3d7e21f48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("books") as batch:
        batch.add_column(
            sa.Column("process_profile", sa.String(), nullable=False, server_default="single_pass")
        )
        batch.add_column(sa.Column("operating_note_path", sa.Text(), nullable=True))

    op.create_table(
        "book_experiment_phases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("configuration_hash", sa.String(), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"]),
        sa.UniqueConstraint("book_id", "ordinal"),
        sa.CheckConstraint("ordinal > 0", name="positive_ordinal"),
    )
    op.create_index("ix_book_experiment_phases_book_id", "book_experiment_phases", ["book_id"])
    op.create_index(
        "ix_book_experiment_phases_configuration_hash",
        "book_experiment_phases",
        ["configuration_hash"],
    )
    op.create_table(
        "book_evaluations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("book_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("phase_id", sa.String(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="STARTED"),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("terminal_invocation_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["phase_id"], ["book_experiment_phases.id"]),
        sa.ForeignKeyConstraint(["terminal_invocation_id"], ["agent_invocations.id"]),
        sa.UniqueConstraint("book_id", "run_id"),
        sa.CheckConstraint("status IN ('STARTED', 'COMPLETED', 'FAILED')", name="valid_status"),
        sa.CheckConstraint(
            "(status = 'STARTED' AND terminal_invocation_id IS NULL AND error IS NULL) OR "
            "(status = 'COMPLETED' AND terminal_invocation_id IS NOT NULL AND error IS NULL) OR "
            "(status = 'FAILED' AND terminal_invocation_id IS NULL AND error IS NOT NULL)",
            name="consistent_outcome",
        ),
    )
    for column in ("book_id", "run_id", "phase_id"):
        op.create_index(f"ix_book_evaluations_{column}", "book_evaluations", [column])
    op.create_index("ix_book_evaluations_book_as_of", "book_evaluations", ["book_id", "as_of"])
    op.create_index(
        "uq_book_evaluations_active_book",
        "book_evaluations",
        ["book_id"],
        unique=True,
        sqlite_where=sa.text("status = 'STARTED'"),
        postgresql_where=sa.text("status = 'STARTED'"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    customized = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM books WHERE process_profile != 'single_pass' "
            "OR operating_note_path IS NOT NULL"
        )
    ).scalar_one()
    phases = connection.execute(sa.text("SELECT COUNT(*) FROM book_experiment_phases")).scalar_one()
    evaluations = connection.execute(sa.text("SELECT COUNT(*) FROM book_evaluations")).scalar_one()
    if customized or phases or evaluations:
        raise RuntimeError(
            "cannot downgrade while book profiles, operating notes, or experiment history exist"
        )
    op.drop_table("book_evaluations")
    op.drop_table("book_experiment_phases")
    with op.batch_alter_table("books") as batch:
        batch.drop_column("operating_note_path")
        batch.drop_column("process_profile")
