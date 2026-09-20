"""Typed settings with explicit paper/live separation."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    alpaca_api_key: SecretStr | None = None
    alpaca_api_secret: SecretStr | None = None
    alpaca_paper: bool = True
    trading_enabled: bool = False
    trader_environment: Literal["paper", "live"] = "paper"
    trader_database_url: str = "sqlite:///data/paper/trader.db"
    trader_risk_config: Path = Path("config/risk.yaml")
    trader_universe_config: Path = Path("config/universe.yaml")
    trader_dynamic_runs_config: Path = Path("config/dynamic_runs.yaml")
    trader_research_config: Path = Path("config/research.yaml")
    trader_agents_config: Path = Path("config/agents.yaml")
    trader_pipelines_config: Path = Path("config/pipelines.yaml")
    trader_strategy_document: Path = Path("knowledge/strategy.md")
    trader_portfolio_policy: Path = Path("knowledge/portfolio_policy.md")
    trader_reasoning_enabled: bool = False
    trader_strategist_enabled: bool = False
    trader_raw_data_dir: Path = Path("data/raw")
    stop_trading_file: Path = Path("STOP_TRADING")
    market_data_max_age_seconds: int = 900
    sec_user_agent: str | None = None

    @field_validator("market_data_max_age_seconds")
    @classmethod
    def positive_max_age(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("market data maximum age must be positive")
        return value

    @model_validator(mode="after")
    def paper_only(self) -> "Settings":
        if self.trader_environment != "paper" or not self.alpaca_paper:
            raise ValueError("live trading is not implemented; paper mode is required")
        if "/live/" in self.trader_database_url:
            raise ValueError("paper mode cannot use a live database")
        return self

    def require_broker_credentials(self) -> tuple[str, str]:
        if not self.alpaca_api_key or not self.alpaca_api_secret:
            raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET are required")
        return self.alpaca_api_key.get_secret_value(), self.alpaca_api_secret.get_secret_value()

    def require_sec_user_agent(self) -> str:
        """Return the operator-supplied SEC identity required by data.sec.gov."""
        if self.sec_user_agent is None or not self.sec_user_agent.strip():
            raise ValueError("SEC_USER_AGENT is required for SEC research collection")
        return self.sec_user_agent.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
