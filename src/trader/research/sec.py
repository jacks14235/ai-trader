"""Bounded primary-source collection from fixed SEC data endpoints."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Literal, cast

import httpx
from pydantic import JsonValue

from trader.agent.models import SYMBOL_PATTERN
from trader.research.models import (
    ProviderName,
    QuestionType,
    ResearchBatch,
    ResearchDocument,
    ResearchRequest,
)

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_ARCHIVE_DOCUMENT_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
)
SEC_FILING_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{accession_hyphenated}-index.html"
)
SEC_COMPANY_TICKERS_URL: Literal[
    "https://www.sec.gov/files/company_tickers.json"
] = "https://www.sec.gov/files/company_tickers.json"
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_SUBSTANTIVE_FORMS = frozenset({"10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"})
_ACCESSION_PATTERN = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_DOCUMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class SecResearchError(RuntimeError):
    """Raised when SEC data cannot be fetched or fails strict validation."""


class SecHttpStatusError(SecResearchError):
    """A non-retryable SEC HTTP response with an explicit status code."""

    def __init__(self, url: str, status_code: int) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(f"SEC endpoint {url} returned HTTP {status_code}")


@dataclass(frozen=True)
class SecTickerMapSnapshot:
    """Immutable official ticker map plus exact and normalized payload identities."""

    source_reference: Literal["https://www.sec.gov/files/company_tickers.json"]
    retrieved_at: datetime
    symbol_to_cik: Mapping[str, str]
    raw_payload: bytes
    raw_content_hash: str
    canonical_payload: bytes
    canonical_content_hash: str
    request_count: int
    response_bytes: int

    @property
    def entry_count(self) -> int:
        return len(self.symbol_to_cik)

    def summary(self) -> dict[str, object]:
        return {
            "source_reference": self.source_reference,
            "retrieved_at": self.retrieved_at.isoformat(),
            "entry_count": self.entry_count,
            "raw_content_hash": self.raw_content_hash,
            "canonical_content_hash": self.canonical_content_hash,
            "request_count": self.request_count,
            "response_bytes": self.response_bytes,
        }


class SecTickerMapResolver:
    """Resolve all SEC tickers from the fixed official JSON reference file.

    The explicit contact User-Agent is mandatory. No symbol search, HTML scraping, or
    fallback endpoint is used.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 10,
        max_response_bytes: int = 2_000_000,
        max_attempts: int = 2,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = _validated_user_agent(user_agent)
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("SEC timeout_seconds must be between 1 and 60")
        if not 1_000 <= max_response_bytes <= 10_000_000:
            raise ValueError("SEC max_response_bytes must be between 1,000 and 10,000,000")
        if not 1 <= max_attempts <= 5:
            raise ValueError("SEC max_attempts must be between 1 and 5")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_attempts = max_attempts
        self.client = client or httpx.Client()
        self.sleep = sleep

    def resolve(self, *, retrieved_at: datetime | None = None) -> SecTickerMapSnapshot:
        raw_payload, attempts = _fetch_sec_payload(
            client=self.client,
            user_agent=self.user_agent,
            url=SEC_COMPANY_TICKERS_URL,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            max_attempts=self.max_attempts,
            sleep=self.sleep,
        )
        retrieved = _retrieval_time(retrieved_at)
        payload = _load_object(raw_payload, "SEC company tickers")
        symbol_to_cik = _validate_company_tickers(payload)
        canonical_payload = _canonical_ticker_mapping(symbol_to_cik)
        return SecTickerMapSnapshot(
            source_reference=SEC_COMPANY_TICKERS_URL,
            retrieved_at=retrieved,
            symbol_to_cik=MappingProxyType(symbol_to_cik),
            raw_payload=raw_payload,
            raw_content_hash=hashlib.sha256(raw_payload).hexdigest(),
            canonical_payload=canonical_payload,
            canonical_content_hash=hashlib.sha256(canonical_payload).hexdigest(),
            request_count=attempts,
            response_bytes=len(raw_payload),
        )


