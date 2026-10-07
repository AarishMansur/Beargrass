"""Pydantic request/response contracts for the Touch Grass Agent API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

PlaceKind = Literal["park", "cafe", "viewpoint", "outdoor", "place"]


class ScreenTimeEvent(BaseModel):
    """A single screen-time observation from the local trigger client."""

    minutes: int = Field(..., ge=0, description="Continuous active screen minutes.")
    application: Optional[str] = Field(
        default=None, description="App/process that dominated the session."
    )
    device: Optional[str] = Field(default=None, description="Human-readable device name.")


class TriggerRequest(BaseModel):
    """Payload POSTed to /api/v1/trigger."""

    event: ScreenTimeEvent = Field(
        default_factory=lambda: ScreenTimeEvent(minutes=120),
        description="Screen-time event that tripped the threshold.",
    )
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    radius_m: int = Field(default=1500, ge=200, le=20000)
    preferences: list[str] = Field(
        default_factory=list,
        description="Free-form hints, e.g. ['quiet', 'shaded', 'coffee'].",
    )

    @field_validator("latitude", "longitude")
    @classmethod
    def _not_zero_island(cls, value: float) -> float:
        if value == 0.0:
            raise ValueError("Coordinates of 0,0 are almost certainly unset.")
        return value


class Place(BaseModel):
    """A candidate destination resolved from OpenStreetMap."""

    name: str
    kind: PlaceKind = "place"
    latitude: float
    longitude: float
    distance_m: float
    address: str = ""
    osm_id: str = ""


class RouteStep(BaseModel):
    """One leg of the walking route to the destination."""

    instruction: str
    distance_m: float


class RouteSummary(BaseModel):
    """Walking route from origin to the chosen place."""

    summary: str
    distance_m: float
    duration_min: float
    steps: list[RouteStep] = Field(default_factory=list)
    source: Literal["osrm", "geodesic"] = "geodesic"


class TriggerResponse(BaseModel):
    """The agent's screen-free action plan."""

    headline: str = Field(..., description="One-line motivational nudge.")
    plan: str = Field(..., description="Concise, actionable screen-free plan.")
    place: Place
    route: RouteSummary
    alternatives: list[Place] = Field(default_factory=list)
    model_used: str = "offline-planner"
    data_source: str = "OpenStreetMap"
