"""OpenStreetMap wrappers: Nominatim (geocoding), Overpass (nearby POIs),
OSRM (walking routes). No proprietary map APIs, no API keys, no tracking."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Iterable, Sequence

import httpx

from app.config import Settings, get_settings
from app.schemas import Place, PlaceKind, RouteStep, RouteSummary

logger = logging.getLogger("touch-grass.osm")

WALKING_SPEED_M_S = 1.35  # ~4.9 km/h, WHO-ish default walking pace

# Overpass tags -> our simplified place taxonomy.
_KIND_RULES: tuple[tuple[PlaceKind, tuple[tuple[str, frozenset[str]], ...]], ...] = (
    ("cafe", ((("amenity", frozenset({"cafe", "bar", "pub"})),),)),
    (
        "park",
        (
            (
                (
                    "leisure",
                    frozenset(
                        {"park", "garden", "nature_reserve", "common", "recreation_ground"}
                    ),
                ),
            ),
            (("landuse", frozenset({"grass", "forest", "meadow", "orchard", "village_green"})),),
        ),
    ),
    ("viewpoint", ((("tourism", frozenset({"viewpoint"})),), (("natural", frozenset({"peak"})),))),
    (
        "outdoor",
        (
            (("tourism", frozenset({"picnic_site", "camp_site", "attraction"})),),
            (("natural", frozenset({"wood", "beach", "coastline", "water"})),),
        ),
    ),
)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def classify(tags: dict[str, str]) -> PlaceKind | None:
    """Map raw OSM tags onto the agent's place taxonomy."""
    for kind, groups in _KIND_RULES:
        for group in groups:
            if any(tags.get(key) in values for key, values in group):
                return kind
    return None


def viewbox(lat: float, lon: float, radius_m: int) -> str:
    """Bounding box `left,top,right,bottom` around a point (Nominatim viewbox)."""
    d_lat = radius_m / 111_320.0
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    d_lon = radius_m / (111_320.0 * cos_lat)
    return f"{lon - d_lon},{lat + d_lat},{lon + d_lon},{lat - d_lat}"


def _element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    lat = element.get("lat")
    lon = element.get("lon")
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    center = element.get("center") or {}
    if "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


class OSMService:
    """Thin async client for the free, usage-limited OSM endpoints.

    Requests are rate-friendly: a shared User-Agent, a short TTL cache, and
    no more than one in-flight Overpass query at a time.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: dict[str, tuple[float, list[Place]]] = {}
        self._overpass_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.osm_user_agent,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Nearby places (Overpass primary, Nominatim fallback)
    # ------------------------------------------------------------------ #
    async def nearby_places(
        self,
        latitude: float,
        longitude: float,
        radius_m: int | None = None,
        kinds: Sequence[str] | None = None,
    ) -> list[Place]:
        radius_m = radius_m or self.settings.osm_nearby_radius_m
        key = f"{latitude:.4f},{longitude:.4f},{radius_m}"
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.settings.osm_cache_ttl_seconds:
            places = cached[1]
        else:
            places = await self._overpass_nearby(latitude, longitude, radius_m)
            if not places:
                places = await self._nominatim_nearby(latitude, longitude, radius_m)
            self._cache[key] = (time.monotonic(), places)

        if kinds:
            wanted = {kind.lower() for kind in kinds}
            filtered = [p for p in places if p.kind in wanted]
            places = filtered or places
        return places[: self.settings.osm_search_limit]

    async def _overpass_nearby(
        self, latitude: float, longitude: float, radius_m: int
    ) -> list[Place]:
        query = f"""
