"""Deterministic decision-quality ledger.

This package owns the durable record of what the portfolio decided, why, and what happened
afterwards. It contains no model invocation and no broker access: every writer here derives its
rows from already-persisted proposals, risk decisions, and account snapshots.
"""
