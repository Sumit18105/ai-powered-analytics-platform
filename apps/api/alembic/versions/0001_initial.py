from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "datasets" not in tables:
        op.create_table(
            "datasets",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("storage_path", sa.Text(), nullable=False),
            sa.Column("file_type", sa.String(32), nullable=False),
            sa.Column("file_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("column_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("profile", postgresql.JSONB(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    else:
        columns = {column["name"] for column in inspector.get_columns("datasets")}
        if "file_size_bytes" not in columns:
            op.add_column("datasets", sa.Column("file_size_bytes", sa.BigInteger(), nullable=False, server_default="0"))

    if "dataset_versions" not in tables:
        op.create_table(
            "dataset_versions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("dataset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(255), nullable=False),
            sa.Column("storage_path", sa.Text(), nullable=False),
            sa.Column("file_type", sa.String(32), nullable=False),
            sa.Column("file_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("column_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("profile", postgresql.JSONB(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_dataset_versions_dataset_id", "dataset_versions", ["dataset_id"])

    if "transformations" not in tables:
        op.create_table(
            "transformations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("dataset_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dataset_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("operation", postgresql.JSONB(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_transformations_dataset_version_id", "transformations", ["dataset_version_id"])

    # Backfill v1 for datasets already present before versioning was introduced.
    if "dataset_versions" in sa.inspect(bind).get_table_names():
        op.execute(sa.text("""
            INSERT INTO dataset_versions
                (id, dataset_id, version, label, storage_path, file_type, file_size_bytes, row_count, column_count, profile)
            SELECT gen_random_uuid(), d.id, 1, 'Original', d.storage_path, d.file_type,
                   d.file_size_bytes, d.row_count, d.column_count, d.profile
            FROM datasets d
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_versions v WHERE v.dataset_id = d.id
            )
        """))


def downgrade() -> None:
    op.drop_index("ix_transformations_dataset_version_id", table_name="transformations")
    op.drop_table("transformations")
    op.drop_index("ix_dataset_versions_dataset_id", table_name="dataset_versions")
    op.drop_table("dataset_versions")
