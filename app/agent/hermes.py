"""Hermes prompt & tool-calling engine.

Talks to any OpenAI-compatible chat endpoint (OpenRouter, Ollama, or a
direct Hermes harness). Hermes decides *where* to send the human outside;
OpenStreetMap supplies the facts and the route.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

from app.config import Settings, get_settings
from app.schemas import Place, TriggerRequest, TriggerResponse
from app.services.osm import OSMService

logger = logging.getLogger("touch-grass.hermes")

SYSTEM_PROMPT = """\
You are Touch Grass, a terse, warm-hearted agent whose only job is to get a \
human off their screen and into the air. You are NOT a productivity coach and \
you never recommend another app, website, or device.

Rules:
- Use the tools to find real nearby outdoor spots (parks, gardens, cafes with \
seating, viewpoints) and a real walking route. Never invent place names.
- The plan must be doable in one sentence of effort: leave now, walk this route.
- Be concrete: name the place, the walking distance/time, and one detail from \
the data (e.g. a garden or a cafe for a coffee).
- Keep the whole reply short. No emoji. No lectures. No follow-up questions.
- If the tool returns nothing useful, suggest the simplest nearby outdoors \
option honestly instead of fabricating data.
- Respect any stated preferences (quiet, shaded, coffee, etc).

Respond ONLY with a JSON object of the shape:
{"headline": "<max 8 words>", "plan": "<2-4 sentences, actionable>",
 "place_name": "<exact name from tool data or empty string>",
 "preferences_applied": ["<preference>", ...]}
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "find_outdoor_spots",
            "description": (
                "Search OpenStreetMap for nearby parks, gardens, cafes and "
                "viewpoints around a lat/lon within a radius in metres."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "radius_m": {"type": "integer", "default": 1500},
                    "kinds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["park", "cafe", "viewpoint", "outdoor"],
                        },
                        "description": "Optional filter, e.g. ['park','cafe'].",
                    },
                },
                "required": ["latitude", "longitude"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_walking_route",
            "description": (
                "Walking distance, duration and turn directions between two "
                "coordinates using OpenStreetMap data (OSRM foot profile)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_lat": {"type": "number"},
                    "from_lon": {"type": "number"},
                    "to_lat": {"type": "number"},
                    "to_lon": {"type": "number"},
                },
                "required": ["from_lat", "from_lon", "to_lat", "to_lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reverse_location",
            "description": "Human-readable area name for a coordinate (Nominatim).",
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                },
                "required": ["latitude", "longitude"],
            },
        },
    },
]


