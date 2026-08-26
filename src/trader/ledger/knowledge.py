"""Proposal-and-approval path for the only knowledge a model may ask to change.

A weekly review writes a *proposal*. Nothing in a run applies it. A human resolves it through an
explicit command, and both the proposal and its resolution are appended to ``knowledge_changes``,
so the strategy document's history is reconstructable from the database rather than from git alone.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.agent.weekly import ProposedStrategyChange, StrategyRecommendation
from trader.ledger.models import StrategyVersion
from trader.persistence.models import KnowledgeChange
from trader.persistence.repositories import PersistenceConflictError

STRATEGY_ENTITY_TYPE = "strategy"
STRATEGY_CHANGE_ENTITY_TYPE = "strategy_change"

STRATEGY_CHANGE_PROPOSED = "STRATEGY_CHANGE_PROPOSED"
STRATEGY_REVIEW_NO_CHANGE = "STRATEGY_REVIEW_NO_CHANGE"
STRATEGY_CHANGE_APPROVED = "STRATEGY_CHANGE_APPROVED"
STRATEGY_CHANGE_REJECTED = "STRATEGY_CHANGE_REJECTED"

REVIEW_CHANGE_TYPES = frozenset({STRATEGY_CHANGE_PROPOSED, STRATEGY_REVIEW_NO_CHANGE})
RESOLUTION_CHANGE_TYPES = frozenset({STRATEGY_CHANGE_APPROVED, STRATEGY_CHANGE_REJECTED})
MAX_REASON_CHARS = 20_000


@dataclass(frozen=True)
class StrategyProposal:
    """A recorded proposal and everything a human needs to decide on it."""

    change_id: str
    run_id: str
    strategy_id: str
    created_at: datetime
    current_text: str
    replacement_text: str
    reason: str

    def summary(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "created_at": self.created_at.isoformat(),
            "current_text": self.current_text,
            "replacement_text": self.replacement_text,
            "reason": self.reason,
        }


def record_strategy_review(
    session: Session,
    *,
    run_id: str,
    strategy_version: StrategyVersion,
    recommendation: StrategyRecommendation,
    as_of: datetime,
) -> tuple[str, ...]:
    """Append one review outcome, refusing to record a second review for the same run."""
    if _has_change(session, run_id=run_id, change_types=REVIEW_CHANGE_TYPES):
        raise PersistenceConflictError(f"strategy review already recorded for run {run_id}")
    citations = sorted(
        {
            *recommendation.cited_run_ids,
            *recommendation.cited_proposal_ids,
            *recommendation.cited_thesis_ids,
        }
    )
    if recommendation.status == "NO_CHANGE":
        record = KnowledgeChange(
            run_id=run_id,
            entity_type=STRATEGY_ENTITY_TYPE,
            entity_id=strategy_version.strategy_id,
            change_type=STRATEGY_REVIEW_NO_CHANGE,
            before_text="",
            after_text="",
            reason=_truncate(
                f"{recommendation.diagnosis}\n\n"
                f"Process assessment: {recommendation.process_assessment}\n\n"
                f"No change: {recommendation.no_change_reason or ''}",
                MAX_REASON_CHARS,
            ),
            evidence_ids_json=json.dumps(citations),
            created_at=as_of,
        )
        session.add(record)
        session.flush()
        change_id = record.id
        session.commit()
        return (change_id,)

    recorded: list[str] = []
    for change in recommendation.proposed_changes:
        record = KnowledgeChange(
            run_id=run_id,
            entity_type=STRATEGY_ENTITY_TYPE,
            entity_id=strategy_version.strategy_id,
            change_type=STRATEGY_CHANGE_PROPOSED,
            before_text=change.current_text,
            after_text=change.replacement_text,
            reason=_proposal_reason(recommendation, change),
            evidence_ids_json=json.dumps(citations),
            created_at=as_of,
        )
        session.add(record)
        session.flush()
        recorded.append(record.id)
    session.commit()
    return tuple(recorded)


def pending_strategy_proposals(session: Session) -> tuple[StrategyProposal, ...]:
    """Return proposals a human has neither approved nor rejected, oldest first."""
    resolved = set(
        session.scalars(
            select(KnowledgeChange.entity_id).where(
                KnowledgeChange.entity_type == STRATEGY_CHANGE_ENTITY_TYPE,
                KnowledgeChange.change_type.in_(sorted(RESOLUTION_CHANGE_TYPES)),
            )
        )
    )
    proposals = session.scalars(
        select(KnowledgeChange)
        .where(KnowledgeChange.change_type == STRATEGY_CHANGE_PROPOSED)
        .order_by(KnowledgeChange.created_at, KnowledgeChange.id)
    )
    return tuple(
        _proposal(record) for record in proposals if record.id not in resolved
    )


def get_strategy_proposal(session: Session, change_id: str) -> StrategyProposal:
    """Return one unresolved proposal, refusing anything already decided."""
    record = session.get(KnowledgeChange, change_id)
    if record is None or record.change_type != STRATEGY_CHANGE_PROPOSED:
        raise LookupError(f"strategy proposal not found: {change_id}")
    resolution = session.scalar(
        select(KnowledgeChange)
        .where(
            KnowledgeChange.entity_type == STRATEGY_CHANGE_ENTITY_TYPE,
            KnowledgeChange.entity_id == change_id,
            KnowledgeChange.change_type.in_(sorted(RESOLUTION_CHANGE_TYPES)),
        )
        .limit(1)
    )
    if resolution is not None:
        raise PersistenceConflictError(
            f"strategy proposal {change_id} was already {resolution.change_type}"
        )
    return _proposal(record)


def resolve_strategy_proposal(
    session: Session,
    *,
    proposal: StrategyProposal,
    approved: bool,
    reviewer: str,
    note: str,
    as_of: datetime,
    applied_content_hash: str | None = None,
) -> str:
    """Append a human decision about one proposal, which is the only way one is ever closed."""
    if approved and applied_content_hash is None:
        raise ValueError("an approved proposal must record the resulting document hash")
    if not reviewer.strip():
        raise ValueError("a strategy decision must record who made it")
    record = KnowledgeChange(
        run_id=proposal.run_id,
        entity_type=STRATEGY_CHANGE_ENTITY_TYPE,
        entity_id=proposal.change_id,
        change_type=STRATEGY_CHANGE_APPROVED if approved else STRATEGY_CHANGE_REJECTED,
        before_text=proposal.current_text,
        after_text=proposal.replacement_text if approved else "",
        reason=_truncate(
            f"Reviewer: {reviewer.strip()}\n"
            f"Note: {note.strip() or 'none'}\n"
            f"Applied document hash: {applied_content_hash or 'not applied'}",
            MAX_REASON_CHARS,
        ),
        evidence_ids_json=json.dumps([]),
        created_at=as_of,
    )
    session.add(record)
    session.flush()
    resolution_id = record.id
    session.commit()
    return resolution_id


def _proposal_reason(
    recommendation: StrategyRecommendation,
    change: ProposedStrategyChange,
) -> str:
    failure_modes = "\n".join(f"- {item}" for item in change.failure_modes) or "- none stated"
    return _truncate(
        "\n\n".join(
            [
                f"Section: {change.section_heading}",
                f"Diagnosis: {recommendation.diagnosis}",
                f"Process assessment: {recommendation.process_assessment}",
                f"Hypothesis: {change.hypothesis}",
                f"Disconfirming evidence: {change.disconfirming_evidence}",
                f"Expected effect: {change.expected_effect}",
                f"Failure modes:\n{failure_modes}",
                f"Evaluation plan: {change.evaluation_plan}",
                f"Revert criteria: {change.revert_criteria}",
            ]
        ),
        MAX_REASON_CHARS,
    )


def _proposal(record: KnowledgeChange) -> StrategyProposal:
    return StrategyProposal(
        change_id=record.id,
        run_id=record.run_id,
        strategy_id=record.entity_id,
        created_at=record.created_at,
        current_text=record.before_text,
        replacement_text=record.after_text,
        reason=record.reason,
    )


def _has_change(session: Session, *, run_id: str, change_types: frozenset[str]) -> bool:
    return (
        session.scalar(
            select(KnowledgeChange.id)
            .where(
                KnowledgeChange.run_id == run_id,
                KnowledgeChange.change_type.in_(sorted(change_types)),
            )
            .limit(1)
        )
        is not None
    )


def _truncate(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 16)].rstrip() + "\n[TRUNCATED]"
