"""Audited shadow research orchestration; this module has no execution access."""

import hashlib
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from trader.persistence.repositories import persist_research_item
from trader.research.artifacts import ImmutableResearchArtifactWriter, ResearchArtifact
from trader.research.collection import (
    BoundedResearchCollector,
    ResearchCollection,
    write_research_artifacts,
)
from trader.research.config import ResearchConfig
from trader.research.models import ResearchDocument, ResearchPlan
from trader.universe.models import UniverseScan


class ResearchPipelineError(RuntimeError):
    """Raised when a research run violates its deterministic policy."""


class ResearchPlanBuilder(Protocol):
    def plan(
        self,
        scan: UniverseScan,
        *,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchPlan: ...


class ResearchPipeline(Protocol):
    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        scan: UniverseScan,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> "ResearchRunResult": ...


@dataclass(frozen=True)
class ResearchRunResult:
    plan: ResearchPlan
    collection: ResearchCollection
    artifacts: dict[str, ResearchArtifact]
    persisted_research_ids: tuple[str, ...]
    elapsed_seconds: float
    reference_artifacts: dict[str, ResearchArtifact] | None = None
    setup_request_count: int = 0
    setup_response_bytes: int = 0

    @property
    def unique_document_count(self) -> int:
        return len(self.artifacts)

    @property
    def total_request_count(self) -> int:
        return self.setup_request_count + self.collection.request_count

    @property
    def total_response_bytes(self) -> int:
        return self.setup_response_bytes + self.collection.response_bytes

    def summary(self) -> dict[str, object]:
        return {
            "as_of": self.plan.as_of.isoformat(),
            "mode": "shadow",
            "candidate_count": len(self.plan.candidate_symbols),
            "deep_symbol_count": len(self.plan.deep_symbols),
            "question_count": len(self.plan.questions),
            "provider_batches": len(self.collection.batches),
            "http_request_count": self.total_request_count,
            "response_bytes": self.total_response_bytes,
            "reference_artifact_count": len(self.reference_artifacts or {}),
            "unique_document_count": self.unique_document_count,
            "persisted_research_ids": list(self.persisted_research_ids),
            "elapsed_seconds": round(self.elapsed_seconds, 6),
        }


class ShadowResearchPipeline:
    """Plan, collect, retain, and persist evidence without invoking a model."""

    def __init__(
        self,
        session: Session,
        planner: ResearchPlanBuilder,
        collector: BoundedResearchCollector,
        config: ResearchConfig,
        *,
        reference_payloads: Mapping[str, bytes] | None = None,
        setup_request_count: int = 0,
        setup_response_bytes: int = 0,
        setup_elapsed_seconds: float = 0,
    ) -> None:
        if config.mode != "shadow":
            raise ValueError("only shadow research is implemented")
        self.session = session
        self.planner = planner
        self.collector = collector
        self.config = config
        self.reference_payloads = dict(reference_payloads or {})
        if setup_request_count < 0 or setup_response_bytes < 0:
            raise ValueError("research setup counts cannot be negative")
        if setup_elapsed_seconds < 0:
            raise ValueError("research setup elapsed time cannot be negative")
        self.setup_request_count = setup_request_count
        self.setup_response_bytes = setup_response_bytes
        self.setup_elapsed_seconds = setup_elapsed_seconds

    def run(
        self,
        *,
        run_id: str,
        run_directory: Path,
        scan: UniverseScan,
        portfolio_symbols: tuple[str, ...] = (),
        event_symbols: tuple[str, ...] = (),
    ) -> ResearchRunResult:
        started = time.monotonic()
        plan = self.planner.plan(
            scan,
            portfolio_symbols=portfolio_symbols,
            event_symbols=event_symbols,
        )
        # The plan is pinned to the scan cutoff, while retrieval records the later
        # wall-clock instant at which the application actually obtained the evidence.
        collection = self.collector.collect(plan.questions)
        elapsed = self.setup_elapsed_seconds + (time.monotonic() - started)
        self._validate_collection(collection, elapsed)

        writer = ImmutableResearchArtifactWriter(run_directory / "research")
        reference_artifacts = {
            path: writer.write_bytes(path, payload)
            for path, payload in self.reference_payloads.items()
        }
        artifacts = write_research_artifacts(
            collection,
            writer,
        )
        collection_artifact = writer.write_json(
            "research_collection.json",
            _collection_manifest(collection, artifacts),
        )
        reference_artifacts["research_collection.json"] = collection_artifact
        questions = {question.question_id: question for question in plan.questions}
        question_order = tuple(question.question_id for question in plan.questions)
        persisted_ids: list[str] = []
        seen_persisted: set[str] = set()
        for content_hash, documents in sorted(_documents_by_content(collection).items()):
            canonical = min(
                documents,
                key=lambda document: (
                    document.provider,
                    document.source_name,
                    document.provider_item_id,
                    document.research_id,
                ),
            )
            artifact = artifacts[canonical.research_id]
            database_id = _run_scoped_research_id(run_id, content_hash)
            symbols = tuple(
                sorted({symbol for document in documents for symbol in document.symbols})
            )
            admitted_question_ids = {
                question_id
                for document in documents
                for question_id in document.question_ids
            }
            unknown_question_ids = admitted_question_ids.difference(questions)
            if unknown_question_ids:
                raise ResearchPipelineError(
                    f"content {content_hash} references unknown questions: "
                    + ", ".join(sorted(unknown_question_ids))
                )
            source_observations = [
                {
                    "research_id": document.research_id,
                    "provider": document.provider,
                    "source_name": document.source_name,
                    "provider_item_id": document.provider_item_id,
                    "headline": document.headline,
                    "symbols": list(document.symbols),
                    "question_ids": list(document.question_ids),
                    "raw_artifact_path": (
                        f"research/{artifacts[document.research_id].relative_path}"
                    ),
                }
                for document in sorted(documents, key=lambda item: item.research_id)
            ]
            canonical_headline = (
                canonical.headline
                if len(source_observations) == 1
                else (
                    f"Deduplicated identical {canonical.source_type.lower()} payload "
                    f"observed in {len(source_observations)} research contexts"
                )
            )
            for question_id in question_order:
                if question_id not in admitted_question_ids:
                    continue
                question = questions[question_id]
                record = persist_research_item(
                    self.session,
                    research_id=database_id,
                    run_id=run_id,
                    symbols=symbols,
                    source_tier=canonical.source_tier,
                    source_type=canonical.source_type,
                    source_name=canonical.source_name,
                    provider=canonical.provider,
                    provider_item_id=canonical.provider_item_id,
                    research_question_id=question.question_id,
                    research_question=question.query,
                    raw_artifact_path=f"research/{artifact.relative_path}",
                    normalized_summary=canonical.summary or canonical_headline,
                    normalized_text=canonical.normalized_text,
                    published_at=canonical.published_at,
                    retrieved_at=canonical.retrieved_at,
                    content_hash=canonical.content_hash,
                    headline=canonical_headline,
                    url=canonical.url,
                    author=canonical.author,
                    metadata={
                        **canonical.metadata,
                        "collector_research_id": canonical.research_id,
                        "source_observations": source_observations,
                    },
                    cost_usd=canonical.cost_usd,
                )
                if record.id not in seen_persisted:
                    persisted_ids.append(record.id)
                    seen_persisted.add(record.id)

        elapsed = self.setup_elapsed_seconds + (time.monotonic() - started)
        if elapsed > self.config.collection.max_wall_clock_seconds:
            raise ResearchPipelineError("research pipeline exceeded its wall-clock budget")

        return ResearchRunResult(
            plan=plan,
            collection=collection,
            artifacts=artifacts,
            persisted_research_ids=tuple(persisted_ids),
            elapsed_seconds=elapsed,
            reference_artifacts=reference_artifacts,
            setup_request_count=self.setup_request_count,
            setup_response_bytes=self.setup_response_bytes,
        )

    def _validate_collection(
        self,
        collection: ResearchCollection,
        elapsed_seconds: float,
    ) -> None:
        policy = self.config.collection
        if elapsed_seconds > policy.max_wall_clock_seconds:
            raise ResearchPipelineError("research collection exceeded its wall-clock budget")
        if self.setup_request_count + collection.request_count > policy.max_total_requests:
            raise ResearchPipelineError("research collection exceeded its request budget")
        if (
            self.setup_response_bytes + collection.response_bytes
            > policy.max_total_response_bytes
        ):
            raise ResearchPipelineError("research collection exceeded its response-byte budget")

        documents = _unique_documents(collection)
        if len(documents) > policy.max_total_items:
            raise ResearchPipelineError("research collection exceeded its item budget")
        by_symbol: dict[str, set[str]] = defaultdict(set)
        for document in collection.documents:
            for symbol in document.symbols:
                by_symbol[symbol].add(document.research_id)
        if any(
            len(research_ids) > policy.max_items_per_symbol
            for research_ids in by_symbol.values()
        ):
            raise ResearchPipelineError("research collection exceeded its per-symbol item budget")

        cost = sum((batch.cost_usd for batch in collection.batches), Decimal("0"))
        if cost > self.config.paid.max_per_run_usd:
            raise ResearchPipelineError("research collection exceeded its paid-provider budget")


def _unique_documents(collection: ResearchCollection) -> dict[str, ResearchDocument]:
    documents: dict[str, ResearchDocument] = {}
    for document in collection.documents:
        existing = documents.get(document.research_id)
        if existing is not None and (
            existing.content_hash != document.content_hash
            or existing.raw_payload != document.raw_payload
        ):
            raise ResearchPipelineError(
                f"collector reused research ID {document.research_id} for different content"
            )
        documents.setdefault(document.research_id, document)
    return documents


def _run_scoped_research_id(run_id: str, content_hash: str) -> str:
    return hashlib.sha256(f"{run_id}\0{content_hash}".encode()).hexdigest()


def _documents_by_content(
    collection: ResearchCollection,
) -> dict[str, list[ResearchDocument]]:
    grouped: dict[str, list[ResearchDocument]] = defaultdict(list)
    for document in collection.documents:
        grouped[document.content_hash].append(document)
    return grouped


def _collection_manifest(
    collection: ResearchCollection,
    artifacts: Mapping[str, ResearchArtifact],
) -> dict[str, object]:
    return {
        "request_count": collection.request_count,
        "response_bytes": collection.response_bytes,
        "batches": [
            {
                "provider": batch.provider,
                "status": "COLLECTED" if batch.documents else "EMPTY",
                "retrieved_at": batch.retrieved_at.isoformat(),
                "question_ids": list(batch.question_ids),
                "request_count": batch.request_count,
                "response_bytes": batch.response_bytes,
                "cost_usd": str(batch.cost_usd),
                "documents": [
                    {
                        "research_id": document.research_id,
                        "content_hash": document.content_hash,
                        "provider_item_id": document.provider_item_id,
                        "question_ids": list(document.question_ids),
                        "symbols": list(document.symbols),
                        "published_at": document.published_at.isoformat(),
                        "retrieved_at": document.retrieved_at.isoformat(),
                        "raw_artifact_path": (
                            f"research/{artifacts[document.research_id].relative_path}"
                        ),
                    }
                    for document in batch.documents
                ],
            }
            for batch in collection.batches
        ],
    }
