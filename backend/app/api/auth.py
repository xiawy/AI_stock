"""Auth endpoints: register / login / logout / me."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.auth import create_access_token, hash_password, verify_password
from app.core.database import get_db
from app.dependencies import get_current_user
from app.models import User
from app.schemas.auth import TokenResponse, UserCreate, UserLogin, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="用户注册",
)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    exists = (
        db.query(User)
        .filter(or_(User.username == payload.username, User.email == payload.email))
        .first()
    )
    if exists is not None:
        field = "用户名" if exists.username == payload.username else "邮箱"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"{field}已被注册"
        )

    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse, summary="用户登录（返回 JWT）")
def login(payload: UserLogin, db: Session = Depends(get_db)) -> dict:
    # Accept username or email for convenience.
    user = (
        db.query(User)
        .filter(or_(User.username == payload.username, User.email == payload.username))
        .first()
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(user.id)
    return {"access_token": token, "token_type": "bearer", "user": user}


@router.post(
    "/login-form",
    response_model=TokenResponse,
    include_in_schema=False,
    summary="OAuth2 表单登录（供 /docs Authorize 按钮使用）",
)
def login_form(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> dict:
    return login(UserLogin(username=form.username, password=form.password), db)


@router.post("/logout", summary="用户登出（JWT 无状态，前端删除 Token 即可）")
def logout(current_user: User = Depends(get_current_user)) -> dict:
    return {"detail": "已登出", "username": current_user.username}


@router.get("/me", response_model=UserResponse, summary="获取当前用户信息")
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
