from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import jwt
from fastapi import HTTPException, status

from app.config import settings


ALGORITHM = "HS256"

def create_access_token(subject: str) -> str:
    """Create a signed JWT for the given subject (username)."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=settings.jwt_expires_min)

    payload: Dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)

def verify_token(token: str) -> str:
    """Verify JWT and return subject (username)."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        sub = payload.get("sub")
        if not sub:
            raise ValueError("Missing 'sub'")
        return str(sub)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_device_key(x_device_key: str | None) -> None:
    if not x_device_key or x_device_key != settings.device_api_key:
        raise HTTPException(status_code=401, detail="Invalid device key")
