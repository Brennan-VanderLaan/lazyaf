"""
Service for fetching available AI models from provider APIs.
Caches results to avoid excessive API calls.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Cache duration
CACHE_TTL = timedelta(hours=1)


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str  # "anthropic" or "google"
    description: str = ""


@dataclass
class ModelsCache:
    models: list[ModelInfo] = field(default_factory=list)
    last_fetched: datetime | None = None

    def is_valid(self) -> bool:
        if not self.last_fetched:
            return False
        return datetime.utcnow() - self.last_fetched < CACHE_TTL


class ModelsService:
    def __init__(self):
        self._cache = ModelsCache()
        self._lock = asyncio.Lock()

    async def get_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        """Get available models, using cache if valid."""
        async with self._lock:
            if not force_refresh and self._cache.is_valid():
                return self._cache.models

            models = await self._fetch_all_models()
            self._cache.models = models
            self._cache.last_fetched = datetime.utcnow()
            return models

    async def _fetch_all_models(self) -> list[ModelInfo]:
        """Fetch models from all providers."""
        models = []

        # Fetch in parallel
        anthropic_task = self._fetch_anthropic_models()
        gemini_task = self._fetch_gemini_models()

        anthropic_models, gemini_models = await asyncio.gather(
            anthropic_task, gemini_task, return_exceptions=True
        )

        if isinstance(anthropic_models, list):
            models.extend(anthropic_models)
        else:
            logger.warning(f"Failed to fetch Anthropic models: {anthropic_models}")
            # Add fallback models
            models.extend(self._get_fallback_anthropic_models())

        if isinstance(gemini_models, list):
            models.extend(gemini_models)
        else:
            logger.warning(f"Failed to fetch Gemini models: {gemini_models}")
            # Add fallback models
            models.extend(self._get_fallback_gemini_models())

        return models

    async def _fetch_anthropic_models(self) -> list[ModelInfo]:
        """Fetch available models from Anthropic API."""
        settings = get_settings()
        if not settings.anthropic_api_key:
            logger.warning("No Anthropic API key configured")
            return self._get_fallback_anthropic_models()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": settings.anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    timeout=10.0,
                )
                response.raise_for_status()
                data = response.json()

                models = []
                for item in data.get("data", []):
                    model_id = item.get("id", "")
                    display_name = item.get("display_name", model_id)

                    # Filter to only include claude models (not legacy)
                    if not model_id.startswith("claude-"):
                        continue

                    # Generate description based on model type
                    description = self._get_claude_description(model_id)

                    models.append(ModelInfo(
                        id=model_id,
                        name=display_name,
                        provider="anthropic",
                        description=description,
                    ))

                logger.info(f"Fetched {len(models)} Anthropic models")
                return models

        except Exception as e:
            logger.error(f"Error fetching Anthropic models: {e}")
            raise

    def _get_claude_description(self, model_id: str) -> str:
        """Generate description based on model ID."""
        model_lower = model_id.lower()
        if "opus" in model_lower:
            return "Most powerful, best for complex tasks"
        elif "sonnet" in model_lower:
            if "4-5" in model_lower or "4.5" in model_lower:
                return "Fast and capable, 1M context"
            return "Balanced speed and capability"
        elif "haiku" in model_lower:
            return "Fastest and most affordable"
        return ""

    async def _fetch_gemini_models(self) -> list[ModelInfo]:
        """Fetch available models from Google Gemini API."""
        settings = get_settings()
        if not settings.gemini_api_key:
            logger.warning("No Gemini API key configured")
            return self._get_fallback_gemini_models()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={settings.gemini_api_key}",
                    timeout=10.0,
                )
                response.raise_for_status()
                data = response.json()

                models = []
                for item in data.get("models", []):
                    # Model name is like "models/gemini-2.0-flash"
                    full_name = item.get("name", "")
                    model_id = full_name.replace("models/", "")
                    display_name = item.get("displayName", model_id)
                    description = item.get("description", "")

                    # Filter to only gemini models that support generateContent
                    supported_methods = item.get("supportedGenerationMethods", [])
                    if "generateContent" not in supported_methods:
                        continue

                    # Skip embedding models and other non-generative models
                    if "embed" in model_id.lower() or "aqa" in model_id.lower():
                        continue

                    models.append(ModelInfo(
                        id=model_id,
                        name=display_name,
                        provider="google",
                        description=description[:100] if description else "",
                    ))

                logger.info(f"Fetched {len(models)} Gemini models")
                return models

        except Exception as e:
            logger.error(f"Error fetching Gemini models: {e}")
            raise

    def _get_fallback_anthropic_models(self) -> list[ModelInfo]:
        """Fallback models if API fetch fails."""
        return [
            ModelInfo(id="claude-sonnet-4-5-20250929", name="Claude Sonnet 4.5", provider="anthropic", description="Fast, 1M context"),
            ModelInfo(id="claude-sonnet-4-20250514", name="Claude Sonnet 4", provider="anthropic", description="Stable, fast"),
            ModelInfo(id="claude-opus-4-5-20250929", name="Claude Opus 4.5", provider="anthropic", description="Most powerful"),
        ]

    def _get_fallback_gemini_models(self) -> list[ModelInfo]:
        """Fallback models if API fetch fails."""
        return [
            ModelInfo(id="gemini-2.5-flash", name="Gemini 2.5 Flash", provider="google", description="Fast and efficient"),
            ModelInfo(id="gemini-2.5-pro", name="Gemini 2.5 Pro", provider="google", description="Most capable"),
        ]


# Global instance
models_service = ModelsService()
