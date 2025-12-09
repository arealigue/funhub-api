from datetime import datetime, timedelta, timezone
import jwt

from app.core.config import settings


def create_session_token(account_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": account_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_exp_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_session_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