[out:json][timeout:25];
(
  node["leisure"~"^(park|garden|nature_reserve|common|recreation_ground)$"](around:{radius_m},{latitude},{longitude});
  way["leisure"~"^(park|garden|nature_reserve|common|recreation_ground)$"](around:{radius_m},{latitude},{longitude});
  node["amenity"~"^(cafe|bar|pub)$"](around:{radius_m},{latitude},{longitude});
  way["amenity"~"^(cafe|bar|pub)$"](around:{radius_m},{latitude},{longitude});
  node["tourism"~"^(viewpoint|picnic_site|camp_site)$"](around:{radius_m},{latitude},{longitude});
  way["tourism"~"^(viewpoint|picnic_site|camp_site)$"](around:{radius_m},{latitude},{longitude});
  node["natural"~"^(peak|wood|beach)$"](around:{radius_m},{latitude},{longitude});
);
out center tags 100;
""".strip()
        async with self._overpass_lock:
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.osm_timeout_seconds, headers=self._headers()
                ) as client:
                    response = await client.post(
                        self.settings.overpass_base_url, data={"data": query}
                    )
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError, ValueError) as exc:
                logger.warning("Overpass query failed: %s", exc)
                return []

        places = self._parse_overpass(payload, latitude, longitude)
        logger.info("Overpass returned %d usable places", len(places))
        return places

    def _parse_overpass(
        self, payload: dict[str, Any], latitude: float, longitude: float
    ) -> list[Place]:
        seen: set[tuple[str, str]] = set()
        places: list[Place] = []
        for element in payload.get("elements", []):
            tags = element.get("tags") or {}
            name = (tags.get("name") or tags.get("name:en") or "").strip()
            if not name:
                continue
            kind = classify(tags)
            if kind is None:
                continue
            point = _element_point(element)
            if point is None:
                continue
            dedupe_key = (name.casefold(), kind)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            lat, lon = point
            places.append(
                Place(
                    name=name,
                    kind=kind,
                    latitude=lat,
                    longitude=lon,
                    distance_m=haversine_m(latitude, longitude, lat, lon),
                    address=_format_address(tags),
                    osm_id=f"{element.get('type', '?')}/{element.get('id', 0)}",
                )
            )
        places.sort(key=lambda p: p.distance_m)
        return places

    async def _nominatim_nearby(
        self, latitude: float, longitude: float, radius_m: int
    ) -> list[Place]:
        """Fallback: bounded Nominatim search (public, no key, 1 req/s)."""
        found: list[Place] = []
        queries = ("park", "cafe", "viewpoint")
        async with httpx.AsyncClient(
            timeout=self.settings.osm_timeout_seconds, headers=self._headers()
        ) as client:
            for term in queries:
                params = {
                    "q": term,
                    "format": "jsonv2",
                    "limit": 5,
                    "addressdetails": 0,
                    "bounded": 1,
                    "viewbox": viewbox(latitude, longitude, radius_m),
                }
                try:
                    response = await client.get(
                        f"{self.settings.nominatim_base_url}/search", params=params
                    )
                    response.raise_for_status()
                    rows = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("Nominatim search failed for %r: %s", term, exc)
                    continue
                for row in rows:
                    try:
                        lat, lon = float(row["lat"]), float(row["lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    distance = haversine_m(latitude, longitude, lat, lon)
                    if distance > radius_m:
                        continue
                    name = (row.get("display_name") or term).split(",")[0].strip()
                    found.append(
                        Place(
                            name=name,
                            kind=term if term in {"park", "cafe", "viewpoint"} else "outdoor",
                            latitude=lat,
                            longitude=lon,
                            distance_m=distance,
                            address=row.get("display_name", ""),
                            osm_id=f"nominatim/{row.get('place_id', 0)}",
                        )
                    )
                await asyncio.sleep(1.1)  # Nominatim fair-use: max 1 request/second
        found.sort(key=lambda p: p.distance_m)
        deduped = {p.name.casefold(): p for p in found}
        return list(deduped.values())

    # ------------------------------------------------------------------ #
    # Reverse geocoding (Nominatim)
    # ------------------------------------------------------------------ #
    async def reverse_geocode(self, latitude: float, longitude: float) -> str:
        params = {
            "lat": latitude,
            "lon": longitude,
            "format": "jsonv2",
            "zoom": 14,
            "accept-language": self.settings.osm_language,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.osm_timeout_seconds, headers=self._headers()
            ) as client:
                response = await client.get(
                    f"{self.settings.nominatim_base_url}/reverse", params=params
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Nominatim reverse failed: %s", exc)
            return ""
        address = payload.get("address") or {}
        parts = [
            address.get("suburb")
            or address.get("neighbourhood")
            or address.get("village")
            or address.get("town")
            or address.get("city"),
            address.get("state"),
            address.get("country_code", "").upper() or None,
        ]
        return ", ".join(part for part in parts if part)

    # ------------------------------------------------------------------ #
    # Walking route (OSRM foot profile, geodesic fallback)
    # ------------------------------------------------------------------ #
    async def walking_route(
        self,
        origin_lat: float,
        origin_lon: float,
        dest_lat: float,
        dest_lon: float,
    ) -> RouteSummary:
        osrm = await self._osrm_route(origin_lat, origin_lon, dest_lat, dest_lon)
        if osrm is not None:
            return osrm
        distance = haversine_m(origin_lat, origin_lon, dest_lat, dest_lon)
        return RouteSummary(
            summary=_geodesic_summary(distance),
            distance_m=round(distance),
            duration_min=round(distance / WALKING_SPEED_M_S / 60, 1),
            steps=[
                RouteStep(
                    instruction="Head straight out the door and walk toward the destination.",
                    distance_m=round(distance),
                )
            ],
            source="geodesic",
        )

    async def _osrm_route(
        self, origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
    ) -> RouteSummary | None:
        url = (
            f"{self.settings.osrm_base_url}/route/v1/foot/"
            f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
        )
        params = {"overview": "false", "steps": "true", "alternatives": "false"}
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.osm_timeout_seconds, headers=self._headers()
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OSRM route failed, using geodesic estimate: %s", exc)
            return None

        routes = payload.get("routes") or []
        if payload.get("code") != "Ok" or not routes:
            return None
        route = routes[0]
        steps: list[RouteStep] = []
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                instruction = _osrm_instruction(step)
                if instruction:
                    steps.append(
                        RouteStep(
                            instruction=instruction,
                            distance_m=float(step.get("distance", 0.0)),
                        )
                    )
        distance = float(route["distance"])
        duration = float(route["duration"])
        # Some public OSRM mirrors ignore the foot profile and return car
        # speeds; trust the value only if it implies a human pace.
        if duration / max(distance, 1.0) > 1 / WALKING_SPEED_M_S * 2.5:
            duration = distance / WALKING_SPEED_M_S
        return RouteSummary(
            summary=f"{round(distance)} m on foot",
            distance_m=round(distance),
            duration_min=round(duration / 60, 1),
            steps=steps[:8],
            source="osrm",
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _format_address(tags: dict[str, str]) -> str:
    street = tags.get("addr:street")
    number = tags.get("addr:housenumber")
    city = tags.get("addr:city")
    bits = [f"{street} {number}".strip() if street else None, city]
    return ", ".join(bit for bit in bits if bit)


def _geodesic_summary(distance_m: float) -> str:
    if distance_m < 1000:
        return f"{round(distance_m)} m walk (straight-line estimate)"
    return f"{distance_m / 1000:.1f} km walk (straight-line estimate)"


def _osrm_instruction(step: dict[str, Any]) -> str:
    maneuver = step.get("maneuver") or {}
    modifier = maneuver.get("modifier")
    road = step.get("name") or "the path"
    bearing = maneuver.get("type", "continue")
    phrases = {
        "depart": f"Start on {road}",
        "turn": f"Turn {modifier or 'slightly'} onto {road}",
        "continue": f"Continue on {road}",
        "new name": f"Continue onto {road}",
        "arrive": "Arrive at your destination",
    }
    text = phrases.get(bearing)
    if text is None:
        return ""
    return text


def _dedupe(places: Iterable[Place]) -> list[Place]:
    seen: set[tuple[str, str]] = set()
    out: list[Place] = []
    for place in places:
        key = (place.name.casefold(), place.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(place)
    return out
