from datetime import UTC, datetime
from decimal import Decimal

from trader.research.fundamentals import build_valuation_record, trailing_value


def _concept(values: list[dict[str, object]], unit: str = "USD") -> dict[str, object]:
    return {"units": {unit: values}}


def _payload() -> dict[str, object]:
    quarters = []
    for start, end, filed, value in (
        ("2024-01-01", "2024-03-31", "2024-04-20", 100),
        ("2024-04-01", "2024-06-30", "2024-07-20", 110),
        ("2024-07-01", "2024-09-30", "2024-10-20", 120),
        ("2024-01-01", "2024-09-30", "2024-10-20", 330),
        ("2024-01-01", "2024-12-31", "2025-02-15", 460),
        # Restatement for the same period must win once filed.
        ("2024-10-01", "2024-12-31", "2025-03-01", 131),
    ):
        quarters.append({"start": start, "end": end, "filed": filed, "form": "10-Q", "val": value})
    return {
        "facts": {
            "us-gaap": {
                "Revenues": _concept(quarters),
                "EarningsPerShareDiluted": _concept(
                    [
                        {
                            "start": "2024-01-01",
                            "end": "2024-03-31",
                            "filed": "2024-04-20",
                            "form": "10-Q",
                            "val": "-0.1",
                        },
                        {
                            "start": "2024-04-01",
                            "end": "2024-06-30",
                            "filed": "2024-07-20",
                            "form": "10-Q",
                            "val": "0.2",
                        },
                        {
                            "start": "2024-07-01",
                            "end": "2024-09-30",
                            "filed": "2024-10-20",
                            "form": "10-Q",
                            "val": "0.3",
                        },
                        {
                            "start": "2024-10-01",
                            "end": "2024-12-31",
                            "filed": "2025-03-01",
                            "form": "10-K",
                            "val": "0.4",
                        },
                    ],
                    "USD/shares",
                ),
                "CashAndCashEquivalentsAtCarryingValue": _concept(
                    [{"end": "2024-12-31", "filed": "2025-02-15", "form": "10-K", "val": 20}]
                ),
                "LongTermDebt": _concept(
                    [{"end": "2024-12-31", "filed": "2025-02-15", "form": "10-K", "val": 10}]
                ),
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": _concept(
                    [{"end": "2025-02-01", "filed": "2025-02-15", "form": "10-K", "val": 10}],
                    "shares",
                )
            },
        }
    }


def test_ttm_derives_q4_and_prefers_latest_restatement() -> None:
    value, concept, reason = trailing_value(
        _payload(), "revenue", cutoff=datetime(2025, 3, 2, tzinfo=UTC).date()
    )
    assert value == Decimal("461")
    assert concept == "us-gaap:Revenues"
    assert reason == "AVAILABLE"


def test_facts_are_causal_at_historical_and_current_cutoffs() -> None:
    historical, _, reason = trailing_value(
        _payload(), "revenue", cutoff=datetime(2024, 12, 31, tzinfo=UTC).date()
    )
    assert historical is None
    assert reason == "INSUFFICIENT_CONSECUTIVE_QUARTERS"
    before_restatement, _, _ = trailing_value(
        _payload(), "revenue", cutoff=datetime(2025, 2, 20, tzinfo=UTC).date()
    )
    assert before_restatement == Decimal("460")


def test_valuation_record_is_decimal_serializable_with_provenance() -> None:
    record = build_valuation_record(
        symbol="INTC",
        companyfacts=_payload(),
        current_price=Decimal("25"),
        as_of=datetime(2025, 3, 2, tzinfo=UTC),
        monthly_bars=({"c": "20"}, {"c": "30"}),
        input_provenance=({"evidence_id": "a" * 64, "content_hash": "b" * 64},),
    )
    assert record["current_metrics"]["market_cap"]["value"] == "250"
    assert record["current_metrics"]["price_to_earnings"]["value"] == "31.25"
    assert record["historical"]["price"]["current_percentile"] == "0.5"
    assert record["input_provenance"][0]["evidence_id"] == "a" * 64
