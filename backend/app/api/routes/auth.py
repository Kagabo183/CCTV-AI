from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.security import ACCESS_TOKEN_COOKIE, create_access_token, hash_password, verify_password
from app.models import User
from app.schemas.api import AuthOut, LoginIn, RegisterIn, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

# Constant-time-ish login: always run bcrypt, even for unknown emails.
_DUMMY_HASH = hash_password("not-a-real-password")


def _issue(response: Response, user: User) -> AuthOut:
    token, expires_at = create_access_token(user.id)
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        token,
        httponly=True,
        secure=get_settings().environment == "production",
        samesite="lax",
        expires=expires_at,
        path="/",
    )
    return AuthOut(user=UserOut.model_validate(user), access_token=token, expires_at=expires_at)


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, response: Response, db: DbSession) -> AuthOut:
    email = body.email.lower()
    if (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    user = User(email=email, password_hash=hash_password(body.password), full_name=body.full_name, preferred_language=body.preferred_language)
    db.add(user)
    await db.commit()
    return _issue(response, user)


@router.post("/login", response_model=AuthOut)
async def login(body: LoginIn, response: Response, db: DbSession) -> AuthOut:
    user = (await db.execute(select(User).where(func.lower(User.email) == body.email.lower()))).scalar_one_or_none()
    valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not valid or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return _issue(response, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user