class HermesAgent:
    """Orchestrates: screen-time alert -> OSM tools -> screen-free plan."""

    def __init__(
        self,
        settings: Settings | None = None,
        osm: OSMService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.osm = osm or OSMService(self.settings)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    async def plan(self, payload: TriggerRequest) -> TriggerResponse:
        # OSM facts are fetched regardless of LLM availability: the response
        # must always carry a real place and a real route.
        candidates = await self.osm.nearby_places(
            payload.latitude,
            payload.longitude,
            payload.radius_m,
            kinds=None,
        )

        headline: str | None = None
        plan_text: str | None = None
        chosen_name = ""
        applied: list[str] = list(payload.preferences)
        model_used = "offline-planner"

        if self.settings.llm_configured:
            try:
                result = await self._run_hermes(payload, candidates)
                headline = result.get("headline")
                plan_text = result.get("plan")
                chosen_name = (result.get("place_name") or "").strip()
                applied = result.get("preferences_applied") or applied
                model_used = self.settings.llm_model
            except Exception as exc:  # noqa: BLE001 - degrade, never 500 on LLM trouble
                logger.warning("Hermes call failed, falling back to planner: %s", exc)
                if not self.settings.allow_offline_planner:
                    raise
        elif not self.settings.allow_offline_planner:
            raise RuntimeError("LLM is not configured and offline planner is disabled.")

        place, alternatives = _select_place(candidates, chosen_name)
        route = await self.osm.walking_route(
            payload.latitude, payload.longitude, place.latitude, place.longitude
        )

        if plan_text is None:
            headline, plan_text = _offline_plan(payload, place, route)

        return TriggerResponse(
            headline=headline or "Screen time's over. Grass time.",
            plan=plan_text,
            place=place,
            route=route,
            alternatives=alternatives,
            model_used=model_used,
        )

    # ------------------------------------------------------------------ #
    # Hermes tool-calling loop
    # ------------------------------------------------------------------ #
    async def _run_hermes(
        self, payload: TriggerRequest, candidates: list[Place]
    ) -> dict[str, Any]:
        user_event = {
            "event": "screen_time_threshold_breached",
            "active_minutes": payload.event.minutes,
            "dominant_application": payload.event.application,
            "device": payload.event.device,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "radius_m": payload.radius_m,
            "preferences": payload.preferences,
            "initial_osm_scan": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "distance_m": round(p.distance_m),
                    "address": p.address,
                }
                for p in candidates
            ],
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_event)},
        ]

        content = ""
        for _round in range(self.settings.llm_max_tool_rounds):
            response = await self._chat(messages)
            message = response.get("choices", [{}])[0].get("message", {})
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content") or ""
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                messages.append(await self._execute_tool_call(call))
        else:
            # Ran out of rounds: ask for the final JSON directly, no tools.
            messages.append(
                {
                    "role": "user",
                    "content": "No more tool calls. Reply with the final JSON object now.",
                }
            )
            response = await self._chat(messages, tools=False)
            content = response.get("choices", [{}])[0].get("message", {}).get("content") or ""

        parsed = _parse_json_block(content)
        if parsed is None:
            raise ValueError(f"Hermes did not return JSON: {content[:200]!r}")
        return parsed

    async def _execute_tool_call(self, call: dict[str, Any]) -> dict[str, Any]:
        function = call.get("function") or {}
        name = function.get("name", "")
        try:
            args = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        try:
            result = await self._dispatch_tool(name, args)
            payload = json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - tools report errors, they don't crash
            logger.warning("tool %s failed: %s", name, exc)
            payload = json.dumps({"error": str(exc)})
        return {"role": "tool", "tool_call_id": call.get("id", name), "content": payload}

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "find_outdoor_spots":
            places = await self.osm.nearby_places(
                latitude=float(args["latitude"]),
                longitude=float(args["longitude"]),
                radius_m=int(args.get("radius_m", self.settings.osm_nearby_radius_m)),
                kinds=args.get("kinds"),
            )
            return [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "distance_m": round(p.distance_m),
                    "latitude": p.latitude,
                    "longitude": p.longitude,
                    "address": p.address,
                }
                for p in places
            ]
        if name == "get_walking_route":
            route = await self.osm.walking_route(
                float(args["from_lat"]),
                float(args["from_lon"]),
                float(args["to_lat"]),
                float(args["to_lon"]),
            )
            return route.model_dump()
        if name == "reverse_location":
            return {
                "area": await self.osm.reverse_geocode(
                    float(args["latitude"]), float(args["longitude"])
                )
            }
        raise ValueError(f"unknown tool: {name}")

    # ------------------------------------------------------------------ #
    # LLM transport
    # ------------------------------------------------------------------ #
    async def _chat(
        self, messages: list[dict[str, Any]], tools: bool = True
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        if "openrouter" in self.settings.llm_base_url:
            headers["HTTP-Referer"] = "https://github.com/aarishmansur/touch-grass"
            headers["X-Title"] = "Touch Grass Agent"

        body: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
        }
        if tools:
            body["tools"] = TOOLS
            body["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            return response.json()


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without the network)
# --------------------------------------------------------------------------- #
def _parse_json_block(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model reply (handles ``` fences)."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip("` \n")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _select_place(candidates: list[Place], chosen_name: str) -> tuple[Place, list[Place]]:
    """Match Hermes' choice against real OSM data; default to the nearest."""
    if chosen_name:
        needle = chosen_name.casefold()
        for index, place in enumerate(candidates):
            if place.name.casefold() == needle or needle in place.name.casefold():
                rest = candidates[:index] + candidates[index + 1 :]
                return place, rest[:3]
    if candidates:
        return candidates[0], candidates[1:4]
    # Nothing on OSM within radius: an honest empty-place fallback.
    return (
        Place(
            name="The nearest patch of outdoors",
            kind="outdoor",
            latitude=0.0,
            longitude=0.0,
            distance_m=0.0,
            address="No mapped POIs inside the search radius",
        ),
        [],
    )


def _offline_plan(
    payload: TriggerRequest, place: Place, route: Any
) -> tuple[str, str]:
    """Deterministic planner used when no LLM credential is configured."""
    minutes = max(1, round(route.duration_min))
    if route.distance_m < 1000:
        distance = f"{round(route.distance_m)} m"
    else:
        distance = f"{route.distance_m / 1000:.1f} km"
    detail = {
        "park": "a proper patch of grass and sky",
        "cafe": "a coffee outside, no feed attached",
        "viewpoint": "some elevation and a horizon to look at",
        "outdoor": "daylight and fresh air",
        "place": "daylight and fresh air",
    }.get(place.kind, "daylight and fresh air")

    if place.distance_m == 0.0 and place.address.startswith("No mapped"):
        plan = (
            f"Nothing mapped within {payload.radius_m} m, so walk {distance} in "
            "any green direction and pick the first bench you see. "
            "Leave the phone in your pocket on the way out."
        )
        return "No signal needed.", plan

    plan = (
        f"Walk {distance} (~{minutes} min) to {place.name} for {detail}. "
        f"Route: {route.steps[0].instruction if route.steps else 'head straight out'}"
        f"{' then follow the mapped path' if len(route.steps) > 1 else ''}. "
        "Screen stays in your pocket until you are back."
    )
    return "Go touch grass.", plan
