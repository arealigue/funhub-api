from datetime import datetime, timezone
from typing import Any, Optional
import httpx

import jwt
from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.auth import verify_session_token
from app.core.config import settings
from app.core.limiter import limiter
from app.core.supabase import get_supabase_client

router = APIRouter(prefix="/credits", tags=["credits"])


class CreditsResponse(BaseModel):
    credits: int
    source: str


class UseCreditsPayload(BaseModel):
    amount: int = Field(..., gt=0)
    type: str = Field(..., min_length=2, max_length=50)
    game: Optional[str] = None


class VerifyPurchasePayload(BaseModel):
    order_id: str = Field(..., min_length=3, max_length=128)
    package: str = Field(..., min_length=1, max_length=64)


class UseCreditsResponse(BaseModel):
    credits: int
    used: int
    source: str


def _require_device(device_id: Optional[str]) -> str:
    if not device_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-ID header")
    return device_id


def _get_account_id_from_auth(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization header")
    token = auth_header.split(" ", 1)[1]
    try:
        payload = verify_session_token(token)
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None


def _fetch_player(device_id: str) -> Optional[dict[str, Any]]:
    sb = get_supabase_client()
    res = (
        sb.table("players")
        .select("*")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _fetch_account(account_id: str) -> Optional[dict[str, Any]]:
    sb = get_supabase_client()
    res = (
        sb.table("accounts")
        .select("*")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _update_last_active(player_id: str) -> None:
    sb = get_supabase_client()
    sb.table("players").update({"last_active_at": datetime.now(timezone.utc).isoformat()}).eq("id", player_id).execute()


@router.get("", response_model=CreditsResponse, summary="Get available credits")
async def get_credits(x_device_id: Optional[str] = Header(None, convert_underscores=False), authorization: Optional[str] = Header(None)) -> CreditsResponse:
    device_id = _require_device(x_device_id)
    sb = get_supabase_client()

    player = _fetch_player(device_id)
    if not player:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")

    _update_last_active(player["id"])

    account_id = _get_account_id_from_auth(authorization) or player.get("account_id")
    if account_id:
        account = _fetch_account(account_id)
        if not account:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
        return CreditsResponse(credits=account.get("credits", 0), source="account")

    return CreditsResponse(credits=player.get("local_credits", 0), source="local")


@router.post("/use", response_model=UseCreditsResponse, summary="Consume credits")
@limiter.limit("30/minute")
async def use_credits(request: Request, payload: UseCreditsPayload, x_device_id: Optional[str] = Header(None, convert_underscores=False), authorization: Optional[str] = Header(None)) -> UseCreditsResponse:
    device_id = _require_device(x_device_id)
    sb = get_supabase_client()

    player = _fetch_player(device_id)
    if not player:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")

    _update_last_active(player["id"])

    account_id = _get_account_id_from_auth(authorization) or player.get("account_id")
    if account_id:
        account = _fetch_account(account_id)
        if not account:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
        balance = account.get("credits", 0)
        if balance < payload.amount:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient credits")
        new_balance = balance - payload.amount
        sb.table("accounts").update({"credits": new_balance}).eq("id", account_id).execute()
        return UseCreditsResponse(credits=new_balance, used=payload.amount, source="account")

    balance = player.get("local_credits", 0)
    if balance < payload.amount:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient credits")
    new_balance = balance - payload.amount
    sb.table("players").update({"local_credits": new_balance}).eq("id", player["id"]).execute()
    return UseCreditsResponse(credits=new_balance, used=payload.amount, source="local")


@router.post("/verify-purchase", summary="Verify PayPal order and grant credits")
@limiter.limit("5/minute")
async def verify_purchase(
    request: Request,
    payload: VerifyPurchasePayload,
    x_device_id: Optional[str] = Header(None, convert_underscores=False),
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """
    Verify a PayPal payment server-side and grant credits.
    
    Flow:
    1. Frontend completes PayPal checkout, gets orderID
    2. Frontend calls this endpoint with orderID
    3. Backend verifies order with PayPal API
    4. Backend checks orderID not already used (replay attack prevention)
    5. Backend grants credits and records orderID
    """
    device_id = _require_device(x_device_id)
    sb = get_supabase_client()

    # Validate player exists
    player = _fetch_player(device_id)
    if not player:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")

    _update_last_active(player["id"])

    # Check if PayPal is configured
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="PayPal verification not configured",
        )

    # 1. Check if order already used (replay attack prevention)
    existing = (
        sb.table("used_order_ids")
        .select("id")
        .eq("order_id", payload.order_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This order has already been processed",
        )

    # 2. Verify order with PayPal API
    paypal_order = await _verify_paypal_order(payload.order_id)
    if not paypal_order:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or incomplete PayPal order",
        )

    # 3. Validate package and amount
    package_info = HINT_PACKAGES.get(payload.package)
    if not package_info:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown package: {payload.package}",
        )

    # Verify the payment amount matches the package price
    paid_amount = paypal_order.get("amount", 0)
    expected_amount = package_info["price"]
    if abs(paid_amount - expected_amount) > 0.01:  # Allow 1 cent tolerance
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment amount does not match package price",
        )

    # 4. Record the order ID to prevent replay attacks
    sb.table("used_order_ids").insert(
        {
            "order_id": payload.order_id,
            "device_id": device_id,
            "package": payload.package,
            "amount": paid_amount,
        }
    ).execute()

    # 5. Grant credits
    credits_to_add = package_info["credits"]
    account_id = _get_account_id_from_auth(authorization) or player.get("account_id")

    if account_id:
        # Add to account credits
        account = _fetch_account(account_id)
        if account:
            new_balance = account.get("credits", 0) + credits_to_add
            sb.table("accounts").update({"credits": new_balance}).eq("id", account_id).execute()
            
            # Record transaction
            sb.table("credit_transactions").insert(
                {
                    "account_id": account_id,
                    "amount": credits_to_add,
                    "type": "purchase",
                    "metadata": {"order_id": payload.order_id, "package": payload.package},
                }
            ).execute()
            
            return {
                "success": True,
                "credits_added": credits_to_add,
                "new_balance": new_balance,
                "source": "account",
            }

    # Add to local credits
    new_balance = player.get("local_credits", 0) + credits_to_add
    sb.table("players").update({"local_credits": new_balance}).eq("id", player["id"]).execute()

    # Record transaction
    sb.table("credit_transactions").insert(
        {
            "player_id": player["id"],
            "amount": credits_to_add,
            "type": "purchase",
            "metadata": {"order_id": payload.order_id, "package": payload.package},
        }
    ).execute()

    return {
        "success": True,
        "credits_added": credits_to_add,
        "new_balance": new_balance,
        "source": "local",
    }


# Hint packages - must match frontend configuration
HINT_PACKAGES = {
    "starter": {"credits": 5, "price": 0.49},
    "popular": {"credits": 15, "price": 1.29},
    "best-value": {"credits": 50, "price": 2.99},
    "premium-24h": {"credits": -1, "price": 4.99},  # -1 = unlimited (special handling)
}


async def _get_paypal_access_token() -> str:
    """Get PayPal OAuth access token."""
    base_url = (
        "https://api-m.paypal.com"
        if settings.environment == "production"
        else "https://api-m.sandbox.paypal.com"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}/v1/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(settings.paypal_client_id, settings.paypal_client_secret),
            data={"grant_type": "client_credentials"},
        )
        response.raise_for_status()
        return response.json()["access_token"]


async def _verify_paypal_order(order_id: str) -> Optional[dict[str, Any]]:
    """
    Verify a PayPal order by fetching it from PayPal API.
    Returns order details if valid and completed, None otherwise.
    """
    try:
        access_token = await _get_paypal_access_token()

        base_url = (
            "https://api-m.paypal.com"
            if settings.environment == "production"
            else "https://api-m.sandbox.paypal.com"
        )

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{base_url}/v2/checkout/orders/{order_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code != 200:
                return None

            order = response.json()

            # Verify order status is COMPLETED
            if order.get("status") != "COMPLETED":
                return None

            # Extract payment amount
            purchase_units = order.get("purchase_units", [])
            if not purchase_units:
                return None

            amount = purchase_units[0].get("amount", {})
            paid_amount = float(amount.get("value", 0))

            return {
                "order_id": order_id,
                "status": order.get("status"),
                "amount": paid_amount,
                "currency": amount.get("currency_code", "USD"),
            }

    except Exception:
        return None
