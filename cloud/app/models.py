"""Database models."""
from __future__ import annotations

import time

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str

    stripe_customer_id: str | None = Field(default=None, index=True)
    # none | active | trialing | past_due | canceled
    subscription_status: str = Field(default="none")
    current_period_end: float = Field(default=0.0)   # epoch seconds

    created_at: float = Field(default_factory=lambda: time.time())

    def subscription_active(self) -> bool:
        if self.subscription_status not in ("active", "trialing"):
            return False
        # allow a small clock-skew grace
        return self.current_period_end == 0.0 or self.current_period_end > time.time() - 60
