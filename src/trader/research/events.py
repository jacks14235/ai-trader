"""Vendor-neutral market-event discovery providers."""

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from trader.scheduling.models import EventType, MarketEventRequest

MAX_EVENT_FEED_BYTES = 1_000_000
BEA_RELEASE_DATES_URL = "https://apps.bea.gov/API/signup/release_dates.json"
BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"
BEA_SOURCE = "bea-release-calendar"
_CACHE_CLOCK_SKEW = timedelta(minutes=5)


class EventProviderError(RuntimeError):
    """Raised when a configured event source cannot be read or validated."""


class _TransientProviderError(EventProviderError):
    """A network failure for which a recent verified cache may be used."""


@dataclass(frozen=True)
class EventBatch:
    """Exact provider payload plus the validated in-scope event candidates."""

    provider: str
    source_reference: str
    retrieved_at: datetime
    content_hash: str
    raw_payload: bytes
    events: tuple[MarketEventRequest, ...]
    skipped_outside_window: int
    skipped_outside_universe: int
    retrieval_mode: Literal["file", "network", "cache"] = "file"
    source_updated_at: str | None = None
    skipped_duplicates: int = 0


class EventProvider(Protocol):
    """Narrow discovery boundary; providers have no database or broker access."""

    def discover(
        self,
        *,
        symbols: frozenset[str],
        window_start: datetime,
        window_end: datetime,
        retrieved_at: datetime | None = None,
    ) -> EventBatch: ...


class FileEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: EventType
    symbols: tuple[str, ...]
    scheduled_at: datetime
    source_event_id: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: dict[str, object]
    announced_at: datetime

    @field_validator("source_event_id")
    @classmethod
    def nonblank_identifier(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_event_id cannot be blank")
        return stripped


class FileEventFeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=100)
    events: tuple[FileEventItem, ...] = Field(max_length=500)

    @field_validator("source")
    @classmethod
    def nonblank_source(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("feed source cannot be blank")
        return stripped

    @model_validator(mode="after")
    def unique_source_event_ids(self) -> "FileEventFeed":
        identifiers = [event.source_event_id for event in self.events]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("event feed contains duplicate source_event_id values")
        return self


class FileEventProvider:
    """Read a strict JSON feed for manual and fixture-backed shadow testing."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def discover(
        self,
        *,
        symbols: frozenset[str],
        window_start: datetime,
        window_end: datetime,
        retrieved_at: datetime | None = None,
    ) -> EventBatch:
        start, end, retrieved = _validate_discovery_inputs(
            symbols,
            window_start,
            window_end,
            retrieved_at,
        )
        try:
            raw_payload = self.path.read_bytes()
        except OSError as exc:
            raise EventProviderError(f"cannot read event feed {self.path}: {exc}") from exc
        _validate_size(raw_payload, str(self.path))
        try:
            feed = FileEventFeed.model_validate_json(raw_payload)
        except (ValidationError, ValueError) as exc:
            raise EventProviderError(f"invalid event feed {self.path}: {exc}") from exc

        events: list[MarketEventRequest] = []
        skipped_outside_window = 0
        skipped_outside_universe = 0
        for item in feed.events:
            request = MarketEventRequest(
                event_type=item.event_type,
                symbols=item.symbols,
                scheduled_at=item.scheduled_at,
                source=feed.source,
                source_event_id=item.source_event_id,
                confidence=item.confidence,
                evidence=item.evidence,
                announced_at=item.announced_at,
                raw=item.model_dump(mode="json"),
            )
            scheduled_at = request.scheduled_at.astimezone(UTC)
            if not start <= scheduled_at < end:
                skipped_outside_window += 1
                continue
            if not set(request.symbols).issubset(symbols):
                skipped_outside_universe += 1
                continue
            events.append(request)

        return EventBatch(
            provider="file",
            source_reference=str(self.path),
            retrieved_at=retrieved,
            content_hash=_content_hash(raw_payload),
            raw_payload=raw_payload,
            events=tuple(events),
            skipped_outside_window=skipped_outside_window,
            skipped_outside_universe=skipped_outside_universe,
            retrieval_mode="file",
        )


class _CacheMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_reference: Literal["https://apps.bea.gov/API/signup/release_dates.json"]
    retrieved_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("retrieved_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cache timestamp must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(frozen=True)
class _BeaParsedFeed:
    source_updated_at: str
    releases: dict[str, tuple[datetime, ...]]
    skipped_duplicates: int


class BeaEventProvider:
    """Fetch BEA's official release calendar with a bounded verified cache."""

    def __init__(
        self,
        *,
        cache_path: Path,
        included_release_names: tuple[str, ...],
        timeout_seconds: int,
        max_cache_age_minutes: int,
        max_fetch_attempts: int,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_path = cache_path
        self.included_release_names = included_release_names
        self.timeout_seconds = timeout_seconds
        self.max_cache_age = timedelta(minutes=max_cache_age_minutes)
        self.max_fetch_attempts = max_fetch_attempts
        self.client = client
        self.sleep = sleep

    def discover(
        self,
        *,
        symbols: frozenset[str],
        window_start: datetime,
        window_end: datetime,
        retrieved_at: datetime | None = None,
    ) -> EventBatch:
        start, end, retrieved = _validate_discovery_inputs(
            symbols,
            window_start,
            window_end,
            retrieved_at,
        )
        try:
            raw_payload = self._fetch_network()
            parsed = self._parse(raw_payload)
            self._write_cache(raw_payload, retrieved)
            retrieval_mode: Literal["network", "cache"] = "network"
        except _TransientProviderError as network_error:
            try:
                raw_payload = self._read_cache(retrieved)
                parsed = self._parse(raw_payload)
            except EventProviderError as cache_error:
                raise EventProviderError(
                    f"BEA calendar unavailable ({network_error}); cache unusable ({cache_error})"
                ) from network_error
            retrieval_mode = "cache"

        events: list[MarketEventRequest] = []
        skipped_outside_window = 0
        normalized_symbols = tuple(sorted(symbols))
        for release_name in self.included_release_names:
            by_year: dict[int, list[datetime]] = {}
            for release_at in parsed.releases[release_name]:
                by_year.setdefault(release_at.year, []).append(release_at)
            for year in sorted(by_year):
                for ordinal, release_at in enumerate(sorted(by_year[year]), start=1):
                    if not start <= release_at < end:
                        skipped_outside_window += 1
                        continue
                    source_event_id = _bea_source_event_id(release_name, year, ordinal)
                    events.append(
                        MarketEventRequest(
                            event_type="ECONOMIC_RELEASE",
                            symbols=normalized_symbols,
                            scheduled_at=release_at,
                            source=BEA_SOURCE,
                            source_event_id=source_event_id,
                            confidence=1.0,
                            evidence={
                                "release_name": release_name,
                                "schedule_url": BEA_SCHEDULE_URL,
                                "machine_readable_url": BEA_RELEASE_DATES_URL,
                                "source_file_last_updated": parsed.source_updated_at,
                            },
                            announced_at=retrieved,
                            raw={
                                "release_name": release_name,
                                "release_at": release_at.isoformat(),
                                "source_file_last_updated": parsed.source_updated_at,
                            },
                        )
                    )

        events.sort(key=lambda item: (item.scheduled_at, item.source_event_id))
        return EventBatch(
            provider="bea",
            source_reference=BEA_RELEASE_DATES_URL,
            retrieved_at=retrieved,
            content_hash=_content_hash(raw_payload),
            raw_payload=raw_payload,
            events=tuple(events),
            skipped_outside_window=skipped_outside_window,
            skipped_outside_universe=0,
            retrieval_mode=retrieval_mode,
            source_updated_at=parsed.source_updated_at,
            skipped_duplicates=parsed.skipped_duplicates,
        )

    def _fetch_network(self) -> bytes:
        last_error: str | None = None
        for attempt in range(1, self.max_fetch_attempts + 1):
            try:
                if self.client is not None:
                    return self._request(self.client)
                with httpx.Client(follow_redirects=False) as client:
                    return self._request(client)
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
            except _TransientProviderError as exc:
                last_error = str(exc)
            if attempt == self.max_fetch_attempts:
                break
            self.sleep(0.5 * (2 ** (attempt - 1)))
        raise _TransientProviderError(
            f"BEA fetch failed after {self.max_fetch_attempts} attempt(s): {last_error}"
        )

    def _request(self, client: httpx.Client) -> bytes:
        with client.stream(
            "GET",
            BEA_RELEASE_DATES_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "ai-trader/0.1 paper-event-discovery",
            },
            timeout=self.timeout_seconds,
        ) as response:
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise _TransientProviderError(f"BEA returned HTTP {response.status_code}")
            if response.status_code != 200:
                raise EventProviderError(f"BEA returned HTTP {response.status_code}")
            content_type = response.headers.get("content-type")
            if content_type is not None and "json" not in content_type.lower():
                raise EventProviderError(
                    f"BEA returned unexpected content type {content_type!r}"
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_EVENT_FEED_BYTES:
                    raise EventProviderError(
                        f"BEA response exceeds the {MAX_EVENT_FEED_BYTES}-byte limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks)

    def _parse(self, raw_payload: bytes) -> _BeaParsedFeed:
        _validate_size(raw_payload, "BEA response")
        try:
            document = json.loads(raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventProviderError(f"invalid BEA JSON: {exc}") from exc
        if not isinstance(document, dict) or len(document) > 250:
            raise EventProviderError("invalid BEA JSON: expected a bounded object")
        source_updated_at = document.get("file_last_updated")
        if not isinstance(source_updated_at, str) or not source_updated_at.strip():
            raise EventProviderError("invalid BEA JSON: missing file_last_updated")
        try:
            datetime.fromisoformat(source_updated_at)
        except ValueError as exc:
            raise EventProviderError("invalid BEA JSON: malformed file_last_updated") from exc

        releases: dict[str, tuple[datetime, ...]] = {}
        skipped_duplicates = 0
        for release_name, value in document.items():
            if release_name == "file_last_updated":
                continue
            if not isinstance(release_name, str) or not release_name.strip():
                raise EventProviderError("invalid BEA JSON: blank release name")
            if not isinstance(value, dict) or set(value) != {"release_dates"}:
                raise EventProviderError(
                    f"invalid BEA JSON: malformed series {release_name!r}"
                )
            raw_dates = value["release_dates"]
            if not isinstance(raw_dates, list) or len(raw_dates) > 500:
                raise EventProviderError(
                    f"invalid BEA JSON: malformed dates for {release_name!r}"
                )
            parsed_dates: set[datetime] = set()
            for raw_date in raw_dates:
                if not isinstance(raw_date, str):
                    raise EventProviderError(
                        f"invalid BEA JSON: non-string date for {release_name!r}"
                    )
                try:
                    release_at = datetime.fromisoformat(raw_date)
                except ValueError as exc:
                    raise EventProviderError(
                        f"invalid BEA JSON: malformed date for {release_name!r}"
                    ) from exc
                if release_at.tzinfo is None or release_at.utcoffset() is None:
                    raise EventProviderError(
                        f"invalid BEA JSON: timezone missing for {release_name!r}"
                    )
                release_at = release_at.astimezone(UTC)
                if release_at in parsed_dates:
                    skipped_duplicates += 1
                parsed_dates.add(release_at)
            releases[release_name] = tuple(sorted(parsed_dates))

        missing = set(self.included_release_names).difference(releases)
        if missing:
            raise EventProviderError(
                "BEA feed is missing configured release series: "
                + ", ".join(sorted(missing))
            )
        if not releases:
            raise EventProviderError("invalid BEA JSON: no release series")
        return _BeaParsedFeed(
            source_updated_at=source_updated_at,
            releases=releases,
            skipped_duplicates=skipped_duplicates,
        )

    @property
    def _metadata_path(self) -> Path:
        return self.cache_path.with_name(f"{self.cache_path.name}.metadata.json")

    def _write_cache(self, raw_payload: bytes, retrieved_at: datetime) -> None:
        metadata = _CacheMetadata(
            source_reference=BEA_RELEASE_DATES_URL,
            retrieved_at=retrieved_at,
            content_hash=_content_hash(raw_payload),
        )
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.cache_path, raw_payload)
            _atomic_write(self._metadata_path, metadata.model_dump_json().encode())
        except OSError as exc:
            raise EventProviderError(f"cannot update BEA cache {self.cache_path}: {exc}") from exc

    def _read_cache(self, now: datetime) -> bytes:
        try:
            raw_payload = self.cache_path.read_bytes()
            metadata_raw = self._metadata_path.read_bytes()
        except OSError as exc:
            raise EventProviderError(f"cannot read BEA cache {self.cache_path}: {exc}") from exc
        _validate_size(raw_payload, f"BEA cache {self.cache_path}")
        try:
            metadata = _CacheMetadata.model_validate_json(metadata_raw)
        except (ValidationError, ValueError) as exc:
            raise EventProviderError(f"invalid BEA cache metadata: {exc}") from exc
        if metadata.content_hash != _content_hash(raw_payload):
            raise EventProviderError("BEA cache content hash does not match metadata")
        age = now - metadata.retrieved_at
        if age < -_CACHE_CLOCK_SKEW:
            raise EventProviderError("BEA cache timestamp is in the future")
        if age > self.max_cache_age:
            raise EventProviderError(
                f"BEA cache is stale ({int(age.total_seconds() // 60)} minutes old)"
            )
        return raw_payload


def _validate_discovery_inputs(
    symbols: frozenset[str],
    window_start: datetime,
    window_end: datetime,
    retrieved_at: datetime | None,
) -> tuple[datetime, datetime, datetime]:
    start = _aware_utc(window_start, "window_start")
    end = _aware_utc(window_end, "window_end")
    retrieved = _aware_utc(retrieved_at or datetime.now(UTC), "retrieved_at")
    if start >= end:
        raise EventProviderError("event discovery window must have positive duration")
    if not symbols:
        raise EventProviderError("event discovery requires at least one configured symbol")
    return start, end, retrieved


def _validate_size(raw_payload: bytes, source: str) -> None:
    if len(raw_payload) > MAX_EVENT_FEED_BYTES:
        raise EventProviderError(
            f"event feed {source} exceeds the {MAX_EVENT_FEED_BYTES}-byte limit"
        )


def _bea_source_event_id(release_name: str, year: int, ordinal: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", release_name.lower()).strip("-")[:120]
    digest = hashlib.sha256(release_name.encode()).hexdigest()[:12]
    return f"{slug}:{digest}:{year}:{ordinal:03d}"


def _content_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventProviderError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)
