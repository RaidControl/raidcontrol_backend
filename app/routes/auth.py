from fastapi import APIRouter, HTTPException

from app.auth import create_access_token
from app.config import settings
from app.schemas import LoginRequest, LoginResponse

router = APIRouter(prefix="/api/v1/admin/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def admin_login(req: LoginRequest):
    if req.username != settings.admin_username or req.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(subject=req.username)
    return LoginResponse(access_token=token, expires_in=settings.jwt_expires_min * 60)
