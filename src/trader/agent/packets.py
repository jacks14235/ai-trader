"""Bounded research memos with claim identity and tamper-evident provenance.

Packets are synthesis, never additional evidence and never executable proposals. Citation
validation proves membership in the admitted record, not that a source entails a claim.
"""

import hashlib
import json
import unicodedata
from collections.abc import Collection, Iterator
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_PACKET_SYMBOLS = 50
MAX_CLAIMS_PER_CATEGORY = 10
MAX_MATERIAL_CHALLENGES_PER_PACKET = 10
MAX_CLAIM_TEXT_CHARS = 2_000
MAX_CLAIM_EVIDENCE_IDS = 20
MAX_PACKET_NOTES = 20
MAX_PACKET_NOTE_CHARS = 1_000


def _safe_text(value: str) -> str:
    if not value.strip():
        raise ValueError("packet text must not be blank")
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\n\r\t" for char in value
    ):
        raise ValueError("packet text cannot contain unsafe control characters")
    return value


LocalIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        description=(
            "Lowercase snake_case identifier using only letters, digits, and underscores; "
            "must start with a letter."
        ),
    ),
]
ContentHash = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
ClaimText = Annotated[
    str, Field(min_length=1, max_length=MAX_CLAIM_TEXT_CHARS), AfterValidator(_safe_text)
]
PacketNote = Annotated[
    str, Field(min_length=1, max_length=MAX_PACKET_NOTE_CHARS), AfterValidator(_safe_text)
]


class PacketModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class CitedClaim(PacketModel):
    claim_id: LocalIdentifier
    text: ClaimText
    evidence_ids: tuple[ContentHash, ...] = Field(min_length=1, max_length=MAX_CLAIM_EVIDENCE_IDS)

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("claim evidence_ids must be unique")
        return values


class SymbolPacket(PacketModel):
    symbol: str = Field(min_length=1, max_length=15, pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    facts: tuple[CitedClaim, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_CATEGORY)
    source_claims: tuple[CitedClaim, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_CATEGORY)
    interpretations: tuple[CitedClaim, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_CATEGORY)
    contradictions: tuple[CitedClaim, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_CATEGORY)
    unknowns: tuple[PacketNote, ...] = Field(default=(), max_length=MAX_PACKET_NOTES)
    dissent: tuple[CitedClaim, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_CATEGORY)

    def cited_claims(self) -> Iterator[CitedClaim]:
        """All evidence-bearing categories, including interpretations and dissent."""
        for group in (
            self.facts,
            self.source_claims,
            self.interpretations,
            self.contradictions,
            self.dissent,
        ):
            yield from group


class ResearchPacket(PacketModel):
    schema_version: Literal[1] = 1
    status: Literal["PACKET"] = "PACKET"
    symbols: tuple[SymbolPacket, ...] = Field(default=(), max_length=MAX_PACKET_SYMBOLS)
    limitations: tuple[PacketNote, ...] = Field(default=(), max_length=MAX_PACKET_NOTES)

    @field_validator("schema_version", mode="before")
    @classmethod
    def actual_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("packet schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_identities(self) -> "ResearchPacket":
        symbols = [item.symbol for item in self.symbols]
        if len(symbols) != len(set(symbols)):
            raise ValueError("packet symbols must be unique")
        claim_ids = [claim.claim_id for item in self.symbols for claim in item.cited_claims()]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_ids must be unique across the entire packet")
        material_challenges = sum(
            len(item.contradictions) + len(item.dissent) for item in self.symbols
        )
        if material_challenges > MAX_MATERIAL_CHALLENGES_PER_PACKET:
            raise ValueError(
                "packet cannot exceed "
                f"{MAX_MATERIAL_CHALLENGES_PER_PACKET} contradiction/dissent claims"
            )
        return self


def packet_content_hash(packet: ResearchPacket) -> str:
    """SHA-256 of the same canonical JSON used by invocation.canonical_json, without imports.

    The producer invocation ID belongs to the envelope. It is not part of the payload hash.
    """
    canonical = json.dumps(
        packet.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NamedPacket(PacketModel):
    step: LocalIdentifier
    invocation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    content_hash: ContentHash
    packet: ResearchPacket

    @model_validator(mode="after")
    def authentic_content(self) -> "NamedPacket":
        if self.content_hash != packet_content_hash(self.packet):
            raise ValueError("research packet content hash mismatch")
        return self


def validate_research_packet(
    packet: ResearchPacket,
    admitted_evidence_ids: Collection[str],
    allowed_symbols: Collection[str],
) -> ResearchPacket:
    """Revalidate structure and run-scoped citations before accepting a model's memo.

    Unknowns and limitations describe gaps, not uncited assertions or alternate evidence IDs.
    The caller owns admission of evidence and the allowed candidate/held symbol set.
    """
    packet = ResearchPacket.model_validate(packet)
    admitted = set(admitted_evidence_ids)
    allowed = set(allowed_symbols)
    for item in packet.symbols:
        if item.symbol not in allowed:
            raise ValueError(f"research packet contains unsupported symbol: {item.symbol}")
        for claim in item.cited_claims():
            if not set(claim.evidence_ids).issubset(admitted):
                raise ValueError(f"claim {claim.claim_id} cites unadmitted evidence IDs")
    return packet
