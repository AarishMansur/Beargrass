"""Settings regressions: .env files must parse without JSON syntax."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_parses() -> None:
    """`cp .env.example .env` must not crash the server at import time."""
    settings = Settings(_env_file=str(ENV_EXAMPLE))
    assert settings.allow_offline_planner is True
    assert settings.cors_origins == ["*"]
    assert settings.osm_nearby_radius_m == 1500
    assert settings.llm_model


def test_cors_origins_splits_comma_separated_env(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "https://a.test, https://b.test")
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["https://a.test", "https://b.test"]


def test_llm_configured_requires_key() -> None:
    assert Settings(_env_file=None, llm_api_key="").llm_configured is False
    assert Settings(_env_file=None, llm_api_key="sk-x").llm_configured is True
    # Local endpoints (Ollama) never need a real key.
    assert (
        Settings(_env_file=None, llm_base_url="http://localhost:11434/v1", llm_api_key="")
        .llm_configured
        is True
    )
