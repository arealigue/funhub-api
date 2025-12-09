from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/health", summary="Health check")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": settings.version, "environment": settings.environment}
