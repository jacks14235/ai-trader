import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from trader.agent.runner import daily_run
from trader.broker.base import Broker
from trader.broker.models import (
    Account,
    BrokerFill,
    BrokerOrder,
    OrderQueryStatus,
    Position,
)
from trader.persistence.db import create_session_factory
from trader.persistence.models import MarketEvent, Run, RunEvent, ScheduledRun
from trader.persistence.repositories import claim_run
from trader.research.events import (
    BEA_RELEASE_DATES_URL,
    MAX_EVENT_FEED_BYTES,
    BeaEventProvider,
    EventProviderError,
    FileEventProvider,
)
from trader.scheduling.config import DynamicRunsConfig
from trader.scheduling.discovery import EventDiscoveryService
from trader.scheduling.service import Scheduler

BASE_TIME = datetime(2026, 8, 20, 12, tzinfo=UTC)
BEA_GDP = "Gross Domestic Product"


class EmptyPaperBroker:
    @property
    def is_paper(self) -> bool:
        return True

    @property
    def paper_options_level_is_provider_managed(self) -> bool:
        return False

    def get_account(self) -> Account:
        return Account(equity="2000", cash="2000", buying_power="2000")

    def get_positions(self) -> list[Position]:
        return []

    def get_open_orders(self) -> list[BrokerOrder]:
        return []

    def get_orders(
        self,
        *,
        status: OrderQueryStatus = "all",
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[BrokerOrder]:
        del status, after, until, limit
        return []

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        del client_order_id
        return None

    def get_fills(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BrokerFill]:
        del after, until
        return []


def test_file_provider_preserves_payload_and_filters_scope(tmp_path: Path) -> None:
    feed_path = tmp_path / "events.json"
    payload = _feed_payload(
        _event("valid", "SPY", BASE_TIME + timedelta(hours=2)),
        _event("low-confidence", "SPY", BASE_TIME + timedelta(hours=3), confidence=0.5),
        _event("outside-universe", "AAPL", BASE_TIME + timedelta(hours=2)),
        _event("outside-window", "SPY", BASE_TIME + timedelta(days=3)),
    )
    raw = json.dumps(payload, sort_keys=True).encode()
    feed_path.write_bytes(raw)

    batch = FileEventProvider(feed_path).discover(
        symbols=frozenset({"SPY"}),
        window_start=BASE_TIME,
        window_end=BASE_TIME + timedelta(days=2),
        retrieved_at=BASE_TIME,
    )

    assert [event.source_event_id for event in batch.events] == ["valid", "low-confidence"]
    assert batch.skipped_outside_universe == 1
    assert batch.skipped_outside_window == 1
    assert batch.raw_payload == raw
    assert batch.content_hash == hashlib.sha256(raw).hexdigest()


def test_file_provider_rejects_unknown_or_malformed_content(tmp_path: Path) -> None:
    feed_path = tmp_path / "events.json"
    payload = _feed_payload(_event("valid", "SPY", BASE_TIME + timedelta(hours=2)))
    payload["unexpected"] = True
    feed_path.write_text(json.dumps(payload))

    with pytest.raises(EventProviderError, match="invalid event feed"):
        FileEventProvider(feed_path).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(days=2),
        )


def test_bea_provider_normalizes_deduplicates_and_preserves_payload(tmp_path: Path) -> None:
    release_at = BASE_TIME + timedelta(hours=2)
    raw = _bea_payload(release_at, release_at, BASE_TIME + timedelta(days=4))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY", "QQQ"}),
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(days=2),
            retrieved_at=BASE_TIME,
        )

    assert len(requests) == 1
    assert str(requests[0].url) == BEA_RELEASE_DATES_URL
    assert requests[0].headers["accept"] == "application/json"
    assert batch.provider == "bea"
    assert batch.retrieval_mode == "network"
    assert batch.raw_payload == raw
    assert batch.content_hash == hashlib.sha256(raw).hexdigest()
    assert batch.skipped_duplicates == 1
    assert batch.skipped_outside_window == 1
    assert len(batch.events) == 1
    assert batch.events[0].symbols == ("QQQ", "SPY")
    assert batch.events[0].scheduled_at == release_at
    assert batch.events[0].source_event_id.endswith(":2026:001")
    assert (tmp_path / "bea.json").read_bytes() == raw


def test_bea_provider_retries_then_uses_recent_verified_cache(tmp_path: Path) -> None:
    raw = _bea_payload(BASE_TIME + timedelta(hours=2))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=raw))
    ) as client:
        _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(days=2),
            retrieved_at=BASE_TIME,
        )

    attempts = 0

    def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        batch = _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME + timedelta(hours=1),
            window_end=BASE_TIME + timedelta(days=2),
            retrieved_at=BASE_TIME + timedelta(hours=1),
        )

    assert attempts == 3
    assert batch.retrieval_mode == "cache"
    assert batch.raw_payload == raw
    assert len(batch.events) == 1


