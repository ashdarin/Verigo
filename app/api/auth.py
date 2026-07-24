from __future__ import annotations

import re
import threading
import time
import hashlib
import hmac
import json
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from collections import defaultdict, deque
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.core.mailer import (
    MailDeliveryError,
    MailNotConfiguredError,
    send_email_binding,
    send_email_verification,
    send_password_reset_email,
)
from app.db.auth import User, auth_store


auth_router = APIRouter(prefix="/api/auth")
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=6, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("请输入有效的邮箱地址")
        if value.rsplit("@", 1)[1] in settings.blocked_email_domains:
            raise ValueError("不支持使用临时邮箱注册")
        return value


class RegistrationCredentials(Credentials):
    turnstile_token: str | None = Field(default=None, max_length=2048)


class LoginCredentials(BaseModel):
    account: str | None = Field(default=None, max_length=254)
    email: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=6, max_length=128)

    @model_validator(mode="after")
    def select_account(self) -> "LoginCredentials":
        value = (self.account or self.email or "").strip().lower()
        if not value:
            raise ValueError("请输入邮箱或旧用户名")
        self.account = value
        return self


class PasswordResetRequest(BaseModel):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("请输入有效的邮箱地址")
        return value


class PasswordResetConfirm(PasswordResetRequest):
    code: str = Field(pattern=r"^\d{6}$")
    password: str = Field(min_length=6, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=6, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class VerificationCode(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class EmailBindingRequest(BaseModel):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("请输入有效的邮箱地址")
        return value


class UserResponse(BaseModel):
    id: str
    email: str
    email_verified: bool
    credits: int
    paid_credits: int
    trial_credits: int
    trial_credit_expires_at: str | None
    needs_email_binding: bool
    is_admin: bool


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请输入 API Key 名称")
        return value


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: str
    last_used_at: str | None


class ApiKeyCreatedResponse(ApiKeyResponse):
    token: str = Field(description="Only returned once when the API Key is created.")


class AttemptLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int = 12, window: int = 300) -> None:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] > window:
                events.popleft()
            if len(events) >= limit:
                raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
            events.append(now)


attempt_limiter = AttemptLimiter()


