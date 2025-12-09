from fastapi import APIRouter

from .auth import router as auth_router
from .credits import router as credits_router
from .games import router as games_router
from .health import router as health_router
from .leaderboard import router as leaderboard_router
from .players import router as players_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(players_router)
api_router.include_router(auth_router)
api_router.include_router(credits_router)
api_router.include_router(games_router)
api_router.include_router(leaderboard_router)
