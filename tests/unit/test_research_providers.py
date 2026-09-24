from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alpaca.data.enums import DataFeed

from trader.research.alpaca import AlpacaResearchError, AlpacaResearchProvider
from trader.research.artifacts import (
    ImmutableResearchArtifactWriter,
    ResearchArtifactConflictError,
    ResearchArtifactError,
    canonical_json_bytes,
)
from trader.research.collection import (
    BoundedResearchCollector,
    ResearchCollection,
    ResearchCollectionError,
    write_research_artifacts,
)
from trader.research.models import QuestionType, ResearchRequest
from trader.research.sec import (
    SEC_ARCHIVE_DOCUMENT_URL,
    SEC_COMPANY_TICKERS_URL,
    SEC_COMPANYFACTS_URL,
    SEC_SUBMISSIONS_URL,
    SecResearchError,
    SecResearchProvider,
    SecTickerMapResolver,
)

RETRIEVED = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 22, tzinfo=UTC)


def _request(question_type: QuestionType = "MARKET_CONTEXT") -> ResearchRequest:
    return ResearchRequest.create(
        symbol="AAPL",
        question_type=question_type,
        query=f"Research AAPL {question_type}",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        priority=90,
    )


class StubStockClient:
    def __init__(self) -> None:
        self.snapshot_requests: list[object] = []
        self.bar_requests: list[object] = []

    def get_stock_snapshot(self, request_params: object) -> object:
        self.snapshot_requests.append(request_params)
        return {
            "AAPL": {
                "latestTrade": {"p": 231.4, "t": "2026-08-21T15:59:58Z"},
                "latestQuote": {"ap": 231.5, "bp": 231.3, "t": "2026-08-21T15:59:59Z"},
            }
        }

    def get_stock_bars(self, request_params: object) -> object:
        self.bar_requests.append(request_params)
        return {
            "AAPL": [
                {
                    "t": "2026-08-20T04:00:00Z",
                    "o": 229.0,
                    "h": 233.0,
                    "l": 228.0,
                    "c": 231.0,
                    "v": 1000,
                }
            ]
        }


class StubNewsClient:
    def __init__(self, response: object | None = None) -> None:
        self.requests: list[object] = []
        self.response = response or {
            "news": [
                {
                    "id": 101,
                    "headline": "Apple files product update",
                    "source": "example-wire",
                    "author": "Reporter",
                    "created_at": "2026-08-21T14:00:00Z",
                    "updated_at": "2026-08-21T14:10:00Z",
                    "url": "https://example.test/apple",
                    "summary": "A short summary",
                    "content": "Full licensed content retained in the raw payload.",
                    "symbols": ["AAPL"],
                }
            ]
        }

    def get_news(self, request_params: object) -> object:
        self.requests.append(request_params)
        return self.response


def test_alpaca_market_context_retains_exact_normalized_responses() -> None:
    stock = StubStockClient()
    news = StubNewsClient()
    provider = AlpacaResearchProvider(
        stock_client=stock,
        news_client=news,
        feed=DataFeed.IEX,
        max_bars=10,
    )

    batch = provider.collect(_request(), retrieved_at=RETRIEVED)

    assert batch.provider == "alpaca"
    assert batch.request_count == 2
    assert len(batch.documents) == 2
    assert {document.source_name for document in batch.documents} == {
        "Alpaca stock snapshot",
        "Alpaca adjusted daily bars",
    }
    assert all(document.metadata["feed"] == "iex" for document in batch.documents)
    assert batch.response_bytes == sum(len(document.raw_payload) for document in batch.documents)
    assert all(
        document.content_hash == hashlib.sha256(document.raw_payload).hexdigest()
        for document in batch.documents
    )
    assert len(stock.snapshot_requests) == 1
    assert len(stock.bar_requests) == 1


