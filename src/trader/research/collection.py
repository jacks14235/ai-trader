"""Provider-neutral, bounded orchestration for research collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from trader.research.artifacts import ImmutableResearchArtifactWriter, ResearchArtifact
from trader.research.models import (
    ProviderName,
    QuestionType,
    ResearchBatch,
    ResearchDocument,
    ResearchRequest,
)


class ResearchCollectionError(RuntimeError):
    """Raised when collection fails or exceeds a deterministic boundary."""


class ResearchProvider(Protocol):
    """Narrow read-only boundary implemented by each admitted research source."""

    provider_name: ProviderName
    supported_question_types: frozenset[QuestionType]

    def estimated_request_count(self, request: ResearchRequest) -> int: ...

    def collect(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchBatch: ...


@dataclass(frozen=True)
class ResearchCollection:
    batches: tuple[ResearchBatch, ...]
    request_count: int
    response_bytes: int

    @property
    def documents(self) -> tuple[ResearchDocument, ...]:
        return tuple(document for batch in self.batches for document in batch.documents)


class BoundedResearchCollector:
    """Route validated questions without giving providers broker, DB, or order access."""

    def __init__(
        self,
        providers: Mapping[QuestionType, ResearchProvider],
        *,
        max_questions: int,
        max_http_requests: int,
        max_documents: int,
        max_response_bytes: int,
    ) -> None:
        if not 1 <= max_questions <= 500:
            raise ValueError("max_questions must be between 1 and 500")
        if not 1 <= max_http_requests <= 2_000:
            raise ValueError("max_http_requests must be between 1 and 2,000")
        if not 1 <= max_documents <= 10_000:
            raise ValueError("max_documents must be between 1 and 10,000")
        if not 1 <= max_response_bytes <= 500_000_000:
            raise ValueError("max_response_bytes must be between 1 and 500,000,000")
        missing = set(providers) - {
            "MARKET_CONTEXT",
            "COMPANY_NEWS",
            "SEC_FILINGS",
        }
        if missing:
            raise ValueError(f"unsupported research routes: {sorted(missing)}")
        for question_type, provider in providers.items():
            if question_type not in provider.supported_question_types:
                raise ValueError(
                    f"provider {provider.provider_name} does not support {question_type}"
                )
        self.providers = dict(providers)
        self.max_questions = max_questions
        self.max_http_requests = max_http_requests
        self.max_documents = max_documents
        self.max_response_bytes = max_response_bytes

    def collect(
        self,
        requests: Sequence[ResearchRequest],
        *,
        retrieved_at: datetime | None = None,
    ) -> ResearchCollection:
        collected_at = retrieved_at
        if collected_at is not None:
            if collected_at.tzinfo is None or collected_at.utcoffset() is None:
                raise ValueError("retrieved_at must be timezone-aware")
            collected_at = collected_at.astimezone(UTC)
        if len(requests) > self.max_questions:
            raise ResearchCollectionError(
                f"research plan has {len(requests)} questions; limit is {self.max_questions}"
            )
        identifiers = [request.question_id for request in requests]
        if len(identifiers) != len(set(identifiers)):
            raise ResearchCollectionError("research plan contains duplicate question IDs")

        batches: list[ResearchBatch] = []
        request_count = 0
        response_bytes = 0
        document_count = 0
        for request in requests:
            provider = self.providers.get(request.question_type)
            if provider is None:
                raise ResearchCollectionError(
                    f"no provider configured for {request.question_type}"
                )
            estimate = provider.estimated_request_count(request)
            if estimate < 1:
                raise ResearchCollectionError(
                    f"provider {provider.provider_name} returned an invalid request estimate"
                )
            if request_count + estimate > self.max_http_requests:
                raise ResearchCollectionError("research HTTP request budget would be exceeded")

            batch = provider.collect(request, retrieved_at=collected_at)
            if batch.provider != provider.provider_name:
                raise ResearchCollectionError("provider returned a batch under the wrong name")
            if batch.question_ids != (request.question_id,):
                raise ResearchCollectionError("provider batch does not match its research question")
            if batch.request_count > estimate:
                raise ResearchCollectionError(
                    "provider exceeded its declared HTTP request estimate"
                )
            request_count += batch.request_count
            response_bytes += batch.response_bytes
            document_count += len(batch.documents)
            if request_count > self.max_http_requests:
                raise ResearchCollectionError("research HTTP request budget exceeded")
            if response_bytes > self.max_response_bytes:
                raise ResearchCollectionError("research response byte budget exceeded")
            if document_count > self.max_documents:
                raise ResearchCollectionError("research document budget exceeded")
            batches.append(batch)

        return ResearchCollection(
            batches=tuple(batches),
            request_count=request_count,
            response_bytes=response_bytes,
        )


def write_research_artifacts(
    collection: ResearchCollection,
    writer: ImmutableResearchArtifactWriter,
) -> dict[str, ResearchArtifact]:
    """Persist exact raw document payloads under deterministic, manifest-ready paths."""
    artifacts: dict[str, ResearchArtifact] = {}
    for batch in collection.batches:
        for document in batch.documents:
            path = f"{batch.provider}/{document.research_id}.json"
            artifact = writer.write_bytes(path, document.raw_payload)
            # Identical evidence may satisfy several questions or symbol batches. The
            # immutable writer verifies its bytes and makes that reuse idempotent.
            artifacts.setdefault(document.research_id, artifact)
    return artifacts
