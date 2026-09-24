"""Strict, provider-neutral contracts for deterministic research collection."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from trader.agent.models import SYMBOL_PATTERN

type ProviderName = Literal["alpaca", "sec"]
type QuestionType = Literal[
    "MARKET_CONTEXT",
    "COMPANY_NEWS",
    "SEC_FILINGS",
    "SEC_FILING_HISTORY",
]
type SourceType = Literal["MARKET_DATA", "NEWS", "REGULATORY_FILING"]
type SourceTier = Literal["BROKER", "PRIMARY"]

MAX_RAW_DOCUMENT_BYTES = 10_000_000
MAX_NORMALIZED_TEXT_CHARS = 1_000_000
MAX_BATCH_BYTES = 100_000_000
MAX_METADATA_BYTES = 100_000
HASH_PATTERN = r"^[0-9a-f]{64}$"


class ResearchModel(BaseModel):
    """Immutable, strict base model for values crossing research boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _normalized_symbol(value: str) -> str:
    normalized = value.upper().strip()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("invalid research symbol")
    return normalized


def _nonblank(value: str, *, label: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} cannot be blank")
    return stripped


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def research_question_id(
    *,
    symbol: str,
    question_type: QuestionType,
    query: str,
    window_start: datetime,
    window_end: datetime,
    priority: int,
) -> str:
    """Return the stable identity of one fully normalized research question."""

    payload = {
        "priority": priority,
        "query": query,
        "question_type": question_type,
        "symbol": symbol,
        "window_end": window_end.astimezone(UTC).isoformat(),
        "window_start": window_start.astimezone(UTC).isoformat(),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


class ResearchRequest(ResearchModel):
    """One bounded provider-neutral question with a reproducible identifier."""

    question_id: str = Field(pattern=HASH_PATTERN)
    symbol: str
    question_type: QuestionType
    query: str = Field(min_length=1, max_length=500)
    window_start: datetime
    window_end: datetime
    priority: int = Field(ge=1, le=100)

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        return _nonblank(value, label="research query")

    @field_validator("priority", mode="before")
    @classmethod
    def actual_priority_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("research priority must be an integer")
        return value

    @field_validator("window_start", "window_end")
    @classmethod
    def aware_window_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="research window timestamp")

    @model_validator(mode="after")
    def coherent_window_and_identity(self) -> "ResearchRequest":
        if self.window_start >= self.window_end:
            raise ValueError("research window_start must precede window_end")
        expected = research_question_id(
            symbol=self.symbol,
            question_type=self.question_type,
            query=self.query,
            window_start=self.window_start,
            window_end=self.window_end,
            priority=self.priority,
        )
        if self.question_id != expected:
            raise ValueError("question_id does not match the normalized research question")
        return self

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        question_type: QuestionType,
        query: str,
        window_start: datetime,
        window_end: datetime,
        priority: int,
    ) -> "ResearchRequest":
        normalized_symbol = _normalized_symbol(symbol)
        normalized_query = _nonblank(query, label="research query")
        normalized_start = _aware_utc(window_start, label="research window_start")
        normalized_end = _aware_utc(window_end, label="research window_end")
        question_id = research_question_id(
            symbol=normalized_symbol,
            question_type=question_type,
            query=normalized_query,
            window_start=normalized_start,
            window_end=normalized_end,
            priority=priority,
        )
        return cls(
            question_id=question_id,
            symbol=normalized_symbol,
            question_type=question_type,
            query=normalized_query,
            window_start=normalized_start,
            window_end=normalized_end,
            priority=priority,
        )


# Both names are exposed because planners speak in questions while collectors accept requests.
ResearchQuestion = ResearchRequest


