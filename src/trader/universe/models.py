"""Typed, serializable values for universe discovery."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.agent.models import SYMBOL_PATTERN

CandidateSource = Literal[
    "PORTFOLIO",
    "BENCHMARK",
    "MOST_ACTIVE_VOLUME",
    "MOST_ACTIVE_TRADES",
    "TOP_GAINER",
    "TOP_LOSER",
    "EXPLORATION",
]


class UniverseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseAsset(UniverseModel):
    symbol: str
    name: str | None = None
    asset_class: str
    status: str
    tradable: bool
    exchange: str | None = None
    fractionable: bool = False
    marginable: bool = False
    shortable: bool = False
    easy_to_borrow: bool = False

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("invalid asset symbol")
        return normalized

    @field_validator("asset_class", "status")
    @classmethod
    def normalized_enum(cls, value: str) -> str:
        return value.lower().strip()


class ActiveStockSignal(UniverseModel):
    symbol: str
    volume: float = Field(ge=0, allow_inf_nan=False)
    trade_count: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("invalid active-stock symbol")
        return normalized


class MoverSignal(UniverseModel):
    symbol: str
    percent_change: float = Field(allow_inf_nan=False)
    change: float = Field(allow_inf_nan=False)
    price: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("invalid mover symbol")
        return normalized


class MostActiveBatch(UniverseModel):
    by: Literal["volume", "trades"]
    last_updated: datetime
    stocks: tuple[ActiveStockSignal, ...]

    @field_validator("last_updated")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("screener timestamp must be timezone-aware")
        return value.astimezone(UTC)


class MarketMoversBatch(UniverseModel):
    last_updated: datetime
    gainers: tuple[MoverSignal, ...]
    losers: tuple[MoverSignal, ...]

    @field_validator("last_updated")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("screener timestamp must be timezone-aware")
        return value.astimezone(UTC)


class CandidateSignal(UniverseModel):
    source: CandidateSource
    rank: int | None = Field(default=None, ge=1)
    metric_name: str | None = None
    metric_value: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def complete_metric(self) -> "CandidateSignal":
        if (self.metric_name is None) != (self.metric_value is None):
            raise ValueError("candidate metric name and value must be supplied together")
        return self


class ResearchCandidate(UniverseModel):
    symbol: str
    score: int = Field(ge=0)
    asset: UniverseAsset
    signals: tuple[CandidateSignal, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def asset_matches_symbol(self) -> "ResearchCandidate":
        if self.symbol != self.asset.symbol:
            raise ValueError("candidate symbol does not match asset metadata")
        return self


class UniverseScan(UniverseModel):
    provider: Literal["alpaca"] = "alpaca"
    market_data_feed: Literal["alpaca_screener_sip"] = "alpaca_screener_sip"
    as_of: datetime
    asset_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_assets: tuple[UniverseAsset, ...] = Field(min_length=1, max_length=20_000)
    candidates: tuple[ResearchCandidate, ...] = Field(min_length=1, max_length=100)
    most_active_volume_updated_at: datetime
    most_active_trades_updated_at: datetime
    market_movers_updated_at: datetime
    skipped_screener_symbols: int = Field(ge=0)

    @field_validator(
        "as_of",
        "most_active_volume_updated_at",
        "most_active_trades_updated_at",
        "market_movers_updated_at",
    )
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("universe timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def eligible_asset_count(self) -> int:
        return len(self.eligible_assets)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def summary(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "market_data_feed": self.market_data_feed,
            "as_of": self.as_of.isoformat(),
            "asset_content_hash": self.asset_content_hash,
            "eligible_asset_count": self.eligible_asset_count,
            "candidate_count": self.candidate_count,
            "most_active_volume_updated_at": self.most_active_volume_updated_at.isoformat(),
            "most_active_trades_updated_at": self.most_active_trades_updated_at.isoformat(),
            "market_movers_updated_at": self.market_movers_updated_at.isoformat(),
            "skipped_screener_symbols": self.skipped_screener_symbols,
            "candidates": [candidate.model_dump(mode="json") for candidate in self.candidates],
        }
