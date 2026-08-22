"""Strict structured output from the reasoning agent."""
import re
from decimal import Decimal
from math import isfinite
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
ResearchEvidenceId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID = Field(default_factory=uuid4)
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    target_notional_usd: Decimal | None = None
    target_position_pct: Decimal | None = None
    thesis_id: UUID | None = None
    strategy_ids: list[UUID] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    time_horizon: Literal["days", "weeks", "months", "long_term"]
    rationale: str
    catalysts: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    evidence_ids: list[ResearchEvidenceId] = Field(default_factory=list)
    desired_order_type: Literal["LIMIT"] = "LIMIT"
    max_acceptable_price: Decimal | None = None
    min_acceptable_price: Decimal | None = None

    @model_validator(mode="after")
    def valid_action(self) -> "TradeProposal":
        self.symbol = self.symbol.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must be a valid uppercase equity symbol")
        if not isfinite(self.confidence):
            raise ValueError("confidence must be finite")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique")

        decimal_fields = {
            "target_notional_usd": self.target_notional_usd,
            "target_position_pct": self.target_position_pct,
            "max_acceptable_price": self.max_acceptable_price,
            "min_acceptable_price": self.min_acceptable_price,
        }
        for name, value in decimal_fields.items():
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be finite")

        targets = (self.target_notional_usd, self.target_position_pct)
        if self.action == "HOLD":
            if any(value is not None for value in targets):
                raise ValueError("hold cannot specify a target")
            if self.max_acceptable_price is not None or self.min_acceptable_price is not None:
                raise ValueError("hold cannot specify an acceptable price")
            return self

        if sum(value is not None for value in targets) != 1:
            raise ValueError("trade requires exactly one target")
        if self.target_notional_usd is not None and self.target_notional_usd <= 0:
            raise ValueError("target notional must be positive")
        if self.target_position_pct is not None:
            if self.target_position_pct < 0 or self.target_position_pct > 100:
                raise ValueError("target position percent must be between 0 and 100")
            if self.action == "BUY" and self.target_position_pct == 0:
                raise ValueError("buy target position percent must be positive")

        if self.action == "BUY":
            if self.max_acceptable_price is None:
                raise ValueError("buy requires max_acceptable_price")
            if self.min_acceptable_price is not None:
                raise ValueError("buy cannot specify min_acceptable_price")
        if self.action == "SELL":
            if self.min_acceptable_price is None:
                raise ValueError("sell requires min_acceptable_price")
            if self.max_acceptable_price is not None:
                raise ValueError("sell cannot specify max_acceptable_price")
        if self.max_acceptable_price is not None and self.max_acceptable_price <= 0:
            raise ValueError("maximum acceptable price must be positive")
        if self.min_acceptable_price is not None and self.min_acceptable_price <= 0:
            raise ValueError("minimum acceptable price must be positive")
        return self
