from datetime import datetime, timezone
import hashlib
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db.models import ApiKey, AuditLog, User, Workspace, WorkspaceMember
from app.services.auth import create_access_token, decode_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User | None:
    if not credentials:
        if get_settings().auth_required:
            raise HTTPException(status_code=401, detail="Authentication required")
        return None
    token = credentials.credentials
    if token.startswith("ak_"):
        key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        api_key = db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
        now = datetime.now(timezone.utc)
        if not api_key or api_key.revoked_at is not None or (api_key.expires_at is not None and api_key.expires_at <= now):
            raise HTTPException(status_code=401, detail="Invalid or expired API key")
        user = db.get(User, api_key.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User is inactive or does not exist")
        api_key.last_used_at = now
        return user
    try:
        user_id = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User is inactive or does not exist")
    return user


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)


def public_user(user: User) -> dict:
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name, "is_active": user.is_active}


def audit(db: Session, user: User | None, action: str, resource_type: str | None = None, resource_id: UUID | None = None, details: dict | None = None):
    db.add(AuditLog(
        user_id=user.id if user else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else None,
        details=details or {},
    ))


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(request: RegisterRequest, db: Session = Depends(get_db)):
    email = request.email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise HTTPException(422, "Enter a valid email address")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    user = User(email=email, password_hash=hash_password(request.password), full_name=request.full_name)
    db.add(user)
    db.flush()
    slug = email.split("@")[0].replace(".", "-")[:80] or "workspace"
    base = slug
    n = 2
    while db.scalar(select(Workspace).where(Workspace.slug == slug)):
        slug = f"{base}-{n}"
        n += 1
    workspace = Workspace(name=f"{request.full_name or email.split('@')[0]}'s Workspace", slug=slug, created_by=user.id)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    audit(db, user, "user.register")
    db.commit()
    db.refresh(user)
    return {"user": public_user(user), "access_token": create_access_token(user.id), "token_type": "bearer"}


@router.post("/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == request.email.strip().lower()))
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    audit(db, user, "user.login")
    db.commit()
    return {"user": public_user(user), "access_token": create_access_token(user.id), "token_type": "bearer"}


@router.get("/me")
def me(user: User | None = Depends(current_user)):
    if user is None:
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": public_user(user)}


@router.post("/logout")
def logout(user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    audit(db, user, "user.logout")
    db.commit()
    return {"ok": True}

@router.get("/audit")
def audit_logs(limit: int = 50, user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    if user is None:
        raise HTTPException(401, "Authentication required to view audit logs")
    rows = db.scalars(
        select(AuditLog).where(AuditLog.user_id == user.id).order_by(AuditLog.created_at.desc()).limit(max(1, min(limit, 200)))
    ).all()
    return {"items": [{"id": str(x.id), "action": x.action, "resource_type": x.resource_type, "resource_id": x.resource_id, "details": x.details, "created_at": x.created_at} for x in rows]}


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_at: datetime | None = None


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
def create_api_key(request: ApiKeyCreateRequest, user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    if user is None:
        raise HTTPException(401, "Authentication required")
    expires_at = request.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(422, "expires_at must be in the future")
    raw = "ak_" + secrets.token_urlsafe(32)
    key = ApiKey(
        user_id=user.id,
        name=request.name.strip(),
        key_prefix=raw[:12],
        key_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        expires_at=expires_at,
    )
    db.add(key)
    db.flush()
    audit(db, user, "api_key.create", "api_key", key.id, {"name": key.name})
    db.commit(); db.refresh(key)
    return {"id": str(key.id), "name": key.name, "key_prefix": key.key_prefix, "api_key": raw, "expires_at": key.expires_at, "created_at": key.created_at}


@router.get("/api-keys")
def list_api_keys(user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    if user is None:
        raise HTTPException(401, "Authentication required")
    rows = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())).all()
    return {"items": [{"id": str(x.id), "name": x.name, "key_prefix": x.key_prefix, "expires_at": x.expires_at, "last_used_at": x.last_used_at, "revoked_at": x.revoked_at, "created_at": x.created_at} for x in rows]}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: UUID, user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    if user is None:
        raise HTTPException(401, "Authentication required")
    key = db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    if not key:
        raise HTTPException(404, "API key not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        audit(db, user, "api_key.revoke", "api_key", key.id, {"name": key.name})
        db.commit()
    return {"ok": True}
