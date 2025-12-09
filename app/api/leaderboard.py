"""
Leaderboard API routes.
Handles score submission with anti-cheat validation and leaderboard retrieval.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.limiter import limiter
from app.core.supabase import get_supabase_client
from app.api.games import (
    VALID_GAMES,
    verify_game_session_token,
    validate_score,
    check_session_used,
    mark_session_used,
)

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


class ScoreSubmitRequest(BaseModel):
    score: int
    session_token: str
    display_name: Optional[str] = None


class ScoreSubmitResponse(BaseModel):
    success: bool
    rank: Optional[int] = None
    message: str


class LeaderboardEntry(BaseModel):
    rank: int
    display_name: str
    score: int
    created_at: str


class LeaderboardResponse(BaseModel):
    game: str
    period: str
    entries: list[LeaderboardEntry]


@router.post("/{game_slug}/submit", response_model=ScoreSubmitResponse)
@limiter.limit("10/minute")
async def submit_score(
    request: Request, game_slug: str, body: ScoreSubmitRequest
):
    """
    Submit a score for a game.
    Requires a valid session token from /games/{game}/start.
    Validates score against anti-cheat rules before saving.
    """
    if game_slug not in VALID_GAMES:
        raise HTTPException(status_code=400, detail=f"Invalid game: {game_slug}")

    # 1. Verify session token
    session_data = verify_game_session_token(body.session_token)

    # Ensure token is for the correct game
    if session_data.get("game_slug") != game_slug:
        raise HTTPException(
            status_code=400,
            detail="Session token is for a different game",
        )

    session_id = session_data["session_id"]
    device_id = session_data["device_id"]
    started_at = session_data["started_at"]

    # 2. Check if session already used
    if await check_session_used(session_id):
        raise HTTPException(
            status_code=400,
            detail="This game session has already been used",
        )

    # 3. Validate score (anti-cheat)
    is_valid, reason = await validate_score(game_slug, body.score, started_at)
    if not is_valid:
        # Log suspicious activity but don't reveal exact reason to client
        # In production, we'd flag this player for review
        raise HTTPException(
            status_code=400,
            detail="Score validation failed",
        )

    # 4. Mark session as used
    await mark_session_used(session_id, game_slug, device_id, body.score, started_at)

    # 5. Save to leaderboard
    supabase = get_supabase_client()
    display_name = body.display_name or "Anonymous"

    supabase.table("leaderboards").insert(
        {
            "game_slug": game_slug,
            "device_id": device_id,
            "display_name": display_name,
            "score": body.score,
        }
    ).execute()

    # 6. Calculate rank
    rank_result = (
        supabase.table("leaderboards")
        .select("id", count="exact")
        .eq("game_slug", game_slug)
        .gt("score", body.score)
        .execute()
    )
    rank = (rank_result.count or 0) + 1

    return ScoreSubmitResponse(
        success=True,
        rank=rank,
        message=f"Score submitted! You ranked #{rank}",
    )


@router.get("/{game_slug}", response_model=LeaderboardResponse)
@limiter.limit("60/minute")
async def get_leaderboard(
    request: Request,
    game_slug: str,
    period: Literal["daily", "weekly", "alltime"] = Query(default="alltime"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    Get the leaderboard for a game.
    Supports filtering by time period: daily, weekly, or alltime.
    """
    if game_slug not in VALID_GAMES:
        raise HTTPException(status_code=400, detail=f"Invalid game: {game_slug}")

    supabase = get_supabase_client()
    query = (
        supabase.table("leaderboards")
        .select("display_name, score, created_at")
        .eq("game_slug", game_slug)
        .order("score", desc=True)
        .limit(limit)
    )

    # Apply time filter
    now = datetime.now(timezone.utc)
    if period == "daily":
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.gte("created_at", start_of_day.isoformat())
    elif period == "weekly":
        days_since_monday = now.weekday()
        start_of_week = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - __import__("datetime").timedelta(days=days_since_monday)
        query = query.gte("created_at", start_of_week.isoformat())

    result = query.execute()

    entries = [
        LeaderboardEntry(
            rank=idx + 1,
            display_name=row["display_name"],
            score=row["score"],
            created_at=row["created_at"],
        )
        for idx, row in enumerate(result.data)
    ]

    return LeaderboardResponse(
        game=game_slug,
        period=period,
        entries=entries,
    )


@router.get("/{game_slug}/me")
@limiter.limit("60/minute")
async def get_my_rank(
    request: Request,
    game_slug: str,
    device_id: str = Query(...),
):
    """
    Get a player's best score and rank for a game.
    """
    if game_slug not in VALID_GAMES:
        raise HTTPException(status_code=400, detail=f"Invalid game: {game_slug}")

    supabase = get_supabase_client()

    # Get player's best score
    best_score_result = (
        supabase.table("leaderboards")
        .select("score, display_name, created_at")
        .eq("game_slug", game_slug)
        .eq("device_id", device_id)
        .order("score", desc=True)
        .limit(1)
        .execute()
    )

    if not best_score_result.data:
        return {"has_score": False, "message": "No scores found for this player"}

    best = best_score_result.data[0]

    # Calculate rank
    rank_result = (
        supabase.table("leaderboards")
        .select("id", count="exact")
        .eq("game_slug", game_slug)
        .gt("score", best["score"])
        .execute()
    )
    rank = (rank_result.count or 0) + 1

    return {
        "has_score": True,
        "rank": rank,
        "score": best["score"],
        "display_name": best["display_name"],
        "achieved_at": best["created_at"],
    }
