"""Tests for the Hermes agent: JSON parsing, place selection, offline plan,
and the POST /api/v1/trigger contract (network mocked)."""

from __future__ import annotations

import json

import pytest

from app.agent.hermes import _offline_plan, _parse_json_block, _select_place
from app.config import Settings
from app.schemas import Place, RouteSummary, TriggerRequest
from app.services.osm import OSMService


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_parse_json_block_plain() -> None:
    assert _parse_json_block('{"headline": "hi", "plan": "go"}') == {
        "headline": "hi",
        "plan": "go",
    }


def test_parse_json_block_fenced_and_surrounded() -> None:
    text = 'Sure!\n```json\n{"headline": "Go", "plan": "Walk."}\n```\nEnjoy.'
    assert _parse_json_block(text) == {"headline": "Go", "plan": "Walk."}


def test_parse_json_block_rejects_garbage() -> None:
    assert _parse_json_block("I could not find anything.") is None
    assert _parse_json_block("") is None


def test_select_place_matches_model_choice() -> None:
    places = [
        Place(name="Riverside Park", kind="park", latitude=1.0, longitude=2.0, distance_m=300),
        Place(name="Corner Cafe", kind="cafe", latitude=1.0, longitude=2.1, distance_m=500),
    ]
    chosen, alternatives = _select_place(places, "riverside park")
    assert chosen.name == "Riverside Park"
    assert [a.name for a in alternatives] == ["Corner Cafe"]


def test_select_place_defaults_to_nearest() -> None:
    places = [Place(name="A", kind="park", latitude=1.0, longitude=2.0, distance_m=100)]
    chosen, alternatives = _select_place(places, "does not exist")
    assert chosen.name == "A"
    assert alternatives == []


def test_select_place_handles_empty_osm_result() -> None:
    chosen, alternatives = _select_place([], "")
    assert alternatives == []
    assert chosen.distance_m == 0.0


def test_offline_plan_mentions_place_and_route() -> None:
    payload = TriggerRequest(
        event={"minutes": 132, "application": "browser"},  # type: ignore[arg-type]
        latitude=40.7128,
        longitude=-74.0060,
    )
    place = Place(
        name="City Park", kind="park", latitude=40.71, longitude=-74.0, distance_m=850
    )
    route = RouteSummary(
        summary="850 m on foot",
        distance_m=850,
        duration_min=10.5,
        steps=[{"instruction": "Start on Main St", "distance_m": 850}],  # type: ignore[list-item]
        source="osrm",
    )
    headline, plan = _offline_plan(payload, place, route)
    assert "City Park" in plan
    assert "Main St" in plan
    assert "Screen stays in your pocket" in plan
    assert headline


# --------------------------------------------------------------------------- #
# HTTP contract (OSM mocked, no LLM configured -> offline planner)
# --------------------------------------------------------------------------- #
FAKE_PLACES = [
    Place(
        name="Bryant Park",
        kind="park",
        latitude=40.7536,
        longitude=-73.9832,
        distance_m=640.0,
        address="New York, NY",
        osm_id="way/1",
    ),
    Place(
        name="Blue Bottle",
        kind="cafe",
        latitude=40.7540,
        longitude=-73.9840,
        distance_m=700.0,
        address="New York, NY",
        osm_id="node/2",
    ),
]


@pytest.fixture()
def test_client(monkeypatch: pytest.MonkeyPatch):
    async def fake_nearby(self, latitude, longitude, radius_m=None, kinds=None):  # noqa: ANN001
        return list(FAKE_PLACES)

    async def fake_route(self, origin_lat, origin_lon, dest_lat, dest_lon):  # noqa: ANN001
        return RouteSummary(
            summary="640 m on foot",
            distance_m=640,
            duration_min=8.0,
            steps=[{"instruction": "Start on 5th Ave", "distance_m": 640}],  # type: ignore[list-item]
            source="osrm",
        )

    monkeypatch.setattr(OSMService, "nearby_places", fake_nearby)
    monkeypatch.setattr(OSMService, "walking_route", fake_route)

    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(Settings(llm_api_key="", allow_offline_planner=True)))


def test_health(test_client) -> None:
    response = test_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_configured"] is False


def test_trigger_returns_plan(test_client) -> None:
    response = test_client.post(
        "/api/v1/trigger",
        json={
            "event": {"minutes": 132, "application": "browser", "device": "laptop"},
            "latitude": 40.7580,
            "longitude": -73.9855,
            "radius_m": 1500,
            "preferences": ["coffee"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["place"]["name"] == "Bryant Park"
    assert body["route"]["distance_m"] == 640
    assert "Bryant Park" in body["plan"]
    assert body["alternatives"][0]["name"] == "Blue Bottle"
    assert body["data_source"] == "OpenStreetMap"
    assert body["model_used"] == "offline-planner"


def test_trigger_rejects_bad_coordinates(test_client) -> None:
    response = test_client.post(
        "/api/v1/trigger",
        json={"event": {"minutes": 120}, "latitude": 0, "longitude": 0},
    )
    assert response.status_code == 422


def test_trigger_rejects_missing_coordinates(test_client) -> None:
    response = test_client.post("/api/v1/trigger", json={"event": {"minutes": 120}})
    assert response.status_code == 422


def test_offline_plan_without_osm_data(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(self, *args, **kwargs):  # noqa: ANN001, ANN003
        return []

    monkeypatch.setattr(OSMService, "nearby_places", empty)
    response = test_client.post(
        "/api/v1/trigger",
        json={"event": {"minutes": 120}, "latitude": 40.7, "longitude": -74.0},
    )
    assert response.status_code == 200
    assert "No signal needed." in response.json()["headline"]


def test_tool_dispatch_finds_spots(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.hermes import HermesAgent

    async def fake_nearby(self, latitude, longitude, radius_m=None, kinds=None):  # noqa: ANN001
        assert latitude == 1.5 and kinds == ["park"]
        return FAKE_PLACES[:1]

    monkeypatch.setattr(OSMService, "nearby_places", fake_nearby)
    agent = HermesAgent(Settings(llm_api_key=""))
    result = asyncio_run(agent._dispatch_tool(
        "find_outdoor_spots",
        {"latitude": 1.5, "longitude": 2.5, "kinds": ["park"]},
    ))
    assert isinstance(result, list)
    assert result[0]["name"] == "Bryant Park"
    assert set(result[0]) >= {"latitude", "longitude", "distance_m"}


def test_tool_dispatch_unknown_tool() -> None:
    import asyncio

    from app.agent.hermes import HermesAgent

    agent = HermesAgent(Settings(llm_api_key=""))
    with pytest.raises(ValueError):
        asyncio.run(agent._dispatch_tool("nope", {}))


def asyncio_run(coro):  # noqa: ANN001, ANN202
    import asyncio

    return asyncio.run(coro)
