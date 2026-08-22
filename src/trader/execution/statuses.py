"""Canonical local order states derived from broker-specific statuses."""

CANONICAL_ORDER_STATUSES = frozenset(
    {
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "UNKNOWN",
    }
)

_STATUS_MAP = {
    "accepted": "ACCEPTED",
    "accepted_for_bidding": "ACCEPTED",
    "calculated": "ACCEPTED",
    "new": "ACCEPTED",
    "pending_cancel": "ACCEPTED",
    "pending_new": "ACCEPTED",
    "pending_replace": "ACCEPTED",
    "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED",
    "canceled": "CANCELED",
    "cancelled": "CANCELED",
    "done_for_day": "CANCELED",
    "replaced": "CANCELED",
    # A stopped order may still be executed; keep counting its exposure.
    "stopped": "ACCEPTED",
    "rejected": "REJECTED",
    "expired": "EXPIRED",
}


def canonical_order_status(status: str) -> str:
    """Map Alpaca and already-canonical states into the deliberately small local set."""
    normalized = status.strip().lower().replace("-", "_").replace(" ", "_")
    return _STATUS_MAP.get(normalized, "UNKNOWN")