def test_alpaca_news_retains_the_complete_response_and_no_results() -> None:
    provider = AlpacaResearchProvider(
        stock_client=StubStockClient(),
        news_client=StubNewsClient(),
    )
    batch = provider.collect(_request("COMPANY_NEWS"), retrieved_at=RETRIEVED)
    document = batch.documents[0]
    assert json.loads(document.raw_payload)["news"][0]["id"] == 101
    assert document.published_at == datetime(2026, 8, 21, 14, 10, tzinfo=UTC)
    assert document.metadata["item_count"] == 1

    empty_provider = AlpacaResearchProvider(
        stock_client=StubStockClient(),
        news_client=StubNewsClient({"news": []}),
    )
    empty = empty_provider.collect(_request("COMPANY_NEWS"), retrieved_at=RETRIEVED)
    assert len(empty.documents) == 1
    assert empty.documents[0].headline.endswith("(no results)")
    assert empty.documents[0].raw_payload == canonical_json_bytes({"news": []})


def test_alpaca_rejects_unsupported_questions_and_malformed_payloads() -> None:
    provider = AlpacaResearchProvider(
        stock_client=StubStockClient(),
        news_client=StubNewsClient(),
    )
    with pytest.raises(AlpacaResearchError, match="does not support"):
        provider.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)

    class MissingSnapshot(StubStockClient):
        def get_stock_snapshot(self, request_params: object) -> object:
            return {"MSFT": {}}

    unavailable = AlpacaResearchProvider(
        stock_client=MissingSnapshot(),
        news_client=StubNewsClient(),
    )
    unavailable_batch = unavailable.collect(_request(), retrieved_at=RETRIEVED)
    assert len(unavailable_batch.documents) == 2
    assert all(
        document.metadata["symbol_snapshot_present"] is False
        for document in unavailable_batch.documents
    )

    class InvalidSnapshot(StubStockClient):
        def get_stock_snapshot(self, request_params: object) -> object:
            return {"AAPL": []}

    malformed = AlpacaResearchProvider(
        stock_client=InvalidSnapshot(),
        news_client=StubNewsClient(),
    )
    with pytest.raises(AlpacaResearchError, match="invalid symbol snapshot"):
        malformed.collect(_request(), retrieved_at=RETRIEVED)


def _submissions(cik: int = 320193) -> dict[str, object]:
    return {
        "cik": cik,
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
                "filingDate": ["2026-08-20", "2026-07-01"],
                "reportDate": ["2026-06-30", "2026-03-31"],
                "acceptanceDateTime": [
                    "2026-08-20T20:15:00Z",
                    "2026-07-01T20:15:00Z",
                ],
                "form": ["10-Q", "8-K"],
                "primaryDocument": ["aapl-20260630.htm", "aapl-8k.htm"],
            }
        },
    }


def _companyfacts(cik: int = 320193) -> dict[str, object]:
    return {
        "cik": cik,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Assets": {
                    "label": "Assets",
                    "description": "Total assets",
                    "units": {
                        "USD": [
                            {
                                "end": "2026-06-30",
                                "val": 100,
                                "accn": "0000320193-26-000001",
                                "fy": 2026,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2026-08-20",
                            }
                        ]
                    },
                }
            }
        },
    }


def _sec_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sec_uses_fixed_endpoints_user_agent_and_exact_payloads() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["user-agent"] == "AI Trader test test@example.com"
        if "/submissions/" in str(request.url):
            return httpx.Response(200, json=_submissions())
        if "/Archives/edgar/" in str(request.url):
            return httpx.Response(
                200,
                content=(
                    b"<html><body><h1>Quarterly report</h1>"
                    b"<p>Revenue improved.</p></body></html>"
                ),
            )
        return httpx.Response(200, json=_companyfacts())

    provider = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": 320193},
        client=_sec_client(handler),
        sleep=lambda _seconds: None,
    )
    batch = provider.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)

    assert [str(call.url) for call in calls] == [
        SEC_SUBMISSIONS_URL.format(cik="0000320193"),
        SEC_COMPANYFACTS_URL.format(cik="0000320193"),
        SEC_ARCHIVE_DOCUMENT_URL.format(
            cik="320193",
            accession="000032019326000001",
            document="aapl-20260630.htm",
        ),
    ]
    assert batch.provider == "sec"
    assert batch.request_count == 3
    assert len(batch.documents) == 3
    submissions = next(
        document for document in batch.documents if document.metadata["endpoint"] == "submissions"
    )
    assert submissions.metadata["filing_count_in_window"] == 1
    assert submissions.metadata["accession_numbers"] == ["0000320193-26-000001"]
    assert json.loads(submissions.raw_payload)["filings"]["recent"]["form"] == [
        "10-Q",
        "8-K",
    ]
    assert all(document.source_tier == "PRIMARY" for document in batch.documents)
    filing = next(
        document
        for document in batch.documents
        if document.metadata["endpoint"] == "primary_filing_document"
    )
    assert "Revenue improved." in filing.normalized_text
    assert filing.metadata["form"] == "10-Q"


