"""Unit tests for the OpenStreetMap service (no live network calls)."""

from __future__ import annotations

import math

import pytest

from app.schemas import RouteSummary
from app.services.osm import (
    OSMService,
    _geodesic_summary,
    _osrm_instruction,
    classify,
    haversine_m,
    viewbox,
)


def test_haversine_zero_distance() -> None:
    assert haversine_m(40.0, -74.0, 40.0, -74.0) == 0.0


def test_haversine_known_distance() -> None:
    # ~1111.9 m per 0.01 degrees of latitude.
    distance = haversine_m(40.0, -74.0, 40.01, -74.0)
    assert math.isclose(distance, 1111.9, rel_tol=0.01)


def test_classify_kinds() -> None:
    assert classify({"leisure": "park"}) == "park"
    assert classify({"amenity": "cafe"}) == "cafe"
    assert classify({"tourism": "viewpoint"}) == "viewpoint"
    assert classify({"natural": "wood"}) == "outdoor"
    assert classify({"building": "yes"}) is None


def test_viewbox_is_symmetric() -> None:
    left, top, right, bottom = (float(v) for v in viewbox(40.0, -74.0, 1000).split(","))
    assert left < -74.0 < right
    assert bottom < 40.0 < top
    assert math.isclose(-74.0 - left, right - -74.0, rel_tol=0.01)


def test_parse_overpass_dedupes_and_sorts() -> None:
    payload = {
        "elements": [
            {
                "type": "node",
                "id": 1,
                "lat": 40.001,
                "lon": -74.0,
                "tags": {"name": "Near Park", "leisure": "park"},
            },
            {
                "type": "way",
                "id": 2,
                "center": {"lat": 40.01, "lon": -74.0},
                "tags": {"name": "Far Park", "leisure": "park"},
            },
            {
                "type": "node",
                "id": 3,
                "lat": 40.002,
                "lon": -74.0,
                "tags": {"name": "Near Park", "leisure": "park"},
            },
            {"type": "node", "id": 4, "lat": 40.003, "lon": -74.0, "tags": {"name": ""}},
        ]
    }
    service = OSMService()
    places = service._parse_overpass(payload, 40.0, -74.0)
    assert [p.name for p in places] == ["Near Park", "Far Park"]
    assert places[0].distance_m < places[1].distance_m
    assert places[0].kind == "park"
    assert places[1].osm_id == "way/2"


def test_geodesic_summary_units() -> None:
    assert "m walk" in _geodesic_summary(320)
    assert "km walk" in _geodesic_summary(2400)


def test_osrm_instruction_phrases() -> None:
    assert "Start on" in _osrm_instruction(
        {"maneuver": {"type": "depart"}, "name": "Main St"}
    )
    assert _osrm_instruction({"maneuver": {"type": "roundabout"}, "name": "x"}) == ""


def test_walking_route_falls_back_to_geodesic(monkeypatch) -> None:
    import asyncio

    service = OSMService()

    async def _fail(*args, **kwargs):  # noqa: ANN001, ANN003
        return None

    monkeypatch.setattr(service, "_osrm_route", _fail)
    route = asyncio.run(service.walking_route(40.0, -74.0, 40.005, -74.0))
    assert isinstance(route, RouteSummary)
    assert route.source == "geodesic"
    assert route.distance_m > 0
    assert route.duration_min > 0
