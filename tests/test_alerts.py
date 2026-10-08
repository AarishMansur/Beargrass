"""Tests for desktop alert formatting and mechanism dispatch (no real popups)."""

from __future__ import annotations

import pytest

from client import alerts

RESULT = {
    "headline": "Go touch grass.",
    "plan": "Walk 640 m to Bryant Park. Phone stays in your pocket.",
    "place": {"name": "Shivaji Park"},
    "route": {"distance_m": 640, "duration_min": 10.0},
}


def test_format_alert_contains_real_place_and_route() -> None:
    title, body = alerts.format_alert(RESULT)
    assert title == "Go touch grass."
    assert "Bryant Park" in body
    assert "640 m" in body
    assert "8 min walk" in body
    assert "Phone stays" in body


def test_format_alert_km_units() -> None:
    result = dict(RESULT, route={"distance_m": 2340, "duration_min": 29})
    _, body = alerts.format_alert(result)
    assert "2.3 km" in body
    assert "29 min walk" in body


def test_format_alert_handles_minimal_result() -> None:
    title, body = alerts.format_alert({"headline": "Outside."})
    assert title == "Outside."
    assert "nearest patch of outdoors" in body


def test_notify_none_style_never_pops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        alerts._MECHANISMS, "toast", lambda *a: (_ for _ in ()).throw(AssertionError())
    )
    assert alerts.notify("t", "b", style="none") == "none"


def test_notify_auto_falls_back_to_next_mechanism(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setitem(alerts._MECHANISMS, "window", lambda t, b: calls.append("window") or False)
    monkeypatch.setitem(alerts._MECHANISMS, "dialog", lambda t, b: calls.append("dialog") or True)
    monkeypatch.setitem(alerts._MECHANISMS, "toast", lambda t, b: calls.append("toast") or True)
    monkeypatch.setitem(alerts._MECHANISMS, "balloon", lambda t, b: calls.append("balloon") or True)
    assert alerts.notify("t", "b") == "dialog"
    assert calls == ["window", "dialog"]


def test_notify_prefers_popup_window_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.sys, "platform", "win32")
    monkeypatch.setitem(alerts._MECHANISMS, "window", lambda t, b: True)
    monkeypatch.setitem(
        alerts._MECHANISMS, "dialog", lambda *a: (_ for _ in ()).throw(AssertionError())
    )
    assert alerts.notify("t", "b") == "window"


def test_notify_survives_a_broken_mechanism(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(title: str, body: str) -> bool:
        raise RuntimeError("popup exploded")

    monkeypatch.setitem(alerts._MECHANISMS, "window", boom)
    monkeypatch.setitem(alerts._MECHANISMS, "dialog", lambda t, b: True)
    monkeypatch.setitem(alerts._MECHANISMS, "toast", lambda t, b: True)
    monkeypatch.setitem(alerts._MECHANISMS, "balloon", lambda t, b: True)
    assert alerts.notify("t", "b") == "dialog"


def test_notify_returns_none_when_all_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("window", "dialog", "toast", "balloon"):
        monkeypatch.setitem(alerts._MECHANISMS, name, lambda t, b: False)
    assert alerts.notify("t", "b") == "none"


def test_notify_rejects_unknown_style() -> None:
    with pytest.raises(ValueError):
        alerts.notify("t", "b", style="carrier-pigeon")


def test_mechanisms_callable_and_windows_only_outside_ci(monkeypatch) -> None:
    # The dispatcher must consult the registry for whatever platform is running.
    seen: list[str] = []
    monkeypatch.setitem(alerts._MECHANISMS, "window", lambda t, b: False)
    monkeypatch.setitem(alerts._MECHANISMS, "toast", lambda t, b: seen.append("x") or False)
    monkeypatch.setattr(alerts.sys, "platform", "linux")
    monkeypatch.setitem(alerts._MECHANISMS, "linux", lambda t, b: True)
    assert alerts.notify("t", "b") == "linux"
    assert seen == []  # linux style never tries the windows toast