class SecResearchProvider:
    """Collect SEC submissions and company facts from deterministic CIK mappings.

    ``user_agent`` is mandatory and must identify the calling application plus a
    contact address. ``symbol_to_cik`` must be supplied by application-owned reference
    data. This provider never resolves tickers through a search page or search engine.
    """

    provider_name: ProviderName = "sec"
    supported_question_types: frozenset[QuestionType] = frozenset(
        {"SEC_FILINGS", "SEC_FILING_HISTORY"}
    )

    def __init__(
        self,
        *,
        user_agent: str,
        symbol_to_cik: Mapping[str, str | int],
        timeout_seconds: float = 10,
        max_response_bytes: int = 5_000_000,
        max_attempts: int = 2,
        max_filings: int = 40,
        max_primary_documents: int = 2,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_user_agent = _validated_user_agent(user_agent)
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("SEC timeout_seconds must be between 1 and 60")
        if not 1_000 <= max_response_bytes <= 10_000_000:
            raise ValueError("SEC max_response_bytes must be between 1,000 and 10,000,000")
        if not 1 <= max_attempts <= 5:
            raise ValueError("SEC max_attempts must be between 1 and 5")
        if not 1 <= max_filings <= 100:
            raise ValueError("SEC max_filings must be between 1 and 100")
        if not 1 <= max_primary_documents <= 5:
            raise ValueError("SEC max_primary_documents must be between 1 and 5")
        self.user_agent = normalized_user_agent
        self.symbol_to_cik = _normalize_cik_mapping(symbol_to_cik)
        if not self.symbol_to_cik:
            raise ValueError("SEC symbol_to_cik mapping cannot be empty")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_attempts = max_attempts
        self.max_filings = max_filings
        self.max_primary_documents = max_primary_documents
        self.client = client or httpx.Client()
        self.sleep = sleep

    def estimated_request_count(self, request: ResearchRequest) -> int:
        if request.question_type not in self.supported_question_types:
            raise SecResearchError(f"SEC research does not support {request.question_type}")
        # Every retained primary document may be an 8-K/6-K whose issuer-authored
        # EX-99 release requires one index request and one exhibit request.
        endpoint_count = 1 + (3 * self.max_primary_documents)
        if request.question_type == "SEC_FILINGS":
            endpoint_count += 1
        return endpoint_count * self.max_attempts

    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch:
        if request.question_type not in self.supported_question_types:
            raise SecResearchError(f"SEC research does not support {request.question_type}")
        cik = self.symbol_to_cik.get(request.symbol)
        if cik is None:
            raise SecResearchError(
                f"no deterministic SEC CIK mapping configured for {request.symbol}"
            )
        submissions_url = SEC_SUBMISSIONS_URL.format(cik=cik)
        submissions_bytes, submissions_attempts = self._fetch(submissions_url)
        facts_url = SEC_COMPANYFACTS_URL.format(cik=cik)
        facts_bytes: bytes | None = None
        facts_attempts = 0
        facts_error: str | None = None
        if request.question_type == "SEC_FILINGS":
            try:
                facts_bytes, facts_attempts = self._fetch(facts_url)
            except SecHttpStatusError as exc:
                if exc.status_code != 404:
                    raise
                facts_attempts = 1
                facts_error = str(exc)
        retrieved = _retrieval_time(retrieved_at)
        submissions = _load_object(submissions_bytes, "SEC submissions")

        recent_rows, entity_name = _validate_submissions(
            submissions,
            cik,
            window_start=request.window_start,
            window_end=request.window_end,
            max_filings=self.max_filings,
        )
        selected_rows = _select_primary_filing_rows(
            recent_rows,
            limit=self.max_primary_documents,
            historical=request.question_type == "SEC_FILING_HISTORY",
        )
        published_at = _latest_submission_timestamp(recent_rows, retrieved)
        common_metadata: dict[str, JsonValue] = {
            "cik": cik,
            "symbol": request.symbol,
            "query": request.query,
            "window_start": request.window_start.isoformat(),
            "window_end": request.window_end.isoformat(),
        }
        submission_document = ResearchDocument.create(
            question_ids=(request.question_id,),
            provider="sec",
            source_type="REGULATORY_FILING",
            source_tier="PRIMARY",
            source_name="SEC EDGAR submissions",
            provider_item_id=_provider_item_id("submissions", cik, submissions_bytes),
            url=submissions_url,
            author=None,
            published_at=published_at,
            retrieved_at=retrieved,
            symbols=(request.symbol,),
            headline=f"{entity_name} SEC submissions ({len(recent_rows)} in window)",
            normalized_text=_normalized_json(recent_rows),
            summary=None,
            raw_payload=submissions_bytes,
            metadata={
                **common_metadata,
                "endpoint": "submissions",
                "entity_name": entity_name,
                "filing_count_in_window": len(recent_rows),
                "retained_primary_document_count": len(selected_rows),
                "companyfacts_available": facts_bytes is not None,
                "companyfacts_error": facts_error,
                "forms": cast(
                    "JsonValue",
                    sorted({str(row["form"]) for row in recent_rows if row.get("form")}),
                ),
                "accession_numbers": cast(
                    "JsonValue",
                    [str(row["accessionNumber"]) for row in recent_rows],
                ),
            },
            cost_usd=Decimal("0"),
        )
        documents = [submission_document]
        if facts_bytes is not None:
            facts = _load_object(facts_bytes, "SEC company facts")
            fact_summary, facts_entity_name, facts_published_at = _validate_companyfacts(
                facts,
                cik,
                retrieved,
            )
            documents.append(
                ResearchDocument.create(
                    question_ids=(request.question_id,),
                    provider="sec",
                    source_type="REGULATORY_FILING",
                    source_tier="PRIMARY",
                    source_name="SEC XBRL company facts",
                    provider_item_id=_provider_item_id("companyfacts", cik, facts_bytes),
                    url=facts_url,
                    author=None,
                    published_at=facts_published_at,
                    retrieved_at=retrieved,
                    symbols=(request.symbol,),
                    headline=f"{facts_entity_name} SEC XBRL company facts",
                    normalized_text=_normalized_json(fact_summary),
                    summary=None,
                    raw_payload=facts_bytes,
                    metadata={
                        **common_metadata,
                        "endpoint": "companyfacts",
                        "entity_name": facts_entity_name,
                        "taxonomy_count": fact_summary["taxonomy_count"],
                        "concept_count": fact_summary["concept_count"],
                        "latest_filed_date": fact_summary["latest_filed_date"],
                    },
                    cost_usd=Decimal("0"),
                )
            )
        filing_attempts = 0
        filing_response_bytes = 0
        for row in selected_rows:
            url = _primary_document_url(cik, row)
            payload, attempts = self._fetch(url)
            filing_attempts += attempts
            filing_response_bytes += len(payload)
            accession = str(row["accessionNumber"])
            form = str(row["form"])
            accepted = _sec_datetime(
                str(row["acceptanceDateTime"]), "acceptanceDateTime"
            )
            if accepted > retrieved:
                raise SecResearchError("SEC filing acceptance time is after retrieval")
            document_name = str(row["primaryDocument"])
            documents.append(
                ResearchDocument.create(
                    question_ids=(request.question_id,),
                    provider="sec",
                    source_type="REGULATORY_FILING",
                    source_tier="PRIMARY",
                    source_name="SEC EDGAR primary filing document",
                    provider_item_id=f"filing:{accession}:{document_name}",
                    url=url,
                    author=None,
                    published_at=accepted,
                    retrieved_at=retrieved,
                    symbols=(request.symbol,),
                    headline=f"{entity_name} {form} filed {row['filingDate']}",
                    normalized_text=_normalized_filing_text(payload),
                    summary=None,
                    raw_payload=payload,
                    metadata={
                        **common_metadata,
                        "endpoint": "primary_filing_document",
                        "entity_name": entity_name,
                        "accession_number": accession,
                        "form": form,
                        "filing_date": str(row["filingDate"]),
                        "report_date": str(row["reportDate"]),
                        "primary_document": document_name,
                        "historical_follow_up": (
                            request.question_type == "SEC_FILING_HISTORY"
                        ),
                    },
                    cost_usd=Decimal("0"),
                )
            )
            if form in {"8-K", "6-K"}:
                index_url = _filing_index_url(cik, accession)
                index_payload, index_attempts = self._fetch(index_url)
                filing_attempts += index_attempts
                filing_response_bytes += len(index_payload)
                exhibit = _issuer_exhibit_reference(index_payload)
                if exhibit is not None:
                    exhibit_type, exhibit_name = exhibit
                    exhibit_url = _archive_document_url(cik, accession, exhibit_name)
                    exhibit_payload, exhibit_attempts = self._fetch(exhibit_url)
                    filing_attempts += exhibit_attempts
                    filing_response_bytes += len(exhibit_payload)
                    documents.append(
                        ResearchDocument.create(
                            question_ids=(request.question_id,),
                            provider="sec",
                            source_type="REGULATORY_FILING",
                            source_tier="PRIMARY",
                            source_name="SEC EDGAR issuer exhibit",
                            provider_item_id=(
                                f"exhibit:{accession}:{exhibit_type}:{exhibit_name}"
                            ),
                            url=exhibit_url,
                            author=None,
                            published_at=accepted,
                            retrieved_at=retrieved,
                            symbols=(request.symbol,),
                            headline=(
                                f"{entity_name} {exhibit_type} issuer exhibit filed "
                                f"{row['filingDate']}"
                            ),
                            normalized_text=_normalized_filing_text(exhibit_payload),
                            summary=None,
                            raw_payload=exhibit_payload,
                            metadata={
                                **common_metadata,
                                "endpoint": "issuer_filing_exhibit",
                                "entity_name": entity_name,
                                "accession_number": accession,
                                "parent_form": form,
                                "exhibit_type": exhibit_type,
                                "filing_date": str(row["filingDate"]),
                                "document": exhibit_name,
                                "historical_follow_up": (
                                    request.question_type == "SEC_FILING_HISTORY"
                                ),
                            },
                            cost_usd=Decimal("0"),
                        )
                    )
        return ResearchBatch(
            provider="sec",
            retrieved_at=retrieved,
            question_ids=(request.question_id,),
            documents=tuple(documents),
            request_count=submissions_attempts + facts_attempts + filing_attempts,
            response_bytes=(
                len(submissions_bytes) + len(facts_bytes or b"") + filing_response_bytes
            ),
            cost_usd=Decimal("0"),
        )

    def _fetch(self, url: str) -> tuple[bytes, int]:
        return _fetch_sec_payload(
            client=self.client,
            user_agent=self.user_agent,
            url=url,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            max_attempts=self.max_attempts,
            sleep=self.sleep,
        )


def _validated_user_agent(value: str) -> str:
    normalized = value.strip()
    if not normalized or "@" not in normalized:
        raise ValueError(
            "SEC user_agent must explicitly identify the application and contact email"
        )
    if len(normalized) > 200 or "\n" in normalized or "\r" in normalized:
        raise ValueError("SEC user_agent is invalid")
    return normalized


def _fetch_sec_payload(
    *,
    client: httpx.Client,
    user_agent: str,
    url: str,
    timeout_seconds: float,
    max_response_bytes: int,
    max_attempts: int,
    sleep: Callable[[float], None],
) -> tuple[bytes, int]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip, deflate",
    }
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with client.stream(
                "GET",
                url,
                headers=headers,
                timeout=timeout_seconds,
            ) as response:
                if response.status_code in _TRANSIENT_STATUSES:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    raise SecHttpStatusError(url, response.status_code)
                else:
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise SecResearchError(
                                f"SEC endpoint {url} returned an invalid Content-Length"
                            ) from exc
                        if declared_length > max_response_bytes:
                            raise SecResearchError(
                                f"SEC endpoint {url} exceeds the response byte limit"
                            )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > max_response_bytes:
                            raise SecResearchError(
                                f"SEC endpoint {url} exceeds the response byte limit"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), attempt
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_attempts:
            sleep(0.25 * (2 ** (attempt - 1)))
    raise SecResearchError(
        f"SEC endpoint {url} unavailable after {max_attempts} attempts: {last_error}"
    )


