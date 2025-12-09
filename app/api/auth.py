from datetime import datetime, timedelta, timezone
import secrets
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.core.auth import create_session_token
from app.core.config import settings
from app.core.limiter import limiter
from app.core.supabase import get_supabase_client

router = APIRouter(prefix="/auth", tags=["auth"])


class RequestOtpPayload(BaseModel):
    email: EmailStr
    device_id: str = Field(..., min_length=3, max_length=255)


class VerifyOtpPayload(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6)
    device_id: str = Field(..., min_length=3, max_length=255)


class VerifyOtpResponse(BaseModel):
    session_token: str
    account: dict[str, Any]
    player: dict[str, Any]


def _get_or_create_player(device_id: str) -> dict[str, Any]:
    sb = get_supabase_client()
    existing = (
        sb.table("players")
        .select("*")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]

    record = {
        "device_id": device_id,
        "display_name": "Anonymous",
        "last_active_at": datetime.now(timezone.utc).isoformat(),
    }
    created = sb.table("players").insert(record, returning="representation").execute()
    return created.data[0]


def _get_account(account_id: str) -> Optional[dict[str, Any]]:
    sb = get_supabase_client()
    res = (
        sb.table("accounts")
        .select("id,email,display_name,credits,is_verified,created_at,updated_at")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


@router.post("/request-otp", summary="Request an OTP code")
@limiter.limit("3/hour")
async def request_otp(payload: RequestOtpPayload, request: Request) -> dict[str, Any]:
    sb = get_supabase_client()
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    record = {
        "email": payload.email,
        "code": code,
        "device_id": payload.device_id,
        "expires_at": expires_at.isoformat(),
    }
    sb.table("otp_codes").insert(record).execute()

    response: dict[str, Any] = {"message": "Code sent", "expires_in": 600}
    if settings.environment.lower() == "development":
        response["debug_code"] = code
    return response


@router.post("/verify-otp", response_model=VerifyOtpResponse, summary="Verify OTP and issue session token")
@limiter.limit("10/minute")
async def verify_otp(payload: VerifyOtpPayload, request: Request) -> VerifyOtpResponse:
    sb = get_supabase_client()
    now = datetime.now(timezone.utc)

    code_res = (
        sb.table("otp_codes")
        .select("*")
        .eq("email", payload.email)
        .eq("code", payload.code)
        .is_("used_at", None)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not code_res.data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or used code")

    code_row = code_res.data[0]
    expires_at = datetime.fromisoformat(code_row["expires_at"])
    if expires_at < now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Code expired")

    sb.table("otp_codes").update({"used_at": now.isoformat()}).eq("id", code_row["id"]).execute()

    account_res = sb.table("accounts").upsert({"email": payload.email}, on_conflict="email", returning="representation").execute()
    account = account_res.data[0]

    player = _get_or_create_player(payload.device_id)

    local_credits = player.get("local_credits") or 0
    if local_credits > 0:
        sb.table("accounts").update({"credits": account.get("credits", 0) + local_credits}).eq("id", account["id"]).execute()
        sb.table("players").update({"local_credits": 0}).eq("id", player["id"]).execute()
        account = _get_account(account["id"]) or account

    sb.table("players").update({"account_id": account["id"], "last_active_at": now.isoformat()}).eq("id", player["id"]).execute()
    player["account_id"] = account["id"]
    player["local_credits"] = 0

    session_token = create_session_token(account_id=account["id"])

    return VerifyOtpResponse(session_token=session_token, account=account, player=player)
