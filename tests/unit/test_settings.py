import pytest
from pydantic import ValidationError

from trader.settings import Settings


def test_settings_default_to_disabled_paper_mode() -> None:
    settings = Settings(_env_file=None)

    assert settings.trader_environment == "paper"
    assert settings.alpaca_paper is True
    assert settings.trading_enabled is False
    assert settings.trader_database_url == "sqlite:///data/paper/trader.db"
    assert str(settings.trader_dynamic_runs_config) == "config/dynamic_runs.yaml"
    assert str(settings.trader_research_config) == "config/research.yaml"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"trader_environment": "live"}, "paper mode is required"),
        ({"alpaca_paper": False}, "paper mode is required"),
        ({"trader_database_url": "sqlite:///data/live/trader.db"}, "live database"),
        ({"market_data_max_age_seconds": 0}, "must be positive"),
    ],
)
def test_settings_reject_unsafe_or_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **overrides)


def test_credentials_are_explicitly_required() -> None:
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        Settings(_env_file=None).require_broker_credentials()


def test_credentials_are_unwrapped_only_at_broker_boundary() -> None:
    settings = Settings(
        _env_file=None,
        alpaca_api_key="paper-key",
        alpaca_api_secret="paper-secret",
    )

    assert settings.require_broker_credentials() == ("paper-key", "paper-secret")


def test_sec_user_agent_must_be_supplied_explicitly() -> None:
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        Settings(_env_file=None).require_sec_user_agent()

    settings = Settings(_env_file=None, sec_user_agent=" Jack jack@example.com ")
    assert settings.require_sec_user_agent() == "Jack jack@example.com"
