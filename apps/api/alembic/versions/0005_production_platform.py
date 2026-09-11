"""production sources and refresh jobs"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_production_platform"
down_revision = "0004_workspaces"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("data_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False, server_default="http_csv"),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_data_sources_workspace_id", "data_sources", ["workspace_id"])
    op.create_index("ix_data_sources_dataset_id", "data_sources", ["dataset_id"])
    op.create_table("refresh_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="1440"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(40), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_refresh_jobs_workspace_id", "refresh_jobs", ["workspace_id"])
    op.create_index("ix_refresh_jobs_source_id", "refresh_jobs", ["source_id"])

def downgrade():
    op.drop_index("ix_refresh_jobs_source_id", table_name="refresh_jobs")
    op.drop_index("ix_refresh_jobs_workspace_id", table_name="refresh_jobs")
    op.drop_table("refresh_jobs")
    op.drop_index("ix_data_sources_dataset_id", table_name="data_sources")
    op.drop_index("ix_data_sources_workspace_id", table_name="data_sources")
    op.drop_table("data_sources")