def test_sec_retains_submissions_when_companyfacts_is_not_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/submissions/" in str(request.url):
            return httpx.Response(200, json=_submissions())
        if "/Archives/edgar/" in str(request.url):
            return httpx.Response(200, content=b"<p>Quarterly report text.</p>")
        return httpx.Response(404, json={"message": "not found"})

    provider = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": 320193},
        client=_sec_client(handler),
    )

    batch = provider.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)

    assert batch.request_count == 3
    assert len(batch.documents) == 2
    submissions = next(
        document for document in batch.documents if document.metadata["endpoint"] == "submissions"
    )
    assert submissions.metadata["companyfacts_available"] is False
    assert "HTTP 404" in str(submissions.metadata["companyfacts_error"])


def test_sec_retries_only_bounded_transient_failures() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "busy"})
        if "/submissions/" in str(request.url):
            return httpx.Response(200, json=_submissions())
        if "/Archives/edgar/" in str(request.url):
            return httpx.Response(200, content=b"<p>Quarterly report text.</p>")
        return httpx.Response(200, json=_companyfacts())

    provider = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": "320193"},
        client=_sec_client(handler),
        max_attempts=2,
        sleep=sleeps.append,
    )
    batch = provider.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)
    assert batch.request_count == 4
    assert calls == 4
    assert sleeps == [0.25]


def test_sec_history_fetches_older_primary_document_without_repeating_companyfacts() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "/submissions/" in str(request.url):
            return httpx.Response(200, json=_submissions())
        if str(request.url).endswith("-index.html"):
            return httpx.Response(200, content=b"<html><table></table></html>")
        if str(request.url).endswith("aapl-8k.htm"):
            return httpx.Response(200, content=b"<p>Earlier material event disclosure.</p>")
        raise AssertionError(f"unexpected SEC request: {request.url}")

    request = ResearchRequest.create(
        symbol="AAPL",
        question_type="SEC_FILING_HISTORY",
        query="Retrieve an older operating baseline for AAPL",
        window_start=datetime(2026, 6, 1, tzinfo=UTC),
        window_end=datetime(2026, 8, 1, tzinfo=UTC),
        priority=80,
    )
    provider = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": 320193},
        max_primary_documents=1,
        client=_sec_client(handler),
    )

    batch = provider.collect(request, retrieved_at=RETRIEVED)

    assert len(calls) == 3
    assert all("companyfacts" not in str(call.url) for call in calls)
    filing = next(
        document
        for document in batch.documents
        if document.metadata["endpoint"] == "primary_filing_document"
    )
    assert filing.metadata["form"] == "8-K"
    assert filing.metadata["historical_follow_up"] is True
    assert "Earlier material event disclosure." in filing.normalized_text


def test_sec_retains_issuer_authored_ex99_release_from_fixed_archive_path() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "/submissions/" in url:
            return httpx.Response(200, json=_submissions())
        if "/companyfacts/" in url:
            return httpx.Response(200, json=_companyfacts())
        if url.endswith("aapl-8k.htm"):
            return httpx.Response(200, content=b"<p>Item 2.02 results furnished.</p>")
        if url.endswith("-index.html"):
            return httpx.Response(
                200,
                content=(
                    b'<table><tr><td>2</td><td><a href="/Archives/edgar/data/'
                    b'320193/000032019326000002/aapl-ex991.htm">aapl-ex991.htm</a>'
                    b"</td><td>EX-99.1</td></tr></table>"
                ),
            )
        if url.endswith("aapl-ex991.htm"):
            return httpx.Response(
                200,
                content=b"<h1>Quarterly Results</h1><p>Revenue and guidance improved.</p>",
            )
        raise AssertionError(f"unexpected SEC request: {url}")

    request = ResearchRequest.create(
        symbol="AAPL",
        question_type="SEC_FILINGS",
        query="Retrieve AAPL's material issuer release",
        window_start=datetime(2026, 6, 1, tzinfo=UTC),
        window_end=datetime(2026, 8, 1, tzinfo=UTC),
        priority=80,
    )
    provider = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": 320193},
        max_primary_documents=1,
        client=_sec_client(handler),
    )

    batch = provider.collect(request, retrieved_at=RETRIEVED)

    assert batch.request_count == 5
    exhibit = next(
        document
        for document in batch.documents
        if document.metadata["endpoint"] == "issuer_filing_exhibit"
    )
    assert exhibit.metadata["exhibit_type"] == "EX-99.1"
    assert "Revenue and guidance improved." in exhibit.normalized_text
    assert exhibit.url is not None
    assert exhibit.url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    assert all(url.startswith(("https://data.sec.gov/", "https://www.sec.gov/")) for url in calls)


