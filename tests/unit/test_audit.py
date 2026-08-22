import json
import logging

from trader.logging.audit import audit, redact


def test_redact_recurses_through_mappings_and_sequences() -> None:
    value = {
        "api_key": "top-secret",
        "nested": [{"token": "also-secret", "symbol": "SPY"}],
    }

    assert redact(value) == {
        "api_key": "[REDACTED]",
        "nested": [{"token": "[REDACTED]", "symbol": "SPY"}],
    }


def test_audit_emits_structured_json(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="trader"):
        audit(
            "proposal_rejected",
            component="risk_engine",
            run_id="run-1",
            proposal_id="proposal-1",
            authorization="Bearer secret",
        )

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "proposal_rejected"
    assert payload["component"] == "risk_engine"
    assert payload["run_id"] == "run-1"
    assert payload["proposal_id"] == "proposal-1"
    assert payload["authorization"] == "[REDACTED]"
    assert payload["level"] == "INFO"
    assert payload["timestamp"].endswith("+00:00")