def _validate_company_tickers(payload: Mapping[str, object]) -> dict[str, str]:
    if not payload:
        raise SecResearchError("SEC company tickers response cannot be empty")
    indexes: list[int] = []
    normalized: dict[str, str] = {}
    for raw_index, raw_entry in payload.items():
        if not raw_index.isdigit() or str(int(raw_index)) != raw_index:
            raise SecResearchError("SEC company tickers contains an invalid row index")
        indexes.append(int(raw_index))
        if not isinstance(raw_entry, Mapping):
            raise SecResearchError("SEC company tickers contains a non-object entry")
        if set(raw_entry) != {"cik_str", "ticker", "title"}:
            raise SecResearchError("SEC company tickers entry has an invalid schema")
        raw_cik = raw_entry["cik_str"]
        raw_ticker = raw_entry["ticker"]
        raw_title = raw_entry["title"]
        if isinstance(raw_cik, bool) or not isinstance(raw_cik, int) or raw_cik <= 0:
            raise SecResearchError("SEC company tickers entry has an invalid CIK")
        cik = str(raw_cik)
        if len(cik) > 10:
            raise SecResearchError("SEC company tickers entry has an invalid CIK")
        if not isinstance(raw_ticker, str) or not isinstance(raw_title, str):
            raise SecResearchError("SEC company tickers entry has invalid text fields")
        symbol = raw_ticker.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(symbol) or not raw_title.strip():
            raise SecResearchError("SEC company tickers entry has invalid text fields")
        if symbol in normalized:
            raise SecResearchError(f"SEC company tickers contains duplicate ticker {symbol}")
        normalized[symbol] = cik.zfill(10)
    if sorted(indexes) != list(range(len(indexes))):
        raise SecResearchError("SEC company tickers row indexes must be contiguous from zero")
    return dict(sorted(normalized.items()))


