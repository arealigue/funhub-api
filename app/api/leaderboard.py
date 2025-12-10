"""
Leaderboard API routes.
Handles score submission with anti-cheat validation and leaderboard retrieval.
"""

from datetime import datetime, timedelta, timezone
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


async def get_or_create_player(device_id: str, display_name: str) -> str:
    """Get existing player by device_id or create new one. Returns player UUID."""
    supabase = get_supabase_client()
    
    # Check if player exists
    existing = (
        supabase.table("players")
        .select("id")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    
    if existing.data:
        player_id = existing.data[0]["id"]
        # Update display name and last_active
        supabase.table("players").update({
            "display_name": display_name,
            "last_active_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", player_id).execute()
        return player_id
    
    # Create new player
    result = (
        supabase.table("players")
        .insert({
            "device_id": device_id,
            "display_name": display_name,
        })
        .execute()
    )
    return result.data[0]["id"]


async def get_game_id(game_slug: str) -> str:
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
        raise HTTPException(status_code=404, detail=f"Game not found: {game_slug}")
    return result.data[0]["id"]


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
        raise HTTPException(
            status_code=400,
            detail="Score validation failed",
        )

    # 4. Mark session as used
    await mark_session_used(session_id, game_slug, device_id, body.score, started_at)

    # 5. Get game_id and player_id
    supabase = get_supabase_client()
    display_name = body.display_name or "Anonymous"
    
    game_id = await get_game_id(game_slug)
    player_id = await get_or_create_player(device_id, display_name)

    # 6. Save to leaderboard (upsert - only keep best score per player/game)
    # Check if player already has a score for this game
    existing = (
        supabase.table("leaderboards")
        .select("id, score")
        .eq("game_id", game_id)
        .eq("player_id", player_id)
        .limit(1)
        .execute()
    )

    is_new_best = False
    if existing.data:
        # Player has existing score - only update if new score is higher
        existing_score = existing.data[0]["score"]
        if body.score > existing_score:
            supabase.table("leaderboards").update({
                "score": body.score,
            }).eq("id", existing.data[0]["id"]).execute()
            is_new_best = True
    else:
        # No existing score - insert new record
        supabase.table("leaderboards").insert({
            "game_id": game_id,
            "player_id": player_id,
            "score": body.score,
        }).execute()
        is_new_best = True

    # 7. Calculate rank
    rank_result = (
        supabase.table("leaderboards")
        .select("id", count="exact")
        .eq("game_id", game_id)
        .gt("score", body.score)
        .execute()
    )
    rank = (rank_result.count or 0) + 1

    if is_new_best:
        message = f"New personal best! You ranked #{rank}"
    else:
        message = f"Score submitted. Your best is still higher. Current rank: #{rank}"

    return ScoreSubmitResponse(
        success=True,
        rank=rank,
        message=message,
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
    game_id = await get_game_id(game_slug)
    
    # Join leaderboards with players to get display_name
    query = (
        supabase.table("leaderboards")
        .select("score, created_at, players(display_name)")
        .eq("game_id", game_id)
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
        ) - timedelta(days=days_since_monday)
        query = query.gte("created_at", start_of_week.isoformat())

    result = query.execute()

    entries = [
        LeaderboardEntry(
            rank=idx + 1,
            display_name=row.get("players", {}).get("display_name", "Anonymous") if row.get("players") else "Anonymous",
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
    game_id = await get_game_id(game_slug)
    
    # Get player by device_id
    player_result = (
        supabase.table("players")
        .select("id, display_name")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    
    if not player_result.data:
        return {"has_score": False, "message": "No scores found for this player"}
    
    player = player_result.data[0]
    player_id = player["id"]

    # Get player's score for this game
    score_result = (
        supabase.table("leaderboards")
        .select("score, created_at")
        .eq("game_id", game_id)
        .eq("player_id", player_id)
        .limit(1)
        .execute()
    )

    if not score_result.data:
        return {"has_score": False, "message": "No scores found for this player"}

    best = score_result.data[0]

    # Calculate rank
    rank_result = (
        supabase.table("leaderboards")
        .select("id", count="exact")
        .eq("game_id", game_id)
        .gt("score", best["score"])
        .execute()
    )
    rank = (rank_result.count or 0) + 1

    return {
        "has_score": True,
        "rank": rank,
        "score": best["score"],
        "display_name": player["display_name"],
        "achieved_at": best["created_at"],
    }
