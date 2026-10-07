"""Validate the Render Blueprint so a broken render.yaml fails CI, not deploys."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required to validate render.yaml")

RENDER_FILE = Path(__file__).resolve().parents[1] / "render.yaml"


@pytest.fixture(scope="module")
def blueprint() -> dict:
    data = yaml.safe_load(RENDER_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "render.yaml must be a YAML mapping"
    return data


def test_exactly_one_web_service(blueprint: dict) -> None:
    services = blueprint.get("services")
    assert isinstance(services, list) and services, "services must be a non-empty list"
    assert len(services) == 1, "monolith ships as a single web service"
    service = services[0]
    assert service["type"] == "web"
    assert service["runtime"] == "python"
    assert service["name"] == "touch-grass-agent"


def test_build_and_start_commands(blueprint: dict) -> None:
    service = blueprint["services"][0]
    assert "requirements.txt" in service["buildCommand"]
    start = service["startCommand"]
    assert "uvicorn app.main:app" in start
    assert "--host 0.0.0.0" in start
    assert "$PORT" in start or "${PORT" in start


def test_health_check_matches_fastapi_route(blueprint: dict) -> None:
    assert blueprint["services"][0]["healthCheckPath"] == "/health"
    # The route must actually exist in the app (inspect the OpenAPI schema,
    # which resolves lazily-included routers).
    from app.main import create_app

    paths = set(create_app().openapi()["paths"])
    assert "/health" in paths
    assert "/api/v1/trigger" in paths


def test_secrets_are_synced_not_committed(blueprint: dict) -> None:
    env_vars = blueprint["services"][0]["envVars"]
    by_key = {entry["key"]: entry for entry in env_vars}
    assert by_key["LLM_API_KEY"].get("sync") is False, "API key must never be committed"
    assert by_key["PYTHON_VERSION"]["value"]
    assert by_key["ALLOW_OFFLINE_PLANNER"]["value"] in {"true", "false"}


def test_env_keys_match_settings_fields() -> None:
    """Every env var in render.yaml must be a real Settings field."""
    from app.config import Settings

    fields = set(Settings.model_fields)
    data = yaml.safe_load(RENDER_FILE.read_text(encoding="utf-8"))
    for entry in data["services"][0]["envVars"]:
        key = entry["key"]
        if key in {"PYTHON_VERSION", "LOG_LEVEL"}:  # runtime-provided / uvicorn
            continue
        assert key.lower() in fields, f"{key} does not map to a Settings field"