def test_bea_provider_retries_transient_http_status_then_recovers(tmp_path: Path) -> None:
    raw = _bea_payload(BASE_TIME + timedelta(hours=2))
    attempts = 0

    def recovering(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=raw)

    with httpx.Client(transport=httpx.MockTransport(recovering)) as client:
        batch = _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(days=2),
            retrieved_at=BASE_TIME,
        )

    assert attempts == 3
    assert batch.retrieval_mode == "network"


def test_bea_provider_rejects_malformed_or_unexpected_success_response(
    tmp_path: Path,
) -> None:
    raw = _bea_payload(BASE_TIME + timedelta(hours=2))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=raw))
    ) as client:
        _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(days=2),
            retrieved_at=BASE_TIME,
        )

    for response in (
        httpx.Response(200, content=b"{not-json"),
        httpx.Response(200, content=b"{}"),
        httpx.Response(200, content=b"x" * (MAX_EVENT_FEED_BYTES + 1)),
    ):
        with httpx.Client(
            transport=httpx.MockTransport(lambda _request, item=response: item)
        ) as client, pytest.raises(EventProviderError):
            _bea_provider(tmp_path, client).discover(
                symbols=frozenset({"SPY"}),
                window_start=BASE_TIME,
                window_end=BASE_TIME + timedelta(days=2),
                retrieved_at=BASE_TIME + timedelta(minutes=1),
            )


def test_bea_provider_does_not_retry_4xx_or_use_cache(tmp_path: Path) -> None:
    attempts = 0

    def forbidden(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403)

    with httpx.Client(
        transport=httpx.MockTransport(forbidden)
    ) as client, pytest.raises(EventProviderError, match="HTTP 403"):
        _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(days=2),
            retrieved_at=BASE_TIME,
        )

    assert attempts == 1


def test_bea_provider_rejects_stale_cache_after_outage(tmp_path: Path) -> None:
    raw = _bea_payload(BASE_TIME + timedelta(days=2))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=raw))
    ) as client:
        _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(days=3),
            retrieved_at=BASE_TIME,
        )

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(
        transport=httpx.MockTransport(unavailable)
    ) as client, pytest.raises(EventProviderError, match="cache is stale"):
        _bea_provider(tmp_path, client).discover(
            symbols=frozenset({"SPY"}),
            window_start=BASE_TIME + timedelta(hours=25),
            window_end=BASE_TIME + timedelta(days=4),
            retrieved_at=BASE_TIME + timedelta(hours=25),
        )


def test_authoritative_reschedule_cancels_old_pending_wakeup(tmp_path: Path) -> None:
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.sqlite")()
    config = _config(tmp_path / "unused.json")
    scheduler = Scheduler(session, config, frozenset({"SPY"}))
    first_parent = claim_run(session, "daily:bea:first", BASE_TIME, "hash")
    second_parent = claim_run(session, "daily:bea:second", BASE_TIME, "hash")
    assert first_parent is not None
    assert second_parent is not None

    first_release = BASE_TIME + timedelta(hours=2)
    second_release = first_release + timedelta(hours=1)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=_bea_payload(first_release))
        )
    ) as client:
        first = EventDiscoveryService(
            _bea_provider(tmp_path, client),
            scheduler,
            config.discovery,
            frozenset({"SPY"}),
        ).discover_and_schedule(first_parent.id, now=BASE_TIME)

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=_bea_payload(second_release))
        )
    ) as client:
        second = EventDiscoveryService(
            _bea_provider(tmp_path, client),
            scheduler,
            config.discovery,
            frozenset({"SPY"}),
        ).discover_and_schedule(second_parent.id, now=BASE_TIME + timedelta(minutes=1))

    assert first.summary.registered_event_ids == second.summary.registered_event_ids
    assert second.summary.rescheduled_event_ids == first.summary.registered_event_ids
    assert second.summary.cancelled_scheduled_run_ids == first.summary.scheduled_run_ids
    records = list(session.scalars(select(ScheduledRun).order_by(ScheduledRun.scheduled_for)))
    assert [record.status for record in records] == ["CANCELLED", "PENDING"]
    assert records[1].scheduled_for.replace(tzinfo=UTC) == second_release + timedelta(
        minutes=10
    )


