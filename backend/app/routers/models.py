"""
API endpoints for fetching available AI models.
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.models_service import models_service

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelResponse(BaseModel):
    id: str
    name: str
    provider: str
    description: str


class ModelsListResponse(BaseModel):
    models: list[ModelResponse]
    anthropic: list[ModelResponse]
    google: list[ModelResponse]


@router.get("", response_model=ModelsListResponse)
async def list_models(refresh: bool = Query(False, description="Force refresh from APIs")):
    """
    Get available AI models from all providers.

    Models are cached for 1 hour. Use refresh=true to force a fresh fetch.
    """
    models = await models_service.get_models(force_refresh=refresh)

    model_responses = [
        ModelResponse(
            id=m.id,
            name=m.name,
            provider=m.provider,
            description=m.description,
        )
        for m in models
    ]

    # Split by provider
    anthropic_models = [m for m in model_responses if m.provider == "anthropic"]
    google_models = [m for m in model_responses if m.provider == "google"]

    return ModelsListResponse(
        models=model_responses,
        anthropic=anthropic_models,
        google=google_models,
    )
