"""Deterministic risk authorization.

Import ``trader.risk.runtime`` directly. Re-exporting the runtime here would make importing
``trader.risk.models`` pull in the executor, which imports the persistence layer that defines
``RiskDecision``'s writer — a cycle that breaks any module importing persistence first.
"""
