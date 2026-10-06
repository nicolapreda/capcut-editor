"""Stripe integration: customers, checkout, billing portal, webhook handling."""
from __future__ import annotations

from sqlmodel import Session

from .config import settings
from .models import User


def _stripe():
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def ensure_customer(user: User, session: Session) -> str:
    """Return the user's Stripe customer id, creating it on first need."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    stripe = _stripe()
    customer = stripe.Customer.create(email=user.email, metadata={"user_id": str(user.id)})
    user.stripe_customer_id = customer["id"]
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.stripe_customer_id


def create_checkout_session(user: User, session: Session) -> str:
    stripe = _stripe()
    customer_id = ensure_customer(user, session)
    cs = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=settings.CHECKOUT_SUCCESS_URL,
        cancel_url=settings.CHECKOUT_CANCEL_URL,
        allow_promotion_codes=True,
    )
    return cs["url"]


def create_portal_session(user: User, session: Session) -> str:
    stripe = _stripe()
    customer_id = ensure_customer(user, session)
    ps = stripe.billing_portal.Session.create(
        customer=customer_id, return_url=settings.PORTAL_RETURN_URL,
    )
    return ps["url"]


def verify_webhook(payload: bytes, sig_header: str):
    """Verify the Stripe signature and return the event, or raise ValueError."""
    stripe = _stripe()
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:  # stripe.error.SignatureVerificationError etc.
        raise ValueError(f"webhook non valido: {e}") from e


def _apply_subscription(user: User, sub: dict, session: Session) -> None:
    user.subscription_status = sub.get("status", "none")
    user.current_period_end = float(sub.get("current_period_end") or 0)
    session.add(user)
    session.commit()


def handle_event(event: dict, session: Session) -> None:
    """Update user subscription state from a Stripe webhook event."""
    from sqlmodel import select

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    # find the user by stripe customer id
    customer_id = obj.get("customer")
    user = None
    if customer_id:
        user = session.exec(select(User).where(User.stripe_customer_id == customer_id)).first()
    if not user:
        return

    if etype in ("customer.subscription.created", "customer.subscription.updated",
                 "customer.subscription.deleted"):
        _apply_subscription(user, obj, session)
    elif etype == "checkout.session.completed":
        # fetch the subscription to get status + period end
        sub_id = obj.get("subscription")
        if sub_id:
            stripe = _stripe()
            sub = stripe.Subscription.retrieve(sub_id)
            _apply_subscription(user, sub, session)