def test_discovery_schedules_once_across_multiple_daily_observations(tmp_path: Path) -> None:
    feed_path = tmp_path / "events.json"
    feed_path.write_text(
        json.dumps(
            _feed_payload(
                _event("earnings", "SPY", BASE_TIME + timedelta(hours=2)),
                _event("uncertain", "SPY", BASE_TIME + timedelta(hours=3), confidence=0.5),
            )
        )
    )
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.sqlite")()
    config = _config(feed_path)
    scheduler = Scheduler(session, config, frozenset({"SPY"}))
    discovery = EventDiscoveryService(
        FileEventProvider(feed_path),
        scheduler,
        config.discovery,
        frozenset({"SPY"}),
    )
    first_parent = claim_run(session, "daily:first", BASE_TIME, "hash")
    second_parent = claim_run(session, "daily:second", BASE_TIME, "hash")
    assert first_parent is not None
    assert second_parent is not None

    first = discovery.discover_and_schedule(first_parent.id, now=BASE_TIME)
    second = discovery.discover_and_schedule(second_parent.id, now=BASE_TIME)

    assert first.summary.candidate_count == 2
    assert first.summary.skipped_low_confidence == 1
    assert first.summary.registered_event_ids == second.summary.registered_event_ids
    assert first.summary.scheduled_run_ids == second.summary.scheduled_run_ids
    assert len(session.scalars(select(MarketEvent)).all()) == 1
    assert len(session.scalars(select(ScheduledRun)).all()) == 1
    scheduled = session.scalar(select(ScheduledRun))
    assert scheduled is not None
    assert json.loads(scheduled.payload_json)["shadow_mode"] is True


def test_daily_run_captures_event_feed_and_shadow_schedule(tmp_path: Path) -> None:
    feed_path = tmp_path / "events.json"
    feed_path.write_text(
        json.dumps(_feed_payload(_event("earnings", "SPY", BASE_TIME + timedelta(hours=2))))
    )
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.sqlite")()
    config = _config(feed_path)
    discovery = EventDiscoveryService(
        FileEventProvider(feed_path),
        Scheduler(session, config, frozenset({"SPY"})),
        config.discovery,
        frozenset({"SPY"}),
    )

    run_id = daily_run(
        session,
        cast(Broker, EmptyPaperBroker()),
        tmp_path / "raw",
        b"mode: paper\n",
        BASE_TIME,
        universe_config_bytes=b"allowed_symbols: [SPY]\nbenchmark_symbols: [SPY]\n",
        dynamic_runs_config_bytes=b"shadow: true\n",
        event_discovery=discovery,
    )

    scheduled = session.scalar(select(ScheduledRun))
    assert scheduled is not None
    assert scheduled.created_by_run_id == run_id
    assert scheduled.status == "PENDING"
    assert scheduled.scheduled_for.replace(tzinfo=UTC) == BASE_TIME + timedelta(
        hours=2, minutes=10
    )
    directory = tmp_path / "raw" / "paper" / "runs" / run_id
    summary = json.loads((directory / "event_discovery.json").read_text())
    assert summary["registered_event_ids"] == [scheduled.market_event_id]
    assert summary["scheduled_run_ids"] == [scheduled.id]
    assert (directory / "event_feed.json").read_bytes() == feed_path.read_bytes()
    assert "Event discovery (shadow mode)" in (directory / "daily_report.md").read_text()
    manifest = json.loads((directory / "manifest.json").read_text())
    assert {"event_feed.json", "event_discovery.json"}.issubset(manifest)
    stage = session.scalar(
        select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.stage == "DISCOVER_MARKET_EVENTS",
        )
    )
    assert stage is not None
    assert scheduled.id in (stage.metadata_json or "")
    run = session.get(Run, run_id)
    assert run is not None
    assert run.status == "COMPLETED"


def test_discovery_records_spacing_rejection_without_failing_run(tmp_path: Path) -> None:
    feed_path = tmp_path / "events.json"
    feed_path.write_text(
        json.dumps(
            _feed_payload(
                _event("first", "SPY", BASE_TIME + timedelta(hours=2)),
                _event("too-close", "SPY", BASE_TIME + timedelta(hours=2, minutes=10)),
            )
        )
    )
    session = create_session_factory(f"sqlite:///{tmp_path}/trader.sqlite")()
    config = _config(feed_path)
    discovery = EventDiscoveryService(
        FileEventProvider(feed_path),
        Scheduler(session, config, frozenset({"SPY"})),
        config.discovery,
        frozenset({"SPY"}),
    )
    parent = claim_run(session, "daily:spacing", BASE_TIME, "hash")
    assert parent is not None

    outcome = discovery.discover_and_schedule(parent.id, now=BASE_TIME)

    assert len(outcome.summary.registered_event_ids) == 2
    assert len(outcome.summary.scheduled_run_ids) == 1
    assert len(outcome.summary.rejected) == 1
    assert "minimum spacing" in outcome.summary.rejected[0].reason
    assert len(session.scalars(select(MarketEvent)).all()) == 2
    assert len(session.scalars(select(ScheduledRun)).all()) == 1


