from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.auth import create_access_token
from app.config import settings

router = APIRouter(tags=["auth"])


@router.post("/token")
def token(form: OAuth2PasswordRequestForm = Depends()):
    # NOTE: OAuth2PasswordRequestForm uses form-data fields: username, password
    if form.username != settings.admin_username or form.password != settings.admin_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access_token = create_access_token(subject=form.username)
    return {"access_token": access_token, "token_type": "bearer"}