def request_network_hash(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    client_host = forwarded_for.split(",", 1)[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    secret = settings.metrics_salt or "verigo-network-limit-unconfigured"
    return hmac.new(
        secret.encode("utf-8"), client_host.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_turnstile(token: str | None, request: Request) -> None:
    if not settings.turnstile_secret_key:
        return
    if not token:
        raise HTTPException(status_code=403, detail="请先完成人机验证")
    payload = urlencode(
        {
            "secret": settings.turnstile_secret_key,
            "response": token,
            "remoteip": request.client.host if request.client else "",
        }
    ).encode("utf-8")
    try:
        with urlopen(
            UrlRequest(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=payload,
                method="POST",
            ),
            timeout=5,
        ) as response:
            result = json.loads(response.read())
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="人机验证服务暂时不可用") from exc
    if not result.get("success"):
        raise HTTPException(status_code=403, detail="人机验证未通过，请重试")


def serialize_user(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email or user.username,
        email_verified=user.email_verified,
        credits=user.credits,
        paid_credits=user.paid_credits,
        trial_credits=user.trial_credits,
        trial_credit_expires_at=user.trial_credit_expires_at,
        needs_email_binding=user.email is None,
        is_admin=bool(
            user.email_verified
            and user.email
            and user.email.lower() in settings.admin_emails
        ),
    )


def api_key_from_headers(authorization: str | None, api_key: str | None) -> str | None:
    if api_key:
        return api_key.strip()
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def optional_user(
    request: Request,
    session: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
    authorization: Annotated[str | None, Header()] = None,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User | None:
    user = auth_store.user_for_session(session)
    if user is not None:
        request.state.auth_via_api_key = False
        return user
    user = auth_store.user_for_api_key(api_key_from_headers(authorization, api_key))
    request.state.auth_via_api_key = user is not None
    return user


def require_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_session_user(
    request: Request,
    user: Annotated[User | None, Depends(optional_user)],
) -> User:
    user = require_user(user)
    if getattr(request.state, "auth_via_api_key", False):
        raise HTTPException(status_code=403, detail="请使用浏览器登录会话管理 API Key")
    return user


def require_admin(
    request: Request, user: Annotated[User | None, Depends(optional_user)]
) -> User:
    user = require_user(user)
    if getattr(request.state, "auth_via_api_key", False):
        raise HTTPException(status_code=403, detail="API Key 无权访问运营后台")
    if (
        not user.email_verified
        or not user.email
        or user.email.lower() not in settings.admin_emails
    ):
        raise HTTPException(status_code=403, detail="没有运营面板访问权限")
    return user


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


@auth_router.post("/register", response_model=UserResponse, status_code=201)
def register(payload: RegistrationCredentials, request: Request, response: Response) -> UserResponse:
    attempt_limiter.check(
        f"register:{request_network_hash(request)}", limit=5, window=3600
    )
    verify_turnstile(payload.turnstile_token, request)
    try:
        user = auth_store.create_user(payload.email, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    set_session_cookie(response, auth_store.create_session(user.id))
    return serialize_user(user)


@auth_router.post("/login", response_model=UserResponse)
def login(payload: LoginCredentials, request: Request, response: Response) -> UserResponse:
    attempt_limiter.check(f"login:{request.client.host if request.client else 'unknown'}")
    user = auth_store.authenticate(payload.account, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    set_session_cookie(response, auth_store.create_session(user.id))
    return serialize_user(user)


@auth_router.post("/email-verification/request", status_code=204)
def request_email_verification(
    request: Request, user: Annotated[User, Depends(require_user)]
) -> None:
    if not user.email:
        raise HTTPException(status_code=409, detail="旧账号尚未绑定邮箱，请联系管理员")
    attempt_limiter.check(f"verify-email:{user.id}", limit=3, window=900)
    attempt_limiter.check(f"verify-email-network:{request_network_hash(request)}", limit=12, window=900)
    try:
        code = auth_store.create_email_verification(user.id)
        send_email_verification(user.email, code)
    except MailNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="验证邮件服务尚未配置") from exc
    except MailDeliveryError as exc:
        raise HTTPException(status_code=503, detail="验证邮件暂时无法发送") from exc


@auth_router.post("/email-verification/confirm", response_model=UserResponse)
def confirm_email_verification(
    payload: VerificationCode,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> UserResponse:
    try:
        verified = auth_store.confirm_email_verification(
            user.id, payload.code, request_network_hash(request)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize_user(verified)


@auth_router.post("/email-binding/request", status_code=204)
def request_email_binding(
    payload: EmailBindingRequest, user: Annotated[User, Depends(require_user)]
) -> None:
    attempt_limiter.check(f"binding:{user.id}", limit=5, window=900)
    try:
        code = auth_store.create_email_binding(user.id, payload.email)
        send_email_binding(payload.email, code)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MailNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="验证邮件服务尚未配置") from exc
    except MailDeliveryError as exc:
        raise HTTPException(status_code=503, detail="验证邮件暂时无法发送") from exc


@auth_router.post("/email-binding/confirm", response_model=UserResponse)
def confirm_email_binding(
    payload: VerificationCode, user: Annotated[User, Depends(require_user)]
) -> UserResponse:
    try:
        bound = auth_store.confirm_email_binding(user.id, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize_user(bound)


@auth_router.post("/password-reset/request", status_code=204)
def request_password_reset(payload: PasswordResetRequest, request: Request) -> None:
    attempt_limiter.check(f"reset:{request.client.host if request.client else 'unknown'}", limit=5, window=900)
    try:
        code = auth_store.create_password_reset(payload.email)
        if code:
            send_password_reset_email(payload.email, code)
    except MailNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="找回密码邮件服务尚未配置") from exc
    except MailDeliveryError as exc:
        raise HTTPException(status_code=503, detail="找回密码邮件暂时无法发送") from exc


@auth_router.post("/password-reset/confirm", status_code=204)
def confirm_password_reset(payload: PasswordResetConfirm, request: Request) -> None:
    attempt_limiter.check(f"reset-confirm:{request.client.host if request.client else 'unknown'}", limit=8, window=900)
    try:
        auth_store.reset_password(payload.email, payload.code, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@auth_router.post("/password/change", status_code=204)
def change_password(
    payload: PasswordChange,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    session: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
) -> None:
    attempt_limiter.check(f"password-change:{user.id}", limit=5, window=900)
    try:
        auth_store.change_password(
            user.id, payload.current_password, payload.new_password, session
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@auth_router.post("/logout", status_code=204)
def logout(
    response: Response,
    session: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
) -> None:
    auth_store.delete_session(session)
    response.delete_cookie(settings.session_cookie_name, path="/")


@auth_router.delete("/account", status_code=204)
def delete_account(
    response: Response, user: Annotated[User, Depends(require_user)]
) -> None:
    try:
        csv_paths = auth_store.delete_user(user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    results_root = settings.results_dir.resolve()
    for csv_path in csv_paths:
        path = Path(csv_path).resolve()
        if path.is_relative_to(results_root):
            path.unlink(missing_ok=True)
    response.delete_cookie(settings.session_cookie_name, path="/")


@auth_router.get("/me", response_model=UserResponse | None)
def me(user: Annotated[User | None, Depends(optional_user)]) -> UserResponse | None:
    return serialize_user(user) if user else None


@auth_router.get("/api-keys", response_model=list[ApiKeyResponse], summary="List API Keys")
def list_api_keys(user: Annotated[User, Depends(require_session_user)]) -> list[ApiKeyResponse]:
    return [ApiKeyResponse(**item) for item in auth_store.list_api_keys(user.id)]


@auth_router.post(
    "/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=201,
    summary="Create API Key",
)
def create_api_key(
    payload: ApiKeyCreateRequest, user: Annotated[User, Depends(require_session_user)]
) -> ApiKeyCreatedResponse:
    try:
        key, token = auth_store.create_api_key(user.id, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiKeyCreatedResponse(**key, token=token)


@auth_router.delete("/api-keys/{key_id}", status_code=204, summary="Revoke API Key")
def revoke_api_key(
    key_id: str, user: Annotated[User, Depends(require_session_user)]
) -> None:
    if not auth_store.revoke_api_key(user.id, key_id):
        raise HTTPException(status_code=404, detail="API Key 不存在或已撤销")


@auth_router.get("/public-config")
def public_config() -> dict[str, str]:
    return {"turnstile_site_key": settings.turnstile_site_key}
