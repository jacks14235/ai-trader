"""allow persisted, deterministic valuation evidence

Revision ID: 9f3c8a67b24e
Revises: b83e219fc641
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f3c8a67b24e"
down_revision: str | None = "b83e219fc641"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TIERS = "'BROKER', 'PRIMARY', 'WEB', 'PAID', 'SOCIAL', 'LEGACY'"
NEW_TIERS = "'BROKER', 'PRIMARY', 'DERIVED', 'WEB', 'PAID', 'SOCIAL', 'LEGACY'"


def upgrade() -> None:
    with op.batch_alter_table("research_items", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_research_items_valid_source_tier"), type_="check"
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_research_items_valid_source_tier"),
            f"source_tier IN ({NEW_TIERS})",
        )


def downgrade() -> None:
    derived_count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM research_items WHERE source_tier = 'DERIVED'")
    ).scalar_one()
    if derived_count:
        raise RuntimeError(
            f"cannot downgrade while {derived_count} derived research items exist"
        )
    with op.batch_alter_table("research_items", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_research_items_valid_source_tier"), type_="check"
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_research_items_valid_source_tier"),
            f"source_tier IN ({OLD_TIERS})",
        )
