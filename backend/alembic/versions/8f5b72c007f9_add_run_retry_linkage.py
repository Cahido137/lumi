"""add run retry linkage

Revision ID: 8f5b72c007f9
Revises: ea474dc85816
Create Date: 2026-10-08 11:00:58.761867

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f5b72c007f9"
down_revision: str | Sequence[str] | None = "ea474dc85816"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 历史数据无需回填: 旧运行没有重试链信息, 保留为空即"首次执行"的显式表达
    op.add_column(
        "runs",
        sa.Column(
            "retry_of_run_id",
            sa.Uuid(as_uuid=False),
            nullable=True,
            comment="用户主动重试时关联的源运行ID, 首次执行为空",
        ),
    )
    op.create_index(op.f("ix_runs_retry_of_run_id"), "runs", ["retry_of_run_id"], unique=False)
    op.create_foreign_key("runs_retry_of_run_id_fkey", "runs", "runs", ["retry_of_run_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("runs_retry_of_run_id_fkey", "runs", type_="foreignkey")
    op.drop_index(op.f("ix_runs_retry_of_run_id"), table_name="runs")
    op.drop_column("runs", "retry_of_run_id")
