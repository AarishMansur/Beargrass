"""Versioned API routes for the Touch Grass Agent monolith."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.schemas import TriggerRequest, TriggerResponse

logger = logging.getLogger("touch-grass.api")
router = APIRouter()


@router.post(
    "/trigger",
    response_model=TriggerResponse,
    summary="Screen-time alert -> outdoor action plan",
)
async def trigger(payload: TriggerRequest, request: Request) -> TriggerResponse:
    """Accept a screen-time event and return a concise screen-free plan."""
    from app.agent.hermes import HermesAgent

    settings = request.app.state.settings
    agent = HermesAgent(settings)
    try:
        result = await agent.plan(payload)
    except Exception as exc:  # pragma: no cover - defensive surface
        logger.exception("agent planning failed")
        raise HTTPException(status_code=502, detail=f"Planning failed: {exc}") from exc
    return result
