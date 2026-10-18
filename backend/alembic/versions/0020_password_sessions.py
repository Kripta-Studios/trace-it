"""Password authentication and revocable browser sessions for private deployments."""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = depends_on = None


def upgrade():
    op.add_column("users", sa.Column("password_hash", sa.String(), nullable=True))
    op.add_column(
        "users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade():
    op.drop_column("users", "session_version")
    op.drop_column("users", "password_hash")
