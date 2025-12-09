from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.core.supabase import get_supabase_client

router = APIRouter(prefix="/players", tags=["players"])


class PlayerRegisterRequest(BaseModel):
    device_id: str = Field(..., min_length=3, max_length=255)
    display_name: Optional[str] = None


class PlayerResponse(BaseModel):
    player: dict[str, Any]
    account: Optional[dict[str, Any]] = None


def _fetch_player(device_id: str) -> Optional[dict[str, Any]]:
    sb = get_supabase_client()
    res = (
        sb.table("players")
        .select("*")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0]


def _fetch_account(account_id: str) -> Optional[dict[str, Any]]:
    sb = get_supabase_client()
    res = (
        sb.table("accounts")
        .select("id,email,display_name,credits,is_verified,created_at,updated_at")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0]


@router.post("/register", response_model=PlayerResponse, summary="Register or update a player")
async def register_player(payload: PlayerRegisterRequest) -> PlayerResponse:
    sb = get_supabase_client()
    record = {
        "device_id": payload.device_id,
        "display_name": payload.display_name or "Anonymous",
        "last_active_at": datetime.now(timezone.utc).isoformat(),
    }
    res = sb.table("players").upsert(record, on_conflict="device_id", returning="representation").execute()
    player = res.data[0]
    account = _fetch_account(player["account_id"]) if player.get("account_id") else None
    return PlayerResponse(player=player, account=account)


@router.get("/me", response_model=PlayerResponse, summary="Get player and linked account")
async def get_me(x_device_id: Optional[str] = Header(None, convert_underscores=False)) -> PlayerResponse:
    if not x_device_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-ID header")

    player = _fetch_player(x_device_id)
    if not player:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")

    sb = get_supabase_client()
    sb.table("players").update({"last_active_at": datetime.now(timezone.utc).isoformat()}).eq("id", player["id"]).execute()

    account = _fetch_account(player["account_id"]) if player.get("account_id") else None
    return PlayerResponse(player=player, account=account)