def _canonical_ticker_mapping(mapping: Mapping[str, str]) -> bytes:
    return json.dumps(
        mapping,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalize_cik_mapping(values: Mapping[str, str | int]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_symbol, raw_cik in values.items():
        symbol = raw_symbol.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"invalid SEC mapping symbol {raw_symbol!r}")
        if symbol in normalized:
            raise ValueError(f"duplicate SEC mapping symbol {symbol}")
        if isinstance(raw_cik, bool):
            raise ValueError(f"invalid SEC CIK for {symbol}")
        cik = str(raw_cik).strip()
        if not cik.isdigit() or not 1 <= len(cik) <= 10 or int(cik) <= 0:
            raise ValueError(f"invalid SEC CIK for {symbol}")
        normalized[symbol] = cik.zfill(10)
    return normalized


def _retrieval_time(value: datetime | None) -> datetime:
    retrieved = value or datetime.now(UTC)
    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return retrieved.astimezone(UTC)


def _load_object(content: bytes, description: str) -> dict[str, object]:
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SecResearchError(f"{description} returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SecResearchError(f"{description} response must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _validate_submissions(
    payload: Mapping[str, object],
    expected_cik: str,
    *,
    window_start: datetime,
    window_end: datetime,
    max_filings: int,
) -> tuple[list[dict[str, object]], str]:
    if _cik(payload.get("cik")) != expected_cik:
        raise SecResearchError("SEC submissions CIK does not match the configured symbol mapping")
    name = payload.get("name")
    filings = payload.get("filings")
    if not isinstance(name, str) or not name.strip() or not isinstance(filings, Mapping):
        raise SecResearchError("SEC submissions response omitted required entity fields")
    recent = filings.get("recent")
    if not isinstance(recent, Mapping):
        raise SecResearchError("SEC submissions response omitted filings.recent")
    required = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "form",
        "primaryDocument",
    )
    columns: dict[str, Sequence[object]] = {}
    for field in required:
        column = recent.get(field)
        if not isinstance(column, Sequence) or isinstance(column, (str, bytes)):
            raise SecResearchError(f"SEC submissions recent.{field} must be an array")
        columns[field] = column
    lengths = {len(column) for column in columns.values()}
    if len(lengths) != 1:
        raise SecResearchError("SEC submissions recent arrays have inconsistent lengths")

    rows: list[dict[str, object]] = []
    row_count = next(iter(lengths), 0)
    start = window_start.astimezone(UTC)
    end = window_end.astimezone(UTC)
    for index in range(row_count):
        row = {field: columns[field][index] for field in required}
        if not all(isinstance(row[field], str) for field in required):
            raise SecResearchError("SEC submissions recent arrays contain non-string values")
        accepted = _sec_datetime(str(row["acceptanceDateTime"]), "acceptanceDateTime")
        if start <= accepted < end:
            rows.append(row)
            if len(rows) >= max_filings:
                break
    return rows, name.strip()


def _select_primary_filing_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    limit: int,
    historical: bool,
) -> tuple[Mapping[str, object], ...]:
    """Choose a bounded set of substantive filings from SEC's newest-first list.

    History requests use a non-overlapping older time window, so both modes retain the
    newest substantive documents inside their own admitted window.
    """

    substantive = [row for row in rows if str(row.get("form")) in _SUBSTANTIVE_FORMS]
    del historical
    return tuple(substantive[:limit])


