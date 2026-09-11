"""add integrity constraints for memberships and dataset versions"""
from alembic import op

revision = "0007_integrity_constraints"
down_revision = "0006_composer_connections"
branch_labels = None
depends_on = None

def upgrade():
    op.create_unique_constraint("uq_workspace_member_user", "workspace_members", ["workspace_id", "user_id"])
    op.create_unique_constraint("uq_dataset_version", "dataset_versions", ["dataset_id", "version"])

def downgrade():
    op.drop_constraint("uq_dataset_version", "dataset_versions", type_="unique")
    op.drop_constraint("uq_workspace_member_user", "workspace_members", type_="unique")