def test_config_requires_offsets_for_every_allowed_discovery_type(tmp_path: Path) -> None:
    content = _config_dict(tmp_path / "events.json")
    discovery = cast(dict[str, object], content["discovery"])
    offsets = cast(dict[str, int], discovery["followup_offsets_minutes"])
    del offsets["FDA_DECISION"]

    with pytest.raises(ValidationError, match="missing discovery follow-up offsets"):
        DynamicRunsConfig.model_validate(content)


def test_bea_config_rejects_unsafe_or_ambiguous_source_settings(tmp_path: Path) -> None:
    content = _config_dict(tmp_path / "events.json")
    discovery = cast(dict[str, object], content["discovery"])
    discovery["source"] = {
        "provider": "bea",
        "cache_path": str(tmp_path / "bea.json"),
        "included_release_names": [BEA_GDP],
        "timeout_seconds": 10,
        "max_cache_age_minutes": 60,
        "max_fetch_attempts": 3,
    }
    parsed = DynamicRunsConfig.model_validate(content)
    assert parsed.discovery.source.provider == "bea"

    source = cast(dict[str, object], discovery["source"])
    source["max_fetch_attempts"] = 4
    with pytest.raises(ValidationError, match="between 1 and 3"):
        DynamicRunsConfig.model_validate(content)

    source["max_fetch_attempts"] = 3
    source["url"] = "https://attacker.invalid/events.json"
    with pytest.raises(ValidationError, match="Extra inputs"):
        DynamicRunsConfig.model_validate(content)


def _config(feed_path: Path) -> DynamicRunsConfig:
    return DynamicRunsConfig.model_validate(_config_dict(feed_path))


def _config_dict(feed_path: Path) -> dict[str, object]:
    return {
        "enabled": True,
        "max_extra_runs_per_day": 3,
        "max_extra_runs_per_symbol_per_day": 2,
        "minimum_spacing_minutes": 30,
        "max_schedule_horizon_days": 30,
        "max_event_followup_delay_minutes": 360,
        "max_lateness_minutes": 30,
        "lease_minutes": 10,
        "max_attempts": 2,
        "max_claims_per_tick": 3,
        "allowed_event_types": [
            "EARNINGS_RELEASE",
            "EARNINGS_CALL",
            "FDA_DECISION",
            "INVESTOR_DAY",
            "ECONOMIC_RELEASE",
        ],
        "discovery": {
            "enabled": True,
            "source": {
                "provider": "file",
                "feed_path": str(feed_path),
            },
            "lookahead_days": 2,
            "minimum_confidence": 0.8,
            "followup_offsets_minutes": {
                "EARNINGS_RELEASE": 10,
                "EARNINGS_CALL": 15,
                "FDA_DECISION": 10,
                "INVESTOR_DAY": 15,
                "ECONOMIC_RELEASE": 10,
            },
        },
    }


def _feed_payload(*events: dict[str, object]) -> dict[str, object]:
    return {"source": "test-earnings-feed", "events": list(events)}


def _event(
    source_event_id: str,
    symbol: str,
    scheduled_at: datetime,
    *,
    confidence: float = 0.95,
) -> dict[str, object]:
    return {
        "event_type": "EARNINGS_RELEASE",
        "symbols": [symbol],
        "scheduled_at": scheduled_at.isoformat(),
        "source_event_id": source_event_id,
        "confidence": confidence,
        "evidence": {"url": f"https://example.test/{source_event_id}"},
        "announced_at": (BASE_TIME - timedelta(hours=1)).isoformat(),
    }


def _bea_provider(tmp_path: Path, client: httpx.Client) -> BeaEventProvider:
    return BeaEventProvider(
        cache_path=tmp_path / "bea.json",
        included_release_names=(BEA_GDP,),
        timeout_seconds=5,
        max_cache_age_minutes=1440,
        max_fetch_attempts=3,
        client=client,
        sleep=lambda _seconds: None,
    )


def _bea_payload(*release_dates: datetime) -> bytes:
    return json.dumps(
        {
            BEA_GDP: {
                "release_dates": [release_at.isoformat() for release_at in release_dates]
            },
            "file_last_updated": "2026-08-20T08:00:00",
        },
        sort_keys=True,
    ).encode()