def _primary_document_url(cik: str, row: Mapping[str, object]) -> str:
    accession = str(row.get("accessionNumber", ""))
    document = str(row.get("primaryDocument", ""))
    if not _ACCESSION_PATTERN.fullmatch(accession):
        raise SecResearchError("SEC filing contains an invalid accession number")
    if not _DOCUMENT_PATTERN.fullmatch(document):
        raise SecResearchError("SEC filing contains an unsafe primary document name")
    return _archive_document_url(cik, accession, document)


def _archive_document_url(cik: str, accession: str, document: str) -> str:
    if not _ACCESSION_PATTERN.fullmatch(accession):
        raise SecResearchError("SEC filing contains an invalid accession number")
    if not _DOCUMENT_PATTERN.fullmatch(document):
        raise SecResearchError("SEC filing contains an unsafe document name")
    return SEC_ARCHIVE_DOCUMENT_URL.format(
        cik=str(int(cik)),
        accession=accession.replace("-", ""),
        document=document,
    )


def _filing_index_url(cik: str, accession: str) -> str:
    if not _ACCESSION_PATTERN.fullmatch(accession):
        raise SecResearchError("SEC filing contains an invalid accession number")
    return SEC_FILING_INDEX_URL.format(
        cik=str(int(cik)),
        accession=accession.replace("-", ""),
        accession_hyphenated=accession,
    )


