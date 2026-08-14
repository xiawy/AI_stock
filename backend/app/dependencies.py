"""Shared FastAPI dependencies (DB session, current user)."""

from __future__ import annotations

import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.auth import decode_access_token
from app.core.database import get_db
from app.models import User

# tokenUrl is relative to the router mount point (/api) for the docs UI.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

_CREDENTIALS_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="登录已过期或未登录",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(token)
    except pyjwt.PyJWTError:
        raise _CREDENTIALS_exc from None

    user_id = payload.get("sub")
    if user_id is None:
        raise _CREDENTIALS_exc

    user = db.get(User, int(user_id))
    if user is None:
        raise _CREDENTIALS_exc
    return user
