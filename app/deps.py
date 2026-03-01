from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from app.auth import verify_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    return verify_token(token)