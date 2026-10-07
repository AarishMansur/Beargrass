"""Environment variables & settings for the Touch Grass Agent monolith."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

  
    app_name: str = "Touch Grass Agent"
    app_version: str = "1.0.0"
    api_prefix: str = "/api/v1"
    debug: bool = False

  
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "nousresearch/hermes-3-llama-3.1-405b"
    llm_temperature: float = 0.6
    llm_max_tokens: int = 700
    llm_timeout_seconds: float = 45.0
    llm_max_tool_rounds: int = 4
    # When no LLM credentials are present, a deterministic local planner keeps
    # the agent useful instead of failing (useful for demos and offline runs).
    allow_offline_planner: bool = True

    # --- OpenStreetMap (privacy-preserving, no proprietary map APIs) ---
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    overpass_base_url: str = "https://overpass-api.de/api/interpreter"
    osrm_base_url: str = "https://routing.openstreetmap.de/routed-foot"
    osm_user_agent: str = "TouchGrassAgent/1.0 (open-source hackathon project)"
    osm_search_limit: int = Field(default=5, ge=1, le=25)
    osm_nearby_radius_m: int = Field(default=1500, ge=200, le=20000)
    osm_language: str = "en"
    osm_timeout_seconds: float = 12.0
    osm_cache_ttl_seconds: int = 300

    # --- Local trigger client defaults ---
    screen_time_threshold_minutes: int = 120
    default_latitude: float = 40.7128
    default_longitude: float = -74.0060

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["*"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def llm_configured(self) -> bool:
        """True when a usable LLM endpoint has been supplied."""
        if not self.llm_base_url:
            return False
        if "localhost" in self.llm_base_url or "127.0.0.1" in self.llm_base_url:
            return True
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (avoids re-reading .env on every request)."""
    return Settings()
