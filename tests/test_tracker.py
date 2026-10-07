"""Tests for the local screen-time tracker (pure logic, no network)."""

from __future__ import annotations

import pytest

from client.tracker import ActiveTimeTracker, get_idle_seconds


def test_accumulates_only_active_time() -> None:
    tracker = ActiveTimeTracker(threshold_minutes=120)
    # 30 min of activity sampled in 30s intervals.
    for _ in range(60):
        assert tracker.tick(idle_seconds=5, elapsed_seconds=30) is False
    assert tracker.active_seconds == 1800
    assert tracker.active_minutes == 30
    assert tracker.progress == pytest.approx(0.25)


def test_idle_time_does_not_accumulate() -> None:
    tracker = ActiveTimeTracker(threshold_minutes=2)
    for _ in range(100):
        assert tracker.tick(idle_seconds=300, elapsed_seconds=30) is False
    assert tracker.active_seconds == 0
    assert tracker.progress == 0


def test_edge_triggered_breach_fires_once() -> None:
    tracker = ActiveTimeTracker(threshold_minutes=1)
    fired = [tracker.tick(idle_seconds=0, elapsed_seconds=30) for _ in range(3)]
    assert fired == [False, True, False]
    assert tracker.breached


def test_acknowledge_resets_window() -> None:
    tracker = ActiveTimeTracker(threshold_minutes=1)
    tracker.tick(idle_seconds=0, elapsed_seconds=61)
    assert tracker.breached
    tracker.acknowledge()
    assert tracker.active_seconds == 0
    assert not tracker.breached
    assert tracker.tick(idle_seconds=0, elapsed_seconds=61) is True


def test_unknown_idle_state_counts_as_active() -> None:
    tracker = ActiveTimeTracker(threshold_minutes=1)
    assert tracker.tick(idle_seconds=None, elapsed_seconds=61) is True


def test_rejects_bad_configuration() -> None:
    with pytest.raises(ValueError):
        ActiveTimeTracker(threshold_minutes=0)
    tracker = ActiveTimeTracker(threshold_minutes=10)
    with pytest.raises(ValueError):
        tracker.tick(idle_seconds=1, elapsed_seconds=-5)


def test_get_idle_seconds_returns_number_on_this_platform() -> None:
    idle = get_idle_seconds()
    # Supported on Windows/macOS/X11; None only on exotic platforms or when
    # the optional OS utility is missing.
    assert idle is None or (isinstance(idle, float) and idle >= 0)
