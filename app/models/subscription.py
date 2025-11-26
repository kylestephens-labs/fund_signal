"""SQLModel mapping for Stripe subscriptions."""
# ruff: noqa: UP017

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, DateTime, String
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Subscription(SQLModel, table=True):
    """Persisted Stripe subscription state."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        sa.Index("ix_subscriptions_email", "email"),
        sa.Index("ix_subscriptions_price_id", "price_id"),
    )

    subscription_id: str = Field(
        sa_column=Column(String(length=255), primary_key=True, nullable=False)
    )
    customer_id: str = Field(sa_column=Column(String(length=255), nullable=False))
    email: str = Field(sa_column=Column(String(length=255), nullable=False))
    price_id: str = Field(sa_column=Column(String(length=255), nullable=False))
    status: str = Field(sa_column=Column(String(length=64), nullable=False))
    trial_start: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    trial_end: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    current_period_end: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    cancel_at_period_end: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=sa.false())
    )
    default_payment_method: str | None = Field(
        default=None, sa_column=Column(String(length=255), nullable=True)
    )
    last_event_id: str | None = Field(
        default=None, sa_column=Column(String(length=255), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )


class ProcessedEvent(SQLModel, table=True):
    """Recorded Stripe webhook events for idempotency."""

    __tablename__ = "processed_events"

    event_id: str = Field(sa_column=Column(String(length=255), primary_key=True, nullable=False))
    subscription_id: str = Field(
        sa_column=Column(
            String(length=255),
            sa.ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    received_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
