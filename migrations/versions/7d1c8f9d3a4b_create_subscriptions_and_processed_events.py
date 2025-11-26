"""Create subscriptions and processed_events tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "7d1c8f9d3a4b"
down_revision = "38e7d2a56681"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("subscription_id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("customer_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("price_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("trial_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_payment_method", sa.String(length=255), nullable=True),
        sa.Column("last_event_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_subscriptions_email", "subscriptions", ["email"])
    op.create_index("ix_subscriptions_price_id", "subscriptions", ["price_id"])

    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column(
            "subscription_id",
            sa.String(length=255),
            sa.ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_index("ix_subscriptions_price_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_email", table_name="subscriptions")
    op.drop_table("subscriptions")
