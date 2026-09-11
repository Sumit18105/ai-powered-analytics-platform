"""dashboard studio metadata support"""
from alembic import op
import sqlalchemy as sa

revision = "0008_dashboard_studio"
down_revision = "0007_integrity_constraints"
branch_labels = None
depends_on = None


def upgrade():
    # Keep dashboard widgets/config in JSONB so the studio can evolve without
    # creating a migration for every new widget type.
    op.add_column("dashboards", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    op.drop_column("dashboards", "version")