def test_sec_rejects_missing_mapping_bad_schema_status_and_oversize() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        SecResearchProvider(user_agent="anonymous", symbol_to_cik={"AAPL": 320193})

    provider = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"MSFT": 789019},
        client=_sec_client(lambda _request: httpx.Response(200, json={})),
    )
    with pytest.raises(SecResearchError, match="no deterministic SEC CIK"):
        provider.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)

    bad_status = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": 320193},
        client=_sec_client(lambda _request: httpx.Response(404, json={})),
    )
    with pytest.raises(SecResearchError, match="HTTP 404"):
        bad_status.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)

    bad_schema = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": 320193},
        client=_sec_client(lambda _request: httpx.Response(200, json={"cik": 123})),
    )
    with pytest.raises(SecResearchError, match="CIK does not match"):
        bad_schema.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)

    too_large = SecResearchProvider(
        user_agent="AI Trader test test@example.com",
        symbol_to_cik={"AAPL": 320193},
        max_response_bytes=1_000,
        client=_sec_client(
            lambda _request: httpx.Response(
                200,
                headers={"content-length": "1001"},
                content=b"{}",
            )
        ),
    )
    with pytest.raises(SecResearchError, match="response byte limit"):
        too_large.collect(_request("SEC_FILINGS"), retrieved_at=RETRIEVED)


def test_sec_ticker_map_resolver_returns_exact_and_canonical_payloads() -> None:
    raw_payload = (
        b'{"1":{"title":"Microsoft Corp","ticker":"msft","cik_str":789019},'
        b'"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}}'
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["user-agent"] == "AI Trader test test@example.com"
        return httpx.Response(200, content=raw_payload)

    resolver = SecTickerMapResolver(
        user_agent="AI Trader test test@example.com",
        client=_sec_client(handler),
        sleep=lambda _seconds: None,
    )
    snapshot = resolver.resolve(retrieved_at=RETRIEVED)

    assert [str(call.url) for call in calls] == [SEC_COMPANY_TICKERS_URL]
    assert dict(snapshot.symbol_to_cik) == {
        "AAPL": "0000320193",
        "MSFT": "0000789019",
    }
    assert snapshot.raw_payload == raw_payload
    assert snapshot.raw_content_hash == hashlib.sha256(raw_payload).hexdigest()
    assert snapshot.canonical_payload == (
        b'{"AAPL":"0000320193","MSFT":"0000789019"}'
    )
    assert snapshot.canonical_content_hash == hashlib.sha256(
        snapshot.canonical_payload
    ).hexdigest()
    assert snapshot.entry_count == 2
    assert snapshot.request_count == 1
    assert snapshot.response_bytes == len(raw_payload)
    assert snapshot.summary()["entry_count"] == 2
    with pytest.raises(TypeError):
        snapshot.symbol_to_cik["TSLA"] = "0001318605"  # type: ignore[index]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            b'{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple"},'
            b'"1":{"cik_str":320194,"ticker":"aapl","title":"Duplicate"}}',
            "duplicate ticker",
        ),
        (
            b'{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple"},'
            b'"0":{"cik_str":789019,"ticker":"MSFT","title":"Microsoft"}}',
            "duplicate JSON key",
        ),
        (
            b'{"1":{"cik_str":320193,"ticker":"AAPL","title":"Apple"}}',
            "contiguous",
        ),
        (
            b'{"0":{"cik_str":"320193","ticker":"AAPL","title":"Apple"}}',
            "invalid CIK",
        ),
        (
            b'{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple","extra":1}}',
            "invalid schema",
        ),
    ],
)
def test_sec_ticker_map_resolver_rejects_duplicates_and_bad_schema(
    payload: bytes,
    message: str,
) -> None:
    resolver = SecTickerMapResolver(
        user_agent="AI Trader test test@example.com",
        client=_sec_client(lambda _request: httpx.Response(200, content=payload)),
    )
    with pytest.raises(SecResearchError, match=message):
        resolver.resolve(retrieved_at=RETRIEVED)