class _FilingIndexExtractor(HTMLParser):
    """Extract the first issuer exhibit from the SEC filing-detail table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_row = False
        self._parts: list[str] = []
        self._links: list[str] = []
        self.rows: list[tuple[str, tuple[str, ...]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() == "tr":
            self._in_row = True
            self._parts = []
            self._links = []
        if tag.lower() == "a" and self._in_row:
            href = next((value for name, value in attrs if name.lower() == "href"), None)
            if href is not None:
                self._links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "tr" and self._in_row:
            self.rows.append((" ".join(self._parts), tuple(self._links)))
            self._in_row = False

    def handle_data(self, data: str) -> None:
        if self._in_row:
            text = " ".join(data.split())
            if text:
                self._parts.append(text)


def _issuer_exhibit_reference(payload: bytes) -> tuple[str, str] | None:
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        decoded = payload.decode("latin-1")
    parser = _FilingIndexExtractor()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise SecResearchError(f"SEC filing index contains invalid HTML: {exc}") from exc
    for text, links in parser.rows:
        match = re.search(r"\bEX-99(?:\.[0-9]+)?\b", text, re.IGNORECASE)
        if match is None:
            continue
        for href in links:
            document = href.rsplit("/", 1)[-1]
            if _DOCUMENT_PATTERN.fullmatch(document):
                return match.group(0).upper(), document
        raise SecResearchError("SEC issuer exhibit row contains no safe document link")
    return None


class _FilingTextExtractor(HTMLParser):
    """Small deterministic HTML-to-text projection; exact source bytes remain retained."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag.lower() in {"script", "style", "noscript", "ix:hidden"}:
            self._ignored_depth += 1
        elif tag.lower() in {"td", "th"}:
            self.parts.append("\t")
        elif tag.lower() in {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "ix:hidden"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif tag.lower() in {"td", "th"}:
            self.parts.append("\t")
        elif tag.lower() in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def _normalized_filing_text(payload: bytes, *, max_chars: int = 500_000) -> str:
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        decoded = payload.decode("latin-1")
    extractor = _FilingTextExtractor()
    try:
        extractor.feed(decoded)
        extractor.close()
    except Exception as exc:
        raise SecResearchError(f"SEC primary filing contains invalid HTML: {exc}") from exc
    text = "\n".join(
        line for line in (" ".join(part.split()) for part in extractor.parts) if line
    ).strip()
    if not text:
        text = " ".join(decoded.split()).strip()
    if not text:
        raise SecResearchError("SEC primary filing document contains no readable text")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[normalized text truncated; exact payload retained]"


def _validate_companyfacts(
    payload: Mapping[str, object],
    expected_cik: str,
    retrieved_at: datetime,
) -> tuple[dict[str, JsonValue], str, datetime]:
    if _cik(payload.get("cik")) != expected_cik:
        raise SecResearchError("SEC companyfacts CIK does not match the configured symbol mapping")
    name = payload.get("entityName")
    facts = payload.get("facts")
    if not isinstance(name, str) or not name.strip() or not isinstance(facts, Mapping):
        raise SecResearchError("SEC companyfacts response omitted required entity fields")
    concept_count = 0
    latest_filed: date | None = None
    concepts: dict[str, list[str]] = {}
    for taxonomy, raw_concepts in facts.items():
        if not isinstance(taxonomy, str) or not isinstance(raw_concepts, Mapping):
            raise SecResearchError("SEC companyfacts facts contain an invalid taxonomy")
        names: list[str] = []
        for concept_name, concept in raw_concepts.items():
            if not isinstance(concept_name, str) or not isinstance(concept, Mapping):
                raise SecResearchError("SEC companyfacts contain an invalid concept")
            concept_count += 1
            names.append(concept_name)
            units = concept.get("units")
            if not isinstance(units, Mapping):
                raise SecResearchError("SEC companyfacts concept omitted units")
            for values in units.values():
                if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                    raise SecResearchError("SEC companyfacts unit values must be arrays")
                for fact in values:
                    if not isinstance(fact, Mapping):
                        raise SecResearchError("SEC companyfacts unit contains an invalid fact")
                    filed = fact.get("filed")
                    if filed is not None:
                        parsed = _sec_date(filed, "companyfacts filed")
                        latest_filed = max(latest_filed, parsed) if latest_filed else parsed
        concepts[taxonomy] = sorted(names)
    published_at = (
        datetime.combine(latest_filed, datetime_time.min, tzinfo=UTC)
        if latest_filed is not None
        else retrieved_at
    )
    if published_at > retrieved_at:
        raise SecResearchError("SEC companyfacts contain a filed date after retrieval")
    return (
        {
            "taxonomy_count": len(facts),
            "concept_count": concept_count,
            "latest_filed_date": latest_filed.isoformat() if latest_filed else None,
            "concepts": cast("JsonValue", concepts),
        },
        name.strip(),
        published_at,
    )


def _latest_submission_timestamp(
    rows: Sequence[Mapping[str, object]],
    retrieved_at: datetime,
) -> datetime:
    if not rows:
        return retrieved_at
    latest = max(
        _sec_datetime(str(row["acceptanceDateTime"]), "acceptanceDateTime") for row in rows
    )
    if latest > retrieved_at:
        raise SecResearchError("SEC submissions contain an acceptance time after retrieval")
    return latest


def _cik(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SecResearchError("SEC response contains an invalid CIK")
    raw = str(value).strip()
    if not raw.isdigit() or not 1 <= len(raw) <= 10:
        raise SecResearchError("SEC response contains an invalid CIK")
    return raw.zfill(10)


def _sec_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SecResearchError(f"SEC {field} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SecResearchError(f"SEC {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _sec_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise SecResearchError(f"SEC {field} is not a string date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SecResearchError(f"SEC {field} is not a valid date") from exc


def _provider_item_id(kind: str, cik: str, payload: bytes) -> str:
    return f"{kind}:CIK{cik}:{hashlib.sha256(payload).hexdigest()}"


def _normalized_json(value: object, *, max_chars: int = 100_000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[normalized text truncated; exact payload retained]"
