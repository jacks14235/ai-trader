"""JSON operational logging and recursive secret redaction."""

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

SENSITIVE = {
    "authorization",
    "api_key",
    "api_secret",
    "alpaca_api_key",
    "alpaca_api_secret",
    "secret",
    "token",
}


def redact(value: object) -> object:
    """Return a recursively redacted, JSON-serializable copy of an audit value."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def audit(
    event: str,
    *,
    component: str = "trader",
    run_id: str | None = None,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit one structured operational audit event."""
    payload: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": logging.getLevelName(level),
        "component": component,
        "event": event,
        **fields,
    }
    if run_id is not None:
        payload["run_id"] = run_id
    logging.getLogger("trader").log(
        level,
        json.dumps(redact(payload), default=str, sort_keys=True),
    )
