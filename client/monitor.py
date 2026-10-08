#!/usr/bin/env python3
"""Touch Grass Agent — local screen-time trigger client.

Tracks how long you have been actively on the machine, and when the
threshold is breached it POSTs your coordinates to the backend, which
replies with a concrete, screen-free plan (a real park and a real route).

Stdlib only — no third-party dependencies, no accounts, no telemetry.

Examples
--------
    python client/monitor.py                      # real tracking, default 2h
    python client/monitor.py --once               # fire one trigger now (smoke test)
    python client/monitor.py --dry-run            # print the payload, send nothing
    python client/monitor.py --simulate --threshold-minutes 1 --interval 1
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # executed as `python client/monitor.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import alerts
from client.tracker import ActiveTimeTracker, get_idle_seconds


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE) so the client honours the same file
    as the server. Never overrides variables already set in the shell."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_ENDPOINT = os.getenv(
    "TRIGGER_ENDPOINT", "http://127.0.0.1:8000/api/v1/trigger"
)


@dataclass
class Config:
    endpoint: str = DEFAULT_ENDPOINT
    latitude: float = float(os.getenv("DEFAULT_LAT", "40.7128"))
    longitude: float = float(os.getenv("DEFAULT_LON", "-74.0060"))
    radius_m: int = 1500
    threshold_minutes: int = int(os.getenv("SCREEN_TIME_THRESHOLD_MIN", "120"))
    interval_seconds: float = 30.0
    preferences: list[str] = field(default_factory=list)
    simulate: bool = False
    simulate_speed: float = 60.0  # 1 real second counts as 60 active seconds
    once: bool = False
    dry_run: bool = False
    alert: str = "auto"
    timeout: float = 60.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client/monitor.py",
        description="Watch screen time and trigger a Touch Grass plan.",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"Backend trigger URL (env TRIGGER_ENDPOINT). Default: {DEFAULT_ENDPOINT}",
    )
    parser.add_argument("--lat", type=float, help="Latitude (env DEFAULT_LAT).")
    parser.add_argument("--lon", type=float, help="Longitude (env DEFAULT_LON).")
    parser.add_argument(
        "--radius-m", type=int, default=1500, help="Search radius in metres."
    )
    parser.add_argument(
        "--threshold-minutes",
        type=int,
        default=int(os.getenv("SCREEN_TIME_THRESHOLD_MIN", "120")),
        help="Active screen minutes before triggering (env SCREEN_TIME_THRESHOLD_MIN).",
    )
    parser.add_argument(
        "--interval", type=float, default=30.0, help="Seconds between samples."
    )
    parser.add_argument(
        "--preferences",
        default="",
        help="Comma-separated hints, e.g. 'quiet,coffee'.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Ignore the real idle counter; every tick counts as active.",
    )
    parser.add_argument(
        "--simulate-speed",
        type=float,
        default=60.0,
        help="Active seconds per real second while simulating.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fire a single trigger immediately, then exit (smoke test).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload instead of sending it.",
    )
    parser.add_argument(
        "--alert",
        choices=alerts.ALERT_STYLES,
        default="auto",
        help="Desktop notification style when the threshold trips (default: auto).",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="HTTP timeout in seconds."
    )
    return parser


def load_config(argv: list[str] | None = None) -> Config:
    args = build_parser().parse_args(argv)
    config = Config(
        endpoint=args.endpoint,
        radius_m=args.radius_m,
        threshold_minutes=args.threshold_minutes,
        interval_seconds=args.interval,
        preferences=[p.strip() for p in args.preferences.split(",") if p.strip()],
        simulate=args.simulate,
        simulate_speed=args.simulate_speed,
        once=args.once,
        dry_run=args.dry_run,
        alert=args.alert,
        timeout=args.timeout,
    )
    if args.lat is not None:
        config.latitude = args.lat
    if args.lon is not None:
        config.longitude = args.lon
    return config


def build_payload(config: Config, minutes: int) -> dict:
    """POST body matching the backend's TriggerRequest schema.

    The dominant application is deliberately omitted: the plan should cost
    you your feed, not read it.
    """
    return {
        "event": {
            "minutes": minutes,
            "device": platform.node() or "unknown-device",
        },
        "latitude": round(config.latitude, 6),
        "longitude": round(config.longitude, 6),
        "radius_m": config.radius_m,
        "preferences": config.preferences,
    }


def post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def format_plan(result: dict) -> str:
    place = (result.get("place") or {}).get("name", "somewhere outdoors")
    route = result.get("route") or {}
    headline = result.get("headline", "Go touch grass.")
    plan = result.get("plan", "")
    distance = route.get("distance_m")
    duration = route.get("duration_min")
    route_line = ""
    if distance is not None and duration is not None:
        route_line = f"\n  route : {round(distance)} m, ~{round(duration)} min on foot"
    return f"\n{headline}\n  place : {place}{route_line}\n  plan  : {plan}"


def run(config: Config) -> int:
    tracker = ActiveTimeTracker(config.threshold_minutes)
    print(
        f"[touch-grass] watching screen time | threshold={config.threshold_minutes}min "
        f"interval={config.interval_seconds}s endpoint={config.endpoint}"
    )

    if config.once:
        return _send(config, tracker.active_minutes or config.threshold_minutes)

    if config.simulate:
        print("[touch-grass] SIMULATE mode: every tick counts as active time.")

    last = time.monotonic()
    try:
        while True:
            time.sleep(config.interval_seconds)
            now = time.monotonic()
            elapsed = now - last
            last = now

            if config.simulate:
                idle: float | None = 0.0
                elapsed *= config.simulate_speed
            else:
                idle = get_idle_seconds()

            if tracker.tick(idle, elapsed):
                code = _send(config, config.threshold_minutes)
                if code != 0 or config.once:
                    return code
                tracker.acknowledge()
            else:
                print(
                    f"\r[touch-grass] {tracker.active_minutes}/{config.threshold_minutes} "
                    f"active min ({tracker.progress:.0%})",
                    end="",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("\n[touch-grass] stopped.")
        return 0


def _send(config: Config, minutes: int) -> int:
    payload = build_payload(config, max(minutes, 1))
    if config.dry_run:
        print("\n[dry-run] payload:\n" + json.dumps(payload, indent=2))
        return 0
    try:
        result = post_json(config.endpoint, payload, config.timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        print(f"\n[touch-grass] backend error {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"\n[touch-grass] could not reach backend: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"\n[touch-grass] bad response from backend: {exc}", file=sys.stderr)
        return 1

    print(format_plan(result))
    print(f"[touch-grass] powered by {result.get('model_used', 'unknown')} / {result.get('data_source', 'OSM')}")

    title, body = alerts.format_alert(result)
    shown = alerts.notify(title, body, style=config.alert)
    print(f"[touch-grass] desktop alert: {shown}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(load_config(argv))


if __name__ == "__main__":
    raise SystemExit(main())
