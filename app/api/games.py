"""
Game sessions API routes.
Handles game session tokens for anti-cheat score validation.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.config import settings
from app.core.limiter import limiter
from app.core.supabase import get_supabase_client

router = APIRouter(prefix="/games", tags=["games"])

# Valid game slugs
VALID_GAMES = ["mixmo", "quizmo"]

# Max score calculation rules per game
# These are sanity checks - if score exceeds these, it's likely cheating
SCORE_RULES = {
    "quizmo": {
        # QuizMo: Max ~10 points per 6 seconds (answer time)
        # Formula: time_seconds / 6 * 10
        "max_score_per_second": 10 / 6,  # ~1.67 points/sec
        "max_absolute": 10000,  # Hard cap regardless of time
    },
    "mixmo": {
        # MixMo: Max ~5 discoveries per minute
        # Formula: time_seconds / 12
        "max_score_per_second": 1 / 12,  # ~0.083 score/sec
        "max_absolute": 1000,  # Hard cap regardless of time
    },
}


class GameStartRequest(BaseModel):
    device_id: str


class GameStartResponse(BaseModel):
    session_token: str
    started_at: str


class GameSessionPayload(BaseModel):
    """Payload embedded in the game session JWT."""

    session_id: str
    game_slug: str
    device_id: str
    started_at: float  # Unix timestamp


def create_game_session_token(game_slug: str, device_id: str) -> tuple[str, datetime]:
    """Create a signed JWT for a game session."""
    now = datetime.now(timezone.utc)
    session_id = str(uuid4())

    payload = {
        "session_id": session_id,
        "game_slug": game_slug,
        "device_id": device_id,
        "started_at": now.timestamp(),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),  # Session valid for 2 hours
    }

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, now


def verify_game_session_token(token: str) -> dict:
    """Verify and decode a game session JWT."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="Game session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=400, detail="Invalid game session token")


@router.post("/{game_slug}/start", response_model=GameStartResponse)
@limiter.limit("30/minute")
async def start_game_session(
    request: Request, game_slug: str, body: GameStartRequest
):
    """
    Start a new game session.
    Returns a signed session token that must be submitted with the score.
    """
    if game_slug not in VALID_GAMES:
        raise HTTPException(status_code=400, detail=f"Invalid game: {game_slug}")

    token, started_at = create_game_session_token(game_slug, body.device_id)

    return GameStartResponse(
        session_token=token, started_at=started_at.isoformat()
    )


async def validate_score(game_slug: str, score: int, started_at: float) -> tuple[bool, str]:
    """
    Validate if a score is possible given the time elapsed.
    Returns (is_valid, reason).
    """
    if score < 0:
        return False, "Score cannot be negative"

    now = datetime.now(timezone.utc).timestamp()
    elapsed_seconds = now - started_at

    if elapsed_seconds < 1:
        return False, "Game ended too quickly"

    rules = SCORE_RULES.get(game_slug)
    if not rules:
        return True, "No rules defined"  # Allow by default if game not configured

    # Check absolute maximum
    if score > rules["max_absolute"]:
        return False, f"Score exceeds maximum allowed ({rules['max_absolute']})"

    # Check score relative to time
    max_possible = elapsed_seconds * rules["max_score_per_second"]
    if score > max_possible * 1.5:  # 50% buffer for edge cases
        return False, f"Score too high for elapsed time ({elapsed_seconds:.1f}s)"

    return True, "Valid"


async def check_session_used(session_token: str) -> bool:
    """Check if a game session has already been used to submit a score."""
    supabase = get_supabase_client()
    result = (
        supabase.table("game_sessions")
        .select("id")
        .eq("session_token", session_token)
        .execute()
    )
    return len(result.data) > 0


async def get_player_id_by_device(device_id: str) -> str | None:
    """Get player UUID by device_id."""
    supabase = get_supabase_client()
    result = (
        supabase.table("players")
        .select("id")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return result.data[0]["id"]


async def get_game_id_by_slug(game_slug: str) -> str | None:
    """Get game UUID by slug."""
    supabase = get_supabase_client()
    result = (
        supabase.table("games")
        .select("id")
        .eq("slug", game_slug)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return result.data[0]["id"]


async def mark_session_used(
    session_token: str, game_slug: str, device_id: str, score: int, started_at: float
):
    """Mark a game session as used after score submission."""
    supabase = get_supabase_client()
    ended_at = datetime.now(timezone.utc)
    
    # Get player_id and game_id
    player_id = await get_player_id_by_device(device_id)
    game_id = await get_game_id_by_slug(game_slug)
    
    if not player_id or not game_id:
        # Can't mark session without valid player/game, but don't fail
        return

    supabase.table("game_sessions").insert(
        {
            "session_token": session_token,
            "player_id": player_id,
            "game_id": game_id,
            "score": score,
            "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
            "ended_at": ended_at.isoformat(),
        }
    ).execute()
