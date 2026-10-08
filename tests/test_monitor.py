"""Tests for the local trigger client (network is stubbed)."""

from __future__ import annotations

import json
import os

import pytest

from client import monitor

SAMPLE_RESULT = {
    "headline": "Go touch grass.",
    "plan": "Walk 640 m to Bryant Park. Screen stays in your pocket.",
    "place": {"name": "Bryant Park"},
    "route": {"distance_m": 640, "duration_min": 8.0},
    "model_used": "offline-planner",
    "data_source": "OpenStreetMap",
}


@pytest.fixture(autouse=True)
def _no_desktop_popups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never raise real Windows toasts."""
    monkeypatch.setattr(monitor.alerts, "notify", lambda *a, **k: "none")


def test_load_config_defaults() -> None:
    config = monitor.load_config([])
    assert config.endpoint.startswith("http")
    assert config.threshold_minutes == 120
    assert config.preferences == []
    assert -90 <= config.latitude <= 90


def test_load_dotenv_reads_file_but_never_overrides_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nDEFAULT_LAT=25.1737019\nDEFAULT_LON=75.8574194\n'
        'TRIGGER_ENDPOINT="http://example.test/trigger"\nMALFORMED LINE\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("DEFAULT_LAT", raising=False)
    monkeypatch.delenv("DEFAULT_LON", raising=False)
    monkeypatch.setenv("TRIGGER_ENDPOINT", "http://shell-wins.test/trigger")

    monitor.load_dotenv(env_file)
    assert os.environ["DEFAULT_LAT"] == "25.1737019"
    assert os.environ["DEFAULT_LON"] == "75.8574194"
    assert os.environ["TRIGGER_ENDPOINT"] == "http://shell-wins.test/trigger"


def test_load_dotenv_tolerates_missing_file(tmp_path) -> None:
    monitor.load_dotenv(tmp_path / "nope.env")  # must not raise


def test_load_config_cli_overrides() -> None:
    config = monitor.load_config(
        ["--lat", "51.5", "--lon", "-0.12", "--threshold-minutes", "45",
         "--preferences", "quiet, coffee", "--once", "--dry-run"]
    )
    assert (config.latitude, config.longitude) == (51.5, -0.12)
    assert config.threshold_minutes == 45
    assert config.preferences == ["quiet", "coffee"]
    assert config.once and config.dry_run


def test_build_payload_matches_backend_contract() -> None:
    config = monitor.load_config(["--lat", "51.5", "--lon", "-0.12", "--radius-m", "900"])
    payload = monitor.build_payload(config, 132)
    assert payload["event"]["minutes"] == 132
    assert payload["event"]["device"]
    assert "application" not in payload["event"]  # privacy: no app surveillance
    assert payload["latitude"] == 51.5
    assert payload["longitude"] == -0.12
    assert payload["radius_m"] == 900
    # Round-trips against the server-side schema without a network call.
    from app.schemas import TriggerRequest

    TriggerRequest.model_validate(payload)


def test_dry_run_never_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError("post_json must not be called in dry-run mode")

    monkeypatch.setattr(monitor, "post_json", explode)
    code = monitor.main(["--dry-run", "--once"])
    assert code == 0


def test_once_sends_single_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def fake_post(url: str, payload: dict, timeout: float) -> dict:
        sent.append(payload)
        return SAMPLE_RESULT

    monkeypatch.setattr(monitor, "post_json", fake_post)
    code = monitor.main(["--once", "--threshold-minutes", "7"])
    assert code == 0
    assert len(sent) == 1
    assert sent[0]["event"]["minutes"] == 7


def test_backend_unreachable_returns_error_code(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import urllib.error

    def fail(url: str, payload: dict, timeout: float) -> dict:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(monitor, "post_json", fail)
    assert monitor.main(["--once"]) == 1
    assert "could not reach backend" in capsys.readouterr().err


def test_trigger_shows_desktop_alert_with_place_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[tuple[str, str]] = []

    def fake_post(url: str, payload: dict, timeout: float) -> dict:
        return SAMPLE_RESULT

    def fake_notify(title: str, body: str, style: str = "auto") -> str:
        shown.append((title, body))
        return "toast"

    monkeypatch.setattr(monitor, "post_json", fake_post)
    monkeypatch.setattr(monitor.alerts, "notify", fake_notify)
    assert monitor.main(["--once"]) == 0
    assert len(shown) == 1
    title, body = shown[0]
    assert title == "Go touch grass."
    assert "Bryant Park" in body  # a real place name, not coordinates
    assert "640 m" in body


def test_alert_style_flag_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    styles: list[str] = []
    monkeypatch.setattr(monitor, "post_json", lambda *a: SAMPLE_RESULT)
    monkeypatch.setattr(
        monitor.alerts,
        "notify",
        lambda t, b, style="auto": styles.append(style) or "dialog",
    )
    assert monitor.main(["--once", "--alert", "dialog"]) == 0
    assert styles == ["dialog"]


def test_format_plan_includes_place_and_route() -> None:
    text = monitor.format_plan(SAMPLE_RESULT)
    assert "Bryant Park" in text
    assert "640 m" in text
    assert "Go touch grass." in text


def test_post_json_builds_http_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps({"ok": True}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(monitor.urllib.request, "urlopen", fake_urlopen)
    result = monitor.post_json("http://example.test/api/v1/trigger", {"a": 1}, 12.5)
    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["body"] == {"a": 1}
    assert captured["timeout"] == 12.5


def test_run_simulate_triggers_and_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end dry loop: simulate mode trips the threshold, then stops."""
    sent: list[dict] = []

    def fake_post(url: str, payload: dict, timeout: float) -> dict:
        sent.append(payload)
        return SAMPLE_RESULT

    monkeypatch.setattr(monitor, "post_json", fake_post)
    config = monitor.load_config(
        ["--simulate", "--threshold-minutes", "1", "--interval", "0.01",
         "--simulate-speed", "6000", "--once"]
    )
    assert monitor.run(config) == 0
    assert sent and sent[0]["event"]["minutes"] == 1