def test_sec_ticker_map_resolver_has_bounded_retry_and_size() -> None:
    calls = 0
    sleeps: list[float] = []
    valid = b'{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple"}}'

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, content=b"busy")
        return httpx.Response(200, content=valid)

    resolver = SecTickerMapResolver(
        user_agent="AI Trader test test@example.com",
        client=_sec_client(handler),
        max_attempts=2,
        sleep=sleeps.append,
    )
    snapshot = resolver.resolve(retrieved_at=RETRIEVED)
    assert snapshot.request_count == 2
    assert calls == 2
    assert sleeps == [0.25]

    oversize = SecTickerMapResolver(
        user_agent="AI Trader test test@example.com",
        max_response_bytes=1_000,
        client=_sec_client(
            lambda _request: httpx.Response(
                200,
                headers={"content-length": "1001"},
                content=b"{}",
            )
        ),
    )
    with pytest.raises(SecResearchError, match="response byte limit"):
        oversize.resolve(retrieved_at=RETRIEVED)


def test_immutable_artifacts_are_canonical_idempotent_and_conflict_safe(
    tmp_path: Path,
) -> None:
    writer = ImmutableResearchArtifactWriter(tmp_path / "run" / "research")
    first = writer.write_json("alpaca/item.json", {"b": 2, "a": 1})
    replay = writer.write_json("alpaca/item.json", {"a": 1, "b": 2})

    assert first.relative_path == "alpaca/item.json"
    assert first.created is True
    assert replay.created is False
    assert first.content_hash == replay.content_hash
    assert (tmp_path / "run" / "research" / first.relative_path).read_bytes() == b'{"a":1,"b":2}'
    with pytest.raises(ResearchArtifactConflictError):
        writer.write_json("alpaca/item.json", {"a": 999})


@pytest.mark.parametrize(
    "path",
    ["../escape.json", "/absolute.json", "alpaca/../../escape.json", "a\\b.json", ""],
)
def test_immutable_artifacts_reject_path_traversal(tmp_path: Path, path: str) -> None:
    writer = ImmutableResearchArtifactWriter(tmp_path / "research")
    with pytest.raises(ResearchArtifactError):
        writer.write_bytes(path, b"evidence")


def test_artifact_writer_deduplicates_documents_across_batches(tmp_path: Path) -> None:
    provider = AlpacaResearchProvider(
        stock_client=StubStockClient(),
        news_client=StubNewsClient(),
    )
    batch = provider.collect(_request("COMPANY_NEWS"), retrieved_at=RETRIEVED)
    collection = ResearchCollection(
        batches=(batch, batch),
        request_count=2,
        response_bytes=2 * batch.response_bytes,
    )
    artifacts = write_research_artifacts(
        collection,
        ImmutableResearchArtifactWriter(tmp_path / "research"),
    )
    assert set(artifacts) == {batch.documents[0].research_id}
    assert len(list((tmp_path / "research" / "alpaca").iterdir())) == 1


def test_bounded_collector_routes_and_enforces_preflight_request_budget() -> None:
    alpaca = AlpacaResearchProvider(
        stock_client=StubStockClient(),
        news_client=StubNewsClient(),
    )
    collector = BoundedResearchCollector(
        {
            "MARKET_CONTEXT": alpaca,
            "COMPANY_NEWS": alpaca,
        },
        max_questions=2,
        max_http_requests=2,
        max_documents=10,
        max_response_bytes=1_000_000,
    )
    result = collector.collect((_request(),), retrieved_at=RETRIEVED)
    assert result.request_count == 2
    assert len(result.documents) == 2
    with pytest.raises(ResearchCollectionError, match="budget would be exceeded"):
        collector.collect((_request("COMPANY_NEWS"), _request()), retrieved_at=RETRIEVED)