def research_document_id(
    *,
    provider: ProviderName,
    source_name: str,
    provider_item_id: str,
    content_hash: str,
) -> str:
    payload = {
        "content_hash": content_hash,
        "provider": provider,
        "provider_item_id": provider_item_id,
        "source_name": source_name,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


class ResearchDocument(ResearchModel):
    """Normalized evidence plus the exact immutable provider payload."""

    research_id: str = Field(pattern=HASH_PATTERN)
    question_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    provider: ProviderName
    source_type: SourceType
    source_tier: SourceTier
    source_name: str = Field(min_length=1, max_length=200)
    provider_item_id: str = Field(min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2_000)
    author: str | None = Field(default=None, max_length=500)
    published_at: datetime
    retrieved_at: datetime
    symbols: tuple[str, ...] = Field(min_length=1, max_length=100)
    headline: str = Field(min_length=1, max_length=2_000)
    normalized_text: str = Field(min_length=1, max_length=MAX_NORMALIZED_TEXT_CHARS)
    summary: str | None = Field(default=None, max_length=20_000)
    raw_payload: bytes = Field(min_length=1, max_length=MAX_RAW_DOCUMENT_BYTES)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    content_hash: str = Field(pattern=HASH_PATTERN)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)

    @field_validator("question_ids")
    @classmethod
    def unique_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("research document question_ids cannot contain duplicates")
        if any(
            len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("research document contains an invalid question_id")
        return values

    @field_validator("source_name", "provider_item_id", "headline", "normalized_text")
    @classmethod
    def nonblank_text(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "research text")
        return _nonblank(value, label=str(field_name))

    @field_validator("summary", "author")
    @classmethod
    def optional_nonblank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _nonblank(value, label="optional research text")

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _nonblank(value, label="research URL")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("research URL must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("research URL cannot contain credentials")
        return normalized

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def aware_document_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="research document timestamp")

    @field_validator("symbols")
    @classmethod
    def valid_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalized_symbol(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("research document symbols cannot contain duplicates")
        return normalized

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if any(not key.strip() for key in value):
            raise ValueError("research metadata keys cannot be blank")
        if len(_canonical_json(value)) > MAX_METADATA_BYTES:
            raise ValueError("research metadata exceeds the hard size limit")
        return value

    @field_validator("cost_usd")
    @classmethod
    def finite_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("research cost must be finite")
        return value

    @model_validator(mode="after")
    def coherent_source_timestamps_and_hashes(self) -> "ResearchDocument":
        if self.published_at > self.retrieved_at:
            raise ValueError("research document cannot be published after retrieval")
        if self.provider == "alpaca":
            if self.source_tier != "BROKER" or self.source_type == "REGULATORY_FILING":
                raise ValueError("Alpaca documents must be broker market data or news")
        elif self.source_tier != "PRIMARY" or self.source_type != "REGULATORY_FILING":
            raise ValueError("SEC documents must be primary regulatory filings")
        expected_content_hash = hashlib.sha256(self.raw_payload).hexdigest()
        if self.content_hash != expected_content_hash:
            raise ValueError("content_hash does not match raw_payload")
        expected_research_id = research_document_id(
            provider=self.provider,
            source_name=self.source_name,
            provider_item_id=self.provider_item_id,
            content_hash=self.content_hash,
        )
        if self.research_id != expected_research_id:
            raise ValueError("research_id does not match the normalized document")
        return self

    @classmethod
    def create(
        cls,
        *,
        question_ids: tuple[str, ...],
        provider: ProviderName,
        source_type: SourceType,
        source_tier: SourceTier,
        source_name: str,
        provider_item_id: str,
        url: str | None,
        author: str | None,
        published_at: datetime,
        retrieved_at: datetime,
        symbols: tuple[str, ...],
        headline: str,
        normalized_text: str,
        summary: str | None,
        raw_payload: bytes,
        metadata: dict[str, JsonValue] | None = None,
        cost_usd: Decimal = Decimal("0"),
    ) -> "ResearchDocument":
        normalized_source_name = _nonblank(source_name, label="source_name")
        normalized_item_id = _nonblank(provider_item_id, label="provider_item_id")
        content_hash = hashlib.sha256(raw_payload).hexdigest()
        research_id = research_document_id(
            provider=provider,
            source_name=normalized_source_name,
            provider_item_id=normalized_item_id,
            content_hash=content_hash,
        )
        return cls(
            research_id=research_id,
            question_ids=question_ids,
            provider=provider,
            source_type=source_type,
            source_tier=source_tier,
            source_name=normalized_source_name,
            provider_item_id=normalized_item_id,
            url=url,
            author=author,
            published_at=published_at,
            retrieved_at=retrieved_at,
            symbols=symbols,
            headline=headline,
            normalized_text=normalized_text,
            summary=summary,
            raw_payload=raw_payload,
            metadata={} if metadata is None else metadata,
            content_hash=content_hash,
            cost_usd=cost_usd,
        )


CollectedResearchItem = ResearchDocument


class ResearchBatch(ResearchModel):
    """One provider collection response, including a valid empty result."""

    provider: ProviderName
    retrieved_at: datetime
    question_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    documents: tuple[ResearchDocument, ...] = Field(max_length=1_000)
    request_count: int = Field(ge=1, le=1_000)
    response_bytes: int = Field(ge=0, le=MAX_BATCH_BYTES)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)

    @field_validator("retrieved_at")
    @classmethod
    def aware_retrieval_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="research batch retrieval timestamp")

    @field_validator("request_count", "response_bytes", mode="before")
    @classmethod
    def actual_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("research batch counts must be integers")
        return value

    @field_validator("question_ids")
    @classmethod
    def unique_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("research batch question_ids cannot contain duplicates")
        if any(
            len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("research batch contains an invalid question_id")
        return values

    @field_validator("cost_usd")
    @classmethod
    def finite_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("research batch cost must be finite")
        return value

    @model_validator(mode="after")
    def coherent_documents(self) -> "ResearchBatch":
        research_ids = [document.research_id for document in self.documents]
        if len(research_ids) != len(set(research_ids)):
            raise ValueError("research batch contains duplicate documents")
        provider_item_ids = [document.provider_item_id for document in self.documents]
        if len(provider_item_ids) != len(set(provider_item_ids)):
            raise ValueError("research batch contains duplicate provider_item_id values")
        admitted_question_ids = set(self.question_ids)
        if any(document.provider != self.provider for document in self.documents):
            raise ValueError("research batch document provider mismatch")
        if any(document.retrieved_at > self.retrieved_at for document in self.documents):
            raise ValueError("research document retrieval cannot be after its batch")
        if any(
            not set(document.question_ids).issubset(admitted_question_ids)
            for document in self.documents
        ):
            raise ValueError("research document references a question outside its batch")
        raw_bytes = sum(len(document.raw_payload) for document in self.documents)
        if self.response_bytes < raw_bytes:
            raise ValueError("response_bytes cannot be smaller than retained raw payloads")
        document_cost = sum((document.cost_usd for document in self.documents), Decimal("0"))
        if self.cost_usd < document_cost:
            raise ValueError("batch cost cannot be smaller than document costs")
        return self


class ResearchPlan(ResearchModel):
    """A complete deterministic research plan for one immutable universe scan."""

    as_of: datetime
    candidate_symbols: tuple[str, ...] = Field(min_length=1, max_length=50)
    deep_symbols: tuple[str, ...] = Field(max_length=12)
    questions: tuple[ResearchQuestion, ...] = Field(min_length=1, max_length=100)

    @field_validator("as_of")
    @classmethod
    def aware_plan_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="research plan timestamp")

    @field_validator("candidate_symbols", "deep_symbols")
    @classmethod
    def unique_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalized_symbol(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("research plan symbol lists cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def coherent_plan(self) -> "ResearchPlan":
        candidate_symbols = set(self.candidate_symbols)
        if not set(self.deep_symbols).issubset(candidate_symbols):
            raise ValueError("deep research symbols must be candidates")
        question_ids = [question.question_id for question in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("research plan contains duplicate questions")
        if any(question.symbol not in candidate_symbols for question in self.questions):
            raise ValueError("research question symbol must be a plan candidate")
        if any(question.window_end > self.as_of for question in self.questions):
            raise ValueError("research questions cannot include future information")
        return self
