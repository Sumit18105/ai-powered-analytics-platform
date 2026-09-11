"""enterprise data quality and automation"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_enterprise_automation"
down_revision = "0008_dashboard_studio"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    op.create_table("quality_snapshots",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("dataset_id", uuid, sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("missing_percent", sa.Float(), nullable=False),
        sa.Column("duplicate_rows", sa.BigInteger(), nullable=False),
        sa.Column("schema", jsonb, nullable=False),
        sa.Column("checks", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_quality_snapshots_dataset_id", "quality_snapshots", ["dataset_id"])
    op.create_index("ix_quality_snapshots_created_at", "quality_snapshots", ["created_at"])
    op.create_table("monitoring_rules",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", uuid, sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("config", jsonb, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_status", sa.String(40)), sa.Column("last_message", sa.Text()),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for t, cols in [("monitoring_rules", ["workspace_id"]), ("monitoring_rules", ["dataset_id"]), ("monitoring_rules", ["created_by"])]:
        op.create_index(f"ix_{t}_{cols[0]}", t, cols)
    op.create_table("insights",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", uuid, sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False), sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False), sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", jsonb, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_insights_workspace_id", "insights", ["workspace_id"])
    op.create_index("ix_insights_dataset_id", "insights", ["dataset_id"])
    op.create_index("ix_insights_created_at", "insights", ["created_at"])
    op.create_table("automation_runs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_id", uuid, sa.ForeignKey("monitoring_rules.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(40), nullable=False), sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", jsonb, nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_automation_runs_workspace_id", "automation_runs", ["workspace_id"])
    op.create_index("ix_automation_runs_rule_id", "automation_runs", ["rule_id"])


def downgrade():
    op.drop_table("automation_runs")
    op.drop_table("insights")
    op.drop_table("monitoring_rules")
    op.drop_table("quality_snapshots")
