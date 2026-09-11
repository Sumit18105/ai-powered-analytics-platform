from pathlib import Path
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
import polars as pl
import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db.models import AuditLog, Dashboard, Dataset, DatasetVersion, Transformation, Workspace, WorkspaceMember, User, DataSource, RefreshJob, DataConnection, MonitoringRule, QualitySnapshot, Insight, AutomationRun, AnalysisJob
from app.services.preprocessing import apply_operations, preview_operations, suggest_operations
from app.services.profiling import SUPPORTED, preview_dataset, profile_dataset, read_dataset
from app.services.eda import build_eda, recommend_charts
from app.services.ai import execute_plan, make_plan
from app.services.production import download_csv, export_csv, export_json, export_xlsx, write_report_html, export_pdf, export_pptx, refresh_source
from app.services.composer import chart_data, list_tables, read_table, merge_frames, normalize_db_url, apply_filters, evaluate_metric
from app.services.automation import create_snapshot, run_rule
from app.services.intelligence import executive_insights, executive_report, scenario_analysis, automl, deep_analysis, driver_analysis
from app.services.profiling import list_excel_sheets
from app.api.auth import current_user

router = APIRouter(prefix="/api", tags=["platform"])
settings = get_settings()


class TransformationOperation(BaseModel):
    type: str
    column: str | None = None
    method: str | None = None
    value: object | None = None
    to: str | None = None


class TransformRequest(BaseModel):
    operations: list[TransformationOperation] = Field(min_length=1, max_length=20)
    label: str | None = Field(default=None, max_length=255)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


ROLE_LEVEL = {"viewer": 1, "editor": 2, "admin": 3, "owner": 4}

def require_user(user: User | None) -> User:
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user

def workspace_role(db: Session, workspace_id: UUID, user_id: UUID) -> str | None:
    return db.scalar(select(WorkspaceMember.role).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id))

def ensure_dataset_access(db: Session, dataset: Dataset, user: User | None) -> None:
    if dataset.workspace_id is None:
        if settings.auth_required and user is None: raise HTTPException(401, "Authentication required")
        return
    if user is None or not workspace_role(db, dataset.workspace_id, user.id):
        raise HTTPException(403, "You do not have access to this dataset")


def ensure_workspace_role(db: Session, workspace_id: UUID, user: User | None, minimum: str = "viewer") -> str | None:
    if user is None:
        if settings.auth_required:
            raise HTTPException(401, "Authentication required")
        return None
    role = workspace_role(db, workspace_id, user.id)
    if not role or ROLE_LEVEL[role] < ROLE_LEVEL[minimum]:
        raise HTTPException(403, f"{minimum.title()} role or higher required")
    return role

def ensure_dataset_role(db: Session, dataset: Dataset, user: User | None, minimum: str = "viewer") -> None:
    ensure_dataset_access(db, dataset, user)
    if dataset.workspace_id is not None:
        ensure_workspace_role(db, dataset.workspace_id, user, minimum)

def default_workspace(db: Session, user: User) -> Workspace | None:
    return db.scalar(select(Workspace).join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id).where(WorkspaceMember.user_id == user.id).order_by(Workspace.created_at).limit(1))


@router.get("/workspaces")
def list_workspaces(db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    user = require_user(user)
    rows = db.execute(select(Workspace, WorkspaceMember.role).join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id).where(WorkspaceMember.user_id == user.id).order_by(Workspace.created_at)).all()
    return {"items": [{"id": str(w.id), "name": w.name, "slug": w.slug, "role": role} for w, role in rows]}

class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=255)

class MemberRequest(BaseModel):
    email: str
    role: str = Field(default="viewer", pattern="^(viewer|editor|admin)$")

@router.post("/workspaces", status_code=201)
def create_workspace(request: WorkspaceCreateRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    user = require_user(user)
    base = "-".join(request.name.lower().split())[:80] or "workspace"
    slug, n = base, 2
    while db.scalar(select(Workspace).where(Workspace.slug == slug)):
        slug = f"{base}-{n}"; n += 1
    workspace = Workspace(name=request.name.strip(), slug=slug, created_by=user.id)
    db.add(workspace); db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    db.add(AuditLog(user_id=user.id, action="workspace.create", resource_type="workspace", resource_id=str(workspace.id), details={"name": workspace.name}))
    db.commit(); db.refresh(workspace)
    return {"id": str(workspace.id), "name": workspace.name, "slug": workspace.slug, "role": "owner"}

@router.get("/workspaces/{workspace_id}/members")
def workspace_members(workspace_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    user = require_user(user)
    if not workspace_role(db, workspace_id, user.id): raise HTTPException(403, "You are not a member of this workspace")
    rows = db.execute(select(User.email, User.full_name, WorkspaceMember.role).join(WorkspaceMember, WorkspaceMember.user_id == User.id).where(WorkspaceMember.workspace_id == workspace_id)).all()
    return {"items": [{"email": email, "full_name": name, "role": role} for email, name, role in rows]}

@router.post("/workspaces/{workspace_id}/members")
def add_workspace_member(workspace_id: UUID, request: MemberRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    user = require_user(user)
    role = workspace_role(db, workspace_id, user.id)
    if role not in {"owner", "admin"}: raise HTTPException(403, "Owner or admin role required")
    target = db.scalar(select(User).where(User.email == request.email.strip().lower()))
    if not target: raise HTTPException(404, "User account not found")
    existing = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == target.id))
    if existing and existing.role == "owner":
        raise HTTPException(403, "The workspace owner role cannot be changed")
    if existing: existing.role = request.role
    else: db.add(WorkspaceMember(workspace_id=workspace_id, user_id=target.id, role=request.role))
    db.add(AuditLog(user_id=user.id, action="workspace.member_upsert", resource_type="workspace", resource_id=str(workspace_id), details={"email": target.email, "role": request.role}))
    db.commit()
    return {"ok": True, "email": target.email, "role": request.role}

@router.delete("/workspaces/{workspace_id}/members/{email}")
def remove_workspace_member(workspace_id: UUID, email: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    role = ensure_workspace_role(db, workspace_id, user, "admin")
    target = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not target:
        raise HTTPException(404, "User account not found")
    member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == target.id))
    if not member:
        raise HTTPException(404, "Workspace member not found")
    if member.role == "owner":
        raise HTTPException(403, "The workspace owner cannot be removed")
    if role == "admin" and member.role == "admin":
        raise HTTPException(403, "Only the owner can remove an admin")
    db.delete(member)
    db.add(AuditLog(user_id=user.id if user else None, action="workspace.member_remove", resource_type="workspace", resource_id=str(workspace_id), details={"email": target.email}))
    db.commit()
    return {"ok": True}


def serialize_dataset(dataset: Dataset) -> dict:
    return {
        "id": str(dataset.id),
        "workspace_id": str(dataset.workspace_id) if dataset.workspace_id else None,
        "name": dataset.name,
        "filename": dataset.filename,
        "file_type": dataset.file_type,
        "file_size_bytes": dataset.file_size_bytes,
        "row_count": dataset.row_count,
        "column_count": dataset.column_count,
        "created_at": dataset.created_at,
    }


def serialize_version(version: DatasetVersion) -> dict:
    return {
        "id": str(version.id),
        "dataset_id": str(version.dataset_id),
        "version": version.version,
        "label": version.label,
        "file_type": version.file_type,
        "file_size_bytes": version.file_size_bytes,
        "row_count": version.row_count,
        "column_count": version.column_count,
        "created_at": version.created_at,
        "profile": version.profile,
    }


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
async def upload_dataset(file: UploadFile = File(...), workspace_id: UUID | None = Form(default=None), db: Session = Depends(get_db), user = Depends(current_user)):
    original_name = file.filename or "dataset"
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(400, "Supported files: CSV, Parquet, XLSX, XLS")

    destination = settings.upload_path / f"{uuid4()}{suffix}"
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, f"File is too large. Maximum is {settings.max_upload_mb} MB")
                output.write(chunk)

        profile = profile_dataset(destination)
        workspace = db.get(Workspace, workspace_id) if workspace_id else (default_workspace(db, user) if user else None)
        if workspace_id and not workspace:
            raise HTTPException(404, "Workspace not found")
        if workspace_id and user is None:
            raise HTTPException(401, "Authentication required for workspace uploads")
        if workspace and user:
            ensure_workspace_role(db, workspace.id, user, "editor")
        dataset = Dataset(
            workspace_id=workspace.id if workspace else None,
            name=Path(original_name).stem,
            filename=original_name,
            storage_path=str(destination),
            file_type=suffix.lstrip("."),
            file_size_bytes=size,
            row_count=profile["rows"],
            column_count=profile["columns"],
            profile=profile,
        )
        db.add(dataset)
        db.flush()
        db.add(AuditLog(user_id=user.id if user else None, action="dataset.upload", resource_type="dataset", resource_id=str(dataset.id), details={"filename": original_name}))
        initial_version = DatasetVersion(
            dataset_id=dataset.id,
            version=1,
            label="Original",
            storage_path=str(destination),
            file_type=suffix.lstrip("."),
            file_size_bytes=size,
            row_count=profile["rows"],
            column_count=profile["columns"],
            profile=profile,
        )
        db.add(initial_version)
        db.commit()
        db.refresh(dataset)
        return {**serialize_dataset(dataset), "profile": profile}
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read dataset: {exc}") from exc


@router.get("/datasets")
def list_datasets(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    stmt = select(Dataset).order_by(Dataset.created_at.desc()).limit(limit)
    if user:
        allowed = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
        stmt = select(Dataset).where((Dataset.workspace_id.is_(None)) | Dataset.workspace_id.in_(allowed)).order_by(Dataset.created_at.desc()).limit(limit)
    datasets = db.scalars(stmt).all()
    return {"items": [serialize_dataset(dataset) for dataset in datasets]}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    return {**serialize_dataset(dataset), "profile": dataset.profile}


@router.get("/datasets/{dataset_id}/preview")
def get_dataset_preview(dataset_id: UUID, limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        return preview_dataset(Path(dataset.storage_path), limit)
    except Exception as exc:
        raise HTTPException(422, f"Could not preview dataset: {exc}") from exc


@router.get("/datasets/{dataset_id}/preprocessing/suggestions")
def preprocessing_suggestions(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    return {"items": suggest_operations(dataset.profile or {})}


@router.post("/datasets/{dataset_id}/preprocessing/preview")
def preprocessing_preview(dataset_id: UUID, request: TransformRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        return preview_operations(Path(dataset.storage_path), [op.model_dump(exclude_none=True) for op in request.operations])
    except Exception as exc:
        raise HTTPException(422, f"Could not preview transformations: {exc}") from exc


@router.post("/datasets/{dataset_id}/preprocessing/apply", status_code=status.HTTP_201_CREATED)
def preprocessing_apply(dataset_id: UUID, request: TransformRequest, db: Session = Depends(get_db), user = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_role(db, dataset, user, "editor")

    operations = [op.model_dump(exclude_none=True) for op in request.operations]
    try:
        source = Path(dataset.storage_path)
        frame = apply_operations(read_dataset(source), operations)
        next_version = (db.scalar(select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset_id)) or 0) + 1
        version_dir = settings.processed_path / str(dataset_id)
        version_dir.mkdir(parents=True, exist_ok=True)
        output = version_dir / f"v{next_version}.parquet"
        frame.write_parquet(output)
        profile = profile_dataset(output)
        output_size = output.stat().st_size
        dataset.storage_path = str(output)
        dataset.file_type = "parquet"
        dataset.file_size_bytes = output_size
        dataset.row_count = profile["rows"]
        dataset.column_count = profile["columns"]
        dataset.profile = profile
        version = DatasetVersion(
            dataset_id=dataset_id,
            version=next_version,
            label=request.label or f"Version {next_version}",
            storage_path=str(output),
            file_type="parquet",
            file_size_bytes=output_size,
            row_count=profile["rows"],
            column_count=profile["columns"],
            profile=profile,
        )
        db.add(version)
        db.flush()
        db.add(AuditLog(user_id=user.id if user else None, action="dataset.transform", resource_type="dataset_version", resource_id=str(version.id), details={"operations": operations}))
        for operation in operations:
            db.add(Transformation(dataset_version_id=version.id, operation=operation))
        db.commit()
        db.refresh(version)
        return serialize_version(version)
    except Exception as exc:
        db.rollback()
        raise HTTPException(422, f"Could not apply transformations: {exc}") from exc



class CellEdit(BaseModel):
    row_index: int = Field(ge=0, le=5000)
    column: str = Field(min_length=1, max_length=255)
    value: object | None = None

class ManualEditRequest(BaseModel):
    changes: list[CellEdit] = Field(min_length=1, max_length=500)
    label: str | None = Field(default=None, max_length=255)

@router.post("/datasets/{dataset_id}/manual-edit", status_code=201)
def manual_edit_dataset(dataset_id: UUID, request: ManualEditRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset: raise HTTPException(404, "Dataset not found")
    ensure_dataset_role(db, dataset, user, "editor")
    try:
        frame = read_dataset(Path(dataset.storage_path))
        for change in request.changes:
            if change.row_index >= frame.height: raise ValueError(f"Row {change.row_index} is outside the dataset")
            if change.column not in frame.columns: raise ValueError(f"Unknown column: {change.column}")
            dtype = frame.schema[change.column]
            raw = change.value
            if raw is None:
                typed = None
            else:
                typed = pl.Series("value", [raw]).cast(dtype, strict=False)[0]
            mask = pl.int_range(0, frame.height, eager=True) == change.row_index
            frame = frame.with_columns(pl.when(mask).then(pl.lit(typed, dtype=dtype)).otherwise(pl.col(change.column)).alias(change.column))
        next_version = (db.scalar(select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset_id)) or 0) + 1
        destination = settings.processed_path / str(dataset_id) / f"manual-v{next_version}.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True); frame.write_parquet(destination)
        profile = profile_dataset(destination)
        output_size = destination.stat().st_size
        dataset.storage_path = str(destination)
        dataset.file_type = "parquet"
        dataset.file_size_bytes = output_size
        dataset.row_count = profile["rows"]
        dataset.column_count = profile["columns"]
        dataset.profile = profile
        version = DatasetVersion(dataset_id=dataset_id, version=next_version, label=request.label or f"Manual edit {next_version}", storage_path=str(destination), file_type="parquet", file_size_bytes=output_size, row_count=profile["rows"], column_count=profile["columns"], profile=profile)
        db.add(version); db.add(AuditLog(user_id=user.id if user else None, action="dataset.manual_edit", resource_type="dataset_version", resource_id=str(version.id), details={"changes": [x.model_dump() for x in request.changes]})); db.commit(); db.refresh(version)
        return serialize_version(version)
    except Exception as exc:
        db.rollback(); raise HTTPException(422, f"Could not save manual edits: {exc}") from exc


@router.get("/datasets/{dataset_id}/versions")
def list_versions(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    versions = db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id).order_by(DatasetVersion.version.desc())).all()
    return {"items": [serialize_version(version) for version in versions]}


@router.get("/datasets/{dataset_id}/versions/{version}")
def get_version(dataset_id: UUID, version: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    item = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id, DatasetVersion.version == version))
    if not item:
        raise HTTPException(404, "Dataset version not found")
    return serialize_version(item)


class AskRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=1000)


class ScenarioRequest(BaseModel):
    column: str = Field(min_length=1, max_length=255)
    change_percent: float = Field(ge=-1000, le=1000)
    group_by: str | None = Field(default=None, max_length=255)


class AutoMLRequest(BaseModel):
    target: str = Field(min_length=1, max_length=255)
    task: str = Field(default="auto", pattern="^(auto|regression|classification)$")
    test_size: float = Field(default=0.2, gt=0.1, lt=0.5)


class AnalysisJobRequest(BaseModel):
    kind: str = Field(pattern="^(deep_analysis|insights|report|scenario|automl)$")
    request: dict = Field(default_factory=dict)


class StatisticsRequest(BaseModel):
    metric: str = Field(min_length=1, max_length=255)
    group_by: str | None = Field(default=None, max_length=255)
    group_a: str | None = Field(default=None, max_length=255)
    group_b: str | None = Field(default=None, max_length=255)



@router.post("/datasets/{dataset_id}/ai/ask")
def ask_dataset(dataset_id: UUID, request: AskRequest, db: Session = Depends(get_db), user = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        plan = make_plan(request.prompt, frame)
        result = execute_plan(frame, plan)
        db.add(AuditLog(user_id=user.id if user else None, action="ai.ask", resource_type="dataset", resource_id=str(dataset_id), details={"prompt": request.prompt[:500]}))
        db.commit()
        return {"prompt": request.prompt, "plan": plan.model_dump(), "result": result, "mode": "llm" if os.getenv("OPENAI_API_KEY") else "local"}
    except Exception as exc:
        raise HTTPException(422, f"Could not answer the question: {exc}") from exc


class DashboardRequest(BaseModel):
    name: str = Field(default="My Dashboard", min_length=1, max_length=255)
    config: dict = Field(default_factory=dict)


@router.post("/datasets/{dataset_id}/ai/insights")
def dataset_ai_insights(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        result = executive_insights(frame)
        if user:
            db.add(AuditLog(user_id=user.id, action="ai.insights", resource_type="dataset", resource_id=str(dataset_id), details={"count": len(result["items"])})); db.commit()
        return result
    except Exception as exc:
        raise HTTPException(422, f"Could not generate insights: {exc}") from exc


@router.post("/datasets/{dataset_id}/ai/deep-analysis")
def dataset_deep_analysis(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        result = deep_analysis(frame)
        if user:
            db.add(AuditLog(user_id=user.id, action="ai.deep_analysis", resource_type="dataset", resource_id=str(dataset_id), details={"steps": len(result["steps"])}))
            db.commit()
        return result
    except Exception as exc:
        raise HTTPException(422, f"Could not run deep analysis: {exc}") from exc


@router.get("/datasets/{dataset_id}/ai/report")
def dataset_ai_report(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    frame = read_dataset(Path(dataset.storage_path))
    result = executive_report(frame, dataset.name)
    db.add(AuditLog(user_id=user.id if user else None, action="ai.report", resource_type="dataset", resource_id=str(dataset.id), details={"rows": frame.height, "columns": frame.width}))
    db.commit()
    return result


@router.post("/datasets/{dataset_id}/ai/driver-analysis")
def dataset_driver_analysis(dataset_id: UUID, metric: str | None = None, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        result = driver_analysis(frame, metric)
        if user:
            db.add(AuditLog(user_id=user.id, action="ai.driver_analysis", resource_type="dataset", resource_id=str(dataset_id), details={"metric": result["metric"], "drivers": len(result["drivers"])}))
            db.commit()
        return result
    except Exception as exc:
        raise HTTPException(422, f"Could not run driver analysis: {exc}") from exc


@router.post("/datasets/{dataset_id}/ai/scenario")
def dataset_scenario(dataset_id: UUID, request: ScenarioRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        result = scenario_analysis(frame, request.column, request.change_percent, request.group_by)
        if user:
            db.add(AuditLog(user_id=user.id, action="ai.scenario", resource_type="dataset", resource_id=str(dataset_id), details={"column": request.column, "change_percent": request.change_percent})); db.commit()
        return result
    except Exception as exc:
        raise HTTPException(422, f"Could not run scenario: {exc}") from exc


@router.post("/datasets/{dataset_id}/ai/automl")
def dataset_automl(dataset_id: UUID, request: AutoMLRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        result = automl(frame, request.target, request.task, request.test_size)
        if user:
            db.add(AuditLog(user_id=user.id, action="ai.automl", resource_type="dataset", resource_id=str(dataset_id), details={"target": request.target, "task": request.task})); db.commit()
        return result
    except Exception as exc:
        raise HTTPException(422, f"Could not run AutoML: {exc}") from exc


@router.post("/datasets/{dataset_id}/analysis-jobs", status_code=202)
def create_analysis_job(dataset_id: UUID, request: AnalysisJobRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    allowed = {"deep_analysis", "insights", "report", "scenario", "automl"}
    if request.kind not in allowed:
        raise HTTPException(422, "Unsupported analysis job")
    job = AnalysisJob(user_id=user.id if user else None, workspace_id=dataset.workspace_id, dataset_id=dataset.id,
                      kind=request.kind, status="queued", request=request.request)
    db.add(job)
    if user:
        db.add(AuditLog(user_id=user.id, action="analysis_job.create", resource_type="analysis_job", resource_id=str(job.id), details={"kind": request.kind}))
    db.commit(); db.refresh(job)
    return {"id": str(job.id), "status": job.status, "kind": job.kind, "created_at": job.created_at}


@router.get("/analysis-jobs/{job_id}")
def get_analysis_job(job_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    job = db.get(AnalysisJob, job_id)
    if not job:
        raise HTTPException(404, "Analysis job not found")
    dataset = db.get(Dataset, job.dataset_id)
    if dataset:
        ensure_dataset_access(db, dataset, user)
    return {"id": str(job.id), "status": job.status, "kind": job.kind, "result": job.result,
            "error": job.error, "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at}


@router.get("/workspaces/{workspace_id}/analysis-jobs")
def list_analysis_jobs(workspace_id: UUID, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    jobs = db.scalars(select(AnalysisJob).where(AnalysisJob.workspace_id == workspace_id).order_by(AnalysisJob.created_at.desc()).limit(limit)).all()
    return {"items": [{"id": str(x.id), "dataset_id": str(x.dataset_id), "kind": x.kind, "status": x.status,
                       "error": x.error, "created_at": x.created_at, "started_at": x.started_at, "finished_at": x.finished_at} for x in jobs]}


@router.post("/datasets/{dataset_id}/statistics")
def dataset_statistics(dataset_id: UUID, request: StatisticsRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        from scipy import stats
        frame = read_dataset(Path(dataset.storage_path))
        if request.metric not in frame.columns or not frame.schema[request.metric].is_numeric():
            raise ValueError("Metric must be numeric")
        series = frame.get_column(request.metric).drop_nulls()
        values = [float(x) for x in series.to_list()]
        if len(values) < 2:
            raise ValueError("At least two observations are required")
        result = {"metric": request.metric, "count": len(values), "mean": float(np.mean(values)),
                  "median": float(np.median(values)), "std": float(np.std(values, ddof=1)),
                  "min": float(np.min(values)), "max": float(np.max(values))}
        if request.group_by:
            if request.group_by not in frame.columns:
                raise ValueError("Unknown grouping column")
            grouped = frame.select([request.group_by, request.metric]).drop_nulls()
            groups = grouped.get_column(request.group_by).unique().to_list()
            result["groups"] = [{"group": str(g), "count": grouped.filter(pl.col(request.group_by) == g).height,
                                  "mean": float(grouped.filter(pl.col(request.group_by) == g).get_column(request.metric).mean())} for g in groups[:50]]
            if request.group_a is not None and request.group_b is not None:
                a = grouped.filter(pl.col(request.group_by).cast(pl.String) == str(request.group_a)).get_column(request.metric).to_list()
                b = grouped.filter(pl.col(request.group_by).cast(pl.String) == str(request.group_b)).get_column(request.metric).to_list()
                if len(a) >= 2 and len(b) >= 2:
                    test = stats.ttest_ind(a, b, equal_var=False, nan_policy="omit")
                    result["comparison"] = {"group_a": request.group_a, "group_b": request.group_b, "t_statistic": float(test.statistic), "p_value": float(test.pvalue),
                                            "significant_at_0_05": bool(test.pvalue < 0.05),
                                            "note": "Statistical significance does not establish causation."}
        if user:
            db.add(AuditLog(user_id=user.id, action="ai.statistics", resource_type="dataset", resource_id=str(dataset.id), details={"metric": request.metric}))
            db.commit()
        return result
    except Exception as exc:
        raise HTTPException(422, f"Could not run statistical analysis: {exc}") from exc


@router.get("/datasets/{dataset_id}/eda")
def get_eda(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        return build_eda(Path(dataset.storage_path))
    except Exception as exc:
        raise HTTPException(422, f"Could not analyze dataset: {exc}") from exc


@router.get("/datasets/{dataset_id}/chart-recommendations")
def get_chart_recommendations(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        return {"items": recommend_charts(Path(dataset.storage_path))}
    except Exception as exc:
        raise HTTPException(422, f"Could not recommend charts: {exc}") from exc


@router.get("/datasets/{dataset_id}/charts/bar")
def get_bar_chart(dataset_id: UUID, x: str, y: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        df = read_dataset(Path(dataset.storage_path))
        if x not in df.columns or y not in df.columns:
            raise ValueError("Unknown chart column")
        result = (df.select([x, y]).drop_nulls().group_by(x).agg(pl.col(y).sum().alias("value")).sort("value", descending=True).head(30))
        return {"type": "bar", "x": x, "y": y, "items": [{"category": str(r[0]), "value": _json_safe(r[1])} for r in result.rows()]}
    except Exception as exc:
        raise HTTPException(422, f"Could not build chart: {exc}") from exc


def _json_safe(value):
    if hasattr(value, "item"):
        try: value = value.item()
        except (ValueError, TypeError): pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


@router.get("/dashboards")
def list_dashboards(dataset_id: UUID | None = None, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if dataset_id:
        dataset = db.get(Dataset, dataset_id)
        if not dataset:
            raise HTTPException(404, "Dataset not found")
        ensure_dataset_access(db, dataset, user)
    stmt = select(Dashboard).order_by(Dashboard.updated_at.desc())
    if dataset_id:
        stmt = stmt.where(Dashboard.dataset_id == dataset_id)
    items = db.scalars(stmt.limit(100)).all()
    return {"items": [{"id": str(d.id), "dataset_id": str(d.dataset_id), "name": d.name, "config": d.config, "created_at": d.created_at, "updated_at": d.updated_at} for d in items]}


@router.post("/datasets/{dataset_id}/dashboards", status_code=status.HTTP_201_CREATED)
def create_dashboard(dataset_id: UUID, request: DashboardRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_role(db, dataset, user, "editor")
    dashboard = Dashboard(dataset_id=dataset_id, name=request.name, config=request.config)
    db.add(dashboard)
    db.commit()
    db.refresh(dashboard)
    return {"id": str(dashboard.id), "dataset_id": str(dashboard.dataset_id), "name": dashboard.name, "config": dashboard.config, "created_at": dashboard.created_at, "updated_at": dashboard.updated_at}


@router.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dashboard = db.get(Dashboard, dashboard_id)
    if not dashboard:
        raise HTTPException(404, "Dashboard not found")
    ensure_dataset_access(db, db.get(Dataset, dashboard.dataset_id), user)
    return {"id": str(dashboard.id), "dataset_id": str(dashboard.dataset_id), "name": dashboard.name, "config": dashboard.config, "created_at": dashboard.created_at, "updated_at": dashboard.updated_at}


@router.put("/dashboards/{dashboard_id}")
def update_dashboard(dashboard_id: UUID, request: DashboardRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dashboard = db.get(Dashboard, dashboard_id)
    if not dashboard:
        raise HTTPException(404, "Dashboard not found")
    dataset = db.get(Dataset, dashboard.dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_role(db, dataset, user, "editor")
    dashboard.name = request.name
    dashboard.config = request.config
    dashboard.version += 1
    db.commit()
    db.refresh(dashboard)
    return {"id": str(dashboard.id), "dataset_id": str(dashboard.dataset_id), "name": dashboard.name, "config": dashboard.config, "created_at": dashboard.created_at, "updated_at": dashboard.updated_at}





@router.post("/datasets/batch", status_code=status.HTTP_201_CREATED)
async def upload_datasets_batch(files: list[UploadFile] = File(...), workspace_id: UUID | None = Form(default=None), db: Session = Depends(get_db), user=Depends(current_user)):
    """Upload several supported files atomically; no orphaned files on failure."""
    if not files or len(files) > 50:
        raise HTTPException(400, "Upload between 1 and 50 files at once")
    workspace = db.get(Workspace, workspace_id) if workspace_id else (default_workspace(db, user) if user else None)
    if workspace_id and not workspace:
        raise HTTPException(404, "Workspace not found")
    if workspace_id and user is None:
        raise HTTPException(401, "Authentication required for workspace uploads")
    if workspace and user:
        ensure_workspace_role(db, workspace.id, user, "editor")
    created, destinations = [], []
    try:
        for file in files:
            original_name = file.filename or "dataset"
            suffix = Path(original_name).suffix.lower()
            if suffix not in SUPPORTED:
                raise HTTPException(400, f"Unsupported file: {original_name}. Supported files: CSV, Parquet, XLSX, XLS")
            destination = settings.upload_path / f"{uuid4()}{suffix}"
            destinations.append(destination)
            size = 0
            with destination.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_mb * 1024 * 1024:
                        raise HTTPException(413, f"{original_name} exceeds the {settings.max_upload_mb} MB limit")
                    output.write(chunk)
            profile = profile_dataset(destination)
            if not profile["rows"] or not profile["columns"]:
                raise ValueError(f"{original_name} does not contain tabular data")
            dataset = Dataset(
                workspace_id=workspace.id if workspace else None, name=Path(original_name).stem,
                filename=original_name, storage_path=str(destination), file_type=suffix.lstrip("."),
                file_size_bytes=size, row_count=profile["rows"], column_count=profile["columns"], profile=profile,
            )
            db.add(dataset)
            db.flush()
            db.add(DatasetVersion(dataset_id=dataset.id, version=1, label="Original", storage_path=str(destination),
                                  file_type=suffix.lstrip("."), file_size_bytes=size, row_count=profile["rows"],
                                  column_count=profile["columns"], profile=profile))
            db.add(AuditLog(user_id=user.id if user else None, action="dataset.upload", resource_type="dataset",
                            resource_id=str(dataset.id), details={"filename": original_name, "batch": True}))
            created.append(dataset)
        db.commit()
        return {"items": [{**serialize_dataset(d), "profile": d.profile} for d in created]}
    except HTTPException:
        db.rollback()
        for destination in destinations:
            destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        db.rollback()
        for destination in destinations:
            destination.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not upload batch: {exc}") from exc

# ---------- Visual composer, workbook sheets, dataset merging, database connections ----------
class ChartBuildRequest(BaseModel):
    chart_type: str = Field(pattern="^(bar|line|area|pie|donut|scatter|histogram|boxplot|pareto|heatmap|funnel|treemap|waterfall)$")
    x: str = Field(min_length=1, max_length=255)
    y: str | None = Field(default=None, max_length=255)
    aggregation: str = Field(default="sum", pattern="^(sum|mean|count|min|max)$")
    title: str | None = Field(default=None, max_length=255)
    limit: int = Field(default=30, ge=1, le=500)
    filters: dict[str, object] = Field(default_factory=dict, max_length=30)

class MergeRequest(BaseModel):
    dataset_ids: list[UUID] = Field(min_length=2, max_length=20)
    mode: str = Field(default="append", pattern="^(append|join)$")
    left_on: str | None = None
    right_on: str | None = None
    name: str = Field(default="Merged Dataset", min_length=1, max_length=255)

class SheetImportRequest(BaseModel):
    sheets: list[str] = Field(min_length=1, max_length=50)
    name: str = Field(default="Merged Workbook", min_length=1, max_length=255)

class DatabaseConnectionRequest(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    kind: str = Field(pattern="^(postgresql|mysql|sqlite)$")
    connection_url: str = Field(min_length=3, max_length=2000)

class DatabaseImportRequest(BaseModel):
    table: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)


def _create_dataset_from_frame(db: Session, frame: pl.DataFrame, name: str, user: User | None, workspace: Workspace | None, source_label: str) -> Dataset:
    if frame.height == 0 or frame.width == 0:
        raise ValueError("The resulting dataset is empty")
    dataset_id = uuid4()
    destination = settings.processed_path / "composed" / str(dataset_id) / "data.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(destination)
    profile = profile_dataset(destination)
    dataset = Dataset(workspace_id=workspace.id if workspace else None, name=name, filename=f"{name}.parquet", storage_path=str(destination), file_type="parquet", file_size_bytes=destination.stat().st_size, row_count=profile["rows"], column_count=profile["columns"], profile=profile)
    db.add(dataset); db.flush()
    db.add(DatasetVersion(dataset_id=dataset.id, version=1, label="Composed", storage_path=str(destination), file_type="parquet", file_size_bytes=destination.stat().st_size, row_count=profile["rows"], column_count=profile["columns"], profile=profile))
    db.add(AuditLog(user_id=user.id if user else None, action="dataset.compose", resource_type="dataset", resource_id=str(dataset.id), details={"source": source_label}))
    return dataset

@router.get("/datasets/{dataset_id}/sheets")
def dataset_sheets(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset: raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try: return {"items": list_excel_sheets(Path(dataset.storage_path))}
    except Exception as exc: raise HTTPException(422, f"Could not read workbook sheets: {exc}") from exc

@router.post("/datasets/{dataset_id}/sheets/import", status_code=201)
def import_workbook_sheets(dataset_id: UUID, request: SheetImportRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset: raise HTTPException(404, "Dataset not found")
    ensure_dataset_role(db, dataset, user, "editor")
    if Path(dataset.storage_path).suffix.lower() not in {".xlsx", ".xls"}: raise HTTPException(400, "Dataset is not an Excel workbook")
    try:
        frames = pl.read_excel(Path(dataset.storage_path), sheet_name=request.sheets, engine="calamine")
        if not isinstance(frames, dict): frames = {request.sheets[0]: frames}
        missing = [x for x in request.sheets if x not in frames]
        if missing: raise ValueError(f"Sheets not found: {', '.join(missing)}")
        result = merge_frames([frames[x] for x in request.sheets], "append")
        created = _create_dataset_from_frame(db, result, request.name, user, db.get(Workspace, dataset.workspace_id) if dataset.workspace_id else None, f"sheets:{','.join(request.sheets)}")
        db.commit(); db.refresh(created)
        return {**serialize_dataset(created), "profile": created.profile}
    except Exception as exc: db.rollback(); raise HTTPException(422, f"Could not merge workbook sheets: {exc}") from exc

@router.post("/datasets/merge", status_code=201)
def merge_datasets(request: MergeRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    datasets = [db.get(Dataset, x) for x in request.dataset_ids]
    if any(x is None for x in datasets): raise HTTPException(404, "One or more datasets were not found")
    for dataset in datasets: ensure_dataset_role(db, dataset, user, "editor")
    workspace_ids = {dataset.workspace_id for dataset in datasets}
    if len(workspace_ids) > 1:
        raise HTTPException(400, "Datasets must belong to the same workspace to be merged")
    try:
        result = merge_frames([read_dataset(Path(x.storage_path)) for x in datasets], request.mode, request.left_on, request.right_on)
        workspace = db.get(Workspace, datasets[0].workspace_id) if datasets[0].workspace_id else None
        created = _create_dataset_from_frame(db, result, request.name, user, workspace, f"datasets:{','.join(str(x.id) for x in datasets)}")
        db.commit(); db.refresh(created)
        return {**serialize_dataset(created), "profile": created.profile}
    except Exception as exc: db.rollback(); raise HTTPException(422, f"Could not merge datasets: {exc}") from exc

class MetricEvaluateRequest(BaseModel):
    expression: str = Field(min_length=1, max_length=1000)
    filters: dict[str, object] = Field(default_factory=dict, max_length=30)


class TableDataRequest(BaseModel):
    columns: list[str] = Field(min_length=1, max_length=30)
    limit: int = Field(default=50, ge=1, le=500)
    filters: dict[str, object] = Field(default_factory=dict, max_length=30)


@router.post("/datasets/{dataset_id}/table-data")
def dashboard_table_data(dataset_id: UUID, request: TableDataRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = apply_filters(read_dataset(Path(dataset.storage_path)), request.filters)
        missing = [c for c in request.columns if c not in frame.columns]
        if missing:
            raise ValueError(f"Unknown columns: {', '.join(missing)}")
        frame = frame.select(request.columns).head(request.limit)
        return {"columns": frame.columns, "rows": [{c: _json_safe(v) for c, v in zip(frame.columns, row)} for row in frame.rows()]}
    except Exception as exc:
        raise HTTPException(422, f"Could not build table: {exc}") from exc


@router.get("/datasets/{dataset_id}/filter-options")
def filter_options(dataset_id: UUID, column: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        if column not in frame.columns:
            raise ValueError("Unknown filter column")
        values = frame.get_column(column).drop_nulls().unique().head(200).sort().to_list()
        return {"column": column, "items": [_json_safe(v) for v in values]}
    except Exception as exc:
        raise HTTPException(422, f"Could not load filter values: {exc}") from exc


@router.post("/datasets/{dataset_id}/metrics/evaluate")
def evaluate_dashboard_metric(dataset_id: UUID, request: MetricEvaluateRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = apply_filters(read_dataset(Path(dataset.storage_path)), request.filters)
        value = evaluate_metric(frame, request.expression)
        return {"expression": request.expression, "value": value, "row_count": frame.height}
    except Exception as exc:
        raise HTTPException(422, f"Could not evaluate metric: {exc}") from exc


@router.post("/datasets/{dataset_id}/charts/build")
def build_manual_chart(dataset_id: UUID, request: ChartBuildRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset: raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    try:
        frame = read_dataset(Path(dataset.storage_path))
        spec = chart_data(frame, request.chart_type, request.x, request.y, request.aggregation, request.limit, request.filters)
        spec["meta"] = {"title": request.title or f"{request.y or request.x} by {request.x}", "description": f"{request.aggregation} aggregation"}; spec["aggregation"] = request.aggregation
        return spec
    except Exception as exc: raise HTTPException(422, f"Could not build chart: {exc}") from exc

@router.get("/workspaces/{workspace_id}/connections")
def list_database_connections(workspace_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    rows = db.scalars(select(DataConnection).where(DataConnection.workspace_id == workspace_id).order_by(DataConnection.created_at.desc())).all()
    return {"items": [{"id": str(x.id), "name": x.name, "kind": x.kind, "enabled": x.enabled, "created_at": x.created_at} for x in rows]}

@router.post("/workspaces/{workspace_id}/connections", status_code=201)
def create_database_connection(workspace_id: UUID, request: DatabaseConnectionRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "admin")
    try:
        url = normalize_db_url(request.kind, request.connection_url)
        # Test connection immediately and only then persist it.
        list_tables(request.kind, url)
    except Exception as exc: raise HTTPException(422, f"Could not connect to database: {exc}") from exc
    conn = DataConnection(workspace_id=workspace_id, name=request.name, kind=request.kind, connection_url=url, created_by=user.id)
    db.add(conn); db.flush(); db.add(AuditLog(user_id=user.id, action="connection.create", resource_type="data_connection", resource_id=str(conn.id), details={"kind": request.kind, "name": request.name})); db.commit(); db.refresh(conn)
    return {"id": str(conn.id), "name": conn.name, "kind": conn.kind, "enabled": conn.enabled, "created_at": conn.created_at}

@router.get("/connections/{connection_id}/tables")
def connection_tables(connection_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    conn = db.get(DataConnection, connection_id)
    if not conn: raise HTTPException(404, "Database connection not found")
    if not conn.enabled: raise HTTPException(409, "Database connection is disabled")
    ensure_workspace_role(db, conn.workspace_id, user, "viewer")
    try: return {"items": list_tables(conn.kind, conn.connection_url)}
    except Exception as exc: raise HTTPException(422, f"Could not inspect database: {exc}") from exc

@router.post("/connections/{connection_id}/import", status_code=201)
def import_database_table(connection_id: UUID, request: DatabaseImportRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    conn = db.get(DataConnection, connection_id)
    if not conn: raise HTTPException(404, "Database connection not found")
    if not conn.enabled: raise HTTPException(409, "Database connection is disabled")
    ensure_workspace_role(db, conn.workspace_id, user, "editor")
    try:
        frame = read_table(conn.kind, conn.connection_url, request.table)
        workspace = db.get(Workspace, conn.workspace_id)
        created = _create_dataset_from_frame(db, frame, request.name, user, workspace, f"database:{conn.name}:{request.table}")
        db.commit(); db.refresh(created)
        return {**serialize_dataset(created), "profile": created.profile}
    except Exception as exc: db.rollback(); raise HTTPException(422, f"Could not import database table: {exc}") from exc


# ---------- Production platform: connectors, refresh, exports, lineage ----------
class DataSourceRequest(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    url: str = Field(min_length=8, max_length=2000)

class RefreshJobRequest(BaseModel):
    source_id: UUID
    interval_minutes: int = Field(default=1440, ge=60, le=43200)
    enabled: bool = True


def serialize_source(x: DataSource) -> dict:
    return {"id": str(x.id), "workspace_id": str(x.workspace_id), "name": x.name, "kind": x.kind,
            "url": x.url, "dataset_id": str(x.dataset_id) if x.dataset_id else None,
            "enabled": x.enabled, "created_at": x.created_at, "updated_at": x.updated_at}


def serialize_job(x: RefreshJob) -> dict:
    return {"id": str(x.id), "workspace_id": str(x.workspace_id), "source_id": str(x.source_id),
            "interval_minutes": x.interval_minutes, "enabled": x.enabled, "last_run_at": x.last_run_at,
            "last_status": x.last_status, "last_error": x.last_error, "next_run_at": x.next_run_at,
            "created_at": x.created_at}



@router.get("/workspaces/{workspace_id}/sources")
def list_sources(workspace_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    return {"items": [serialize_source(x) for x in db.scalars(select(DataSource).where(DataSource.workspace_id == workspace_id).order_by(DataSource.created_at.desc())).all()]}


@router.post("/workspaces/{workspace_id}/sources", status_code=201)
def create_source(workspace_id: UUID, request: DataSourceRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "editor")
    source = DataSource(workspace_id=workspace_id, name=request.name.strip(), url=request.url.strip(), created_by=user.id)
    db.add(source); db.flush()
    db.add(AuditLog(user_id=user.id, action="connector.create", resource_type="data_source", resource_id=str(source.id), details={"url": source.url}))
    db.commit(); db.refresh(source)
    return serialize_source(source)


@router.post("/sources/{source_id}/refresh")
def run_source_refresh(source_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    source = db.get(DataSource, source_id)
    if not source:
        raise HTTPException(404, "Data source not found")
    if not source.enabled:
        raise HTTPException(409, "Data source is disabled")
    ensure_workspace_role(db, source.workspace_id, user, "editor")
    try:
        dataset = refresh_source(db, source, user)
        db.commit(); db.refresh(dataset)
        return {"ok": True, "dataset": serialize_dataset(dataset)}
    except Exception as exc:
        db.rollback()
        raise HTTPException(422, f"Refresh failed: {exc}") from exc


@router.get("/workspaces/{workspace_id}/refresh-jobs")
def list_refresh_jobs(workspace_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    return {"items": [serialize_job(x) for x in db.scalars(select(RefreshJob).where(RefreshJob.workspace_id == workspace_id).order_by(RefreshJob.created_at.desc())).all()]}


@router.post("/workspaces/{workspace_id}/refresh-jobs", status_code=201)
def create_refresh_job(workspace_id: UUID, request: RefreshJobRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "editor")
    source = db.get(DataSource, request.source_id)
    if not source or source.workspace_id != workspace_id:
        raise HTTPException(404, "Data source not found")
    if not source.enabled:
        raise HTTPException(409, "Data source is disabled")
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    job = RefreshJob(workspace_id=workspace_id, source_id=source.id, interval_minutes=request.interval_minutes,
                     enabled=request.enabled, next_run_at=now + timedelta(minutes=request.interval_minutes), created_by=user.id)
    db.add(job); db.add(AuditLog(user_id=user.id, action="refresh_job.create", resource_type="refresh_job", resource_id=str(job.id), details={"interval_minutes": request.interval_minutes}))
    db.commit(); db.refresh(job)
    return serialize_job(job)


@router.get("/workspaces/{workspace_id}/lineage")
def workspace_lineage(workspace_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    datasets = db.scalars(select(Dataset).where(Dataset.workspace_id == workspace_id).order_by(Dataset.created_at)).all()
    nodes = [{"id": str(d.id), "type": "dataset", "label": d.name} for d in datasets]
    edges = []
    for d in datasets:
        versions = db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == d.id).order_by(DatasetVersion.version)).all()
        prev = None
        for v in versions:
            vid = str(v.id); nodes.append({"id": vid, "type": "version", "label": f"{d.name} · v{v.version}"})
            edges.append({"source": str(d.id), "target": vid, "type": "contains"})
            if prev: edges.append({"source": str(prev.id), "target": vid, "type": "derived"})
            prev = v
    return {"nodes": nodes, "edges": edges}


@router.get("/workspaces/{workspace_id}/audit")
def workspace_audit(workspace_id: UUID, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    role = ensure_workspace_role(db, workspace_id, user, "admin")
    user_ids = select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_id)
    rows = db.scalars(select(AuditLog).where(AuditLog.user_id.in_(user_ids)).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return {"role": role, "items": [{"id": str(x.id), "action": x.action, "resource_type": x.resource_type, "resource_id": x.resource_id, "details": x.details, "created_at": x.created_at} for x in rows]}


def _download_filename(name: str, extension: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in name).strip(" .") or "dataset"
    return f"{safe}.{extension}"


@router.get("/datasets/{dataset_id}/export")
def export_dataset(dataset_id: UUID, format: str = Query("csv", pattern="^(csv|json|xlsx|html|pdf|pptx)$"), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset: raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    frame = read_dataset(Path(dataset.storage_path))
    if format == "csv": body, media, ext = export_csv(frame), "text/csv; charset=utf-8", "csv"
    elif format == "json": body, media, ext = export_json(frame), "application/json", "json"
    elif format == "xlsx": body, media, ext = export_xlsx(frame), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    elif format == "pdf": body, media, ext = export_pdf(dataset.name, dataset.profile or {}), "application/pdf", "pdf"
    elif format == "pptx": body, media, ext = export_pptx(dataset.name, dataset.profile or {}), "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"
    else: body, media, ext = write_report_html(dataset.name, frame, dataset.profile or {}), "text/html; charset=utf-8", "html"
    db.add(AuditLog(user_id=user.id if user else None, action="dataset.export", resource_type="dataset", resource_id=str(dataset.id), details={"format": format})); db.commit()
    return Response(content=body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{_download_filename(dataset.name, ext)}"'})


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    return {"status": "ok", "datasets": db.scalar(select(func.count()).select_from(Dataset)) or 0,
            "users": db.scalar(select(func.count()).select_from(User)) or 0,
            "workspaces": db.scalar(select(func.count()).select_from(Workspace)) or 0,
            "dashboards": db.scalar(select(func.count()).select_from(Dashboard)) or 0}


class MonitoringRuleRequest(BaseModel):
    dataset_id: UUID
    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(pattern="^(quality|freshness|row_change|schema)$")
    config: dict = Field(default_factory=dict)
    enabled: bool = True


def serialize_rule(rule: MonitoringRule) -> dict:
    return {"id": str(rule.id), "workspace_id": str(rule.workspace_id), "dataset_id": str(rule.dataset_id),
            "name": rule.name, "kind": rule.kind, "config": rule.config, "enabled": rule.enabled,
            "last_status": rule.last_status, "last_message": rule.last_message, "last_run_at": rule.last_run_at}


@router.get("/datasets/{dataset_id}/quality")
def dataset_quality(dataset_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset: raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    snapshot = db.scalar(select(QualitySnapshot).where(QualitySnapshot.dataset_id == dataset.id).order_by(QualitySnapshot.created_at.desc()).limit(1))
    if snapshot is None:
        snapshot = create_snapshot(db, dataset); db.commit(); db.refresh(snapshot)
    return {"dataset_id": str(dataset.id), "quality_score": snapshot.quality_score, "row_count": snapshot.row_count,
            "missing_percent": snapshot.missing_percent, "duplicate_rows": snapshot.duplicate_rows,
            "schema": snapshot.schema, "checks": snapshot.checks, "created_at": snapshot.created_at}


@router.get("/datasets/{dataset_id}/quality/history")
def quality_history(dataset_id: UUID, limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset: raise HTTPException(404, "Dataset not found")
    ensure_dataset_access(db, dataset, user)
    rows = db.scalars(select(QualitySnapshot).where(QualitySnapshot.dataset_id == dataset.id).order_by(QualitySnapshot.created_at.desc()).limit(limit)).all()
    return {"items": [{"version": x.version, "quality_score": x.quality_score, "row_count": x.row_count, "missing_percent": x.missing_percent, "duplicate_rows": x.duplicate_rows, "checks": x.checks, "created_at": x.created_at} for x in rows]}


@router.get("/workspaces/{workspace_id}/monitoring-rules")
def list_monitoring_rules(workspace_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    rows = db.scalars(select(MonitoringRule).where(MonitoringRule.workspace_id == workspace_id).order_by(MonitoringRule.created_at.desc())).all()
    return {"items": [serialize_rule(x) for x in rows]}


@router.post("/workspaces/{workspace_id}/monitoring-rules", status_code=201)
def create_monitoring_rule(workspace_id: UUID, request: MonitoringRuleRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "editor")
    dataset = db.get(Dataset, request.dataset_id)
    if not dataset or dataset.workspace_id != workspace_id: raise HTTPException(400, "Dataset must belong to the workspace")
    actor = require_user(user) if settings.auth_required else user
    if actor is None: raise HTTPException(401, "Authentication required")
    rule = MonitoringRule(workspace_id=workspace_id, dataset_id=dataset.id, name=request.name, kind=request.kind, config=request.config, enabled=request.enabled, created_by=actor.id)
    db.add(rule); db.flush()
    db.add(AuditLog(user_id=actor.id, action="monitoring.rule.create", resource_type="monitoring_rule", resource_id=str(rule.id), details={"kind": rule.kind, "dataset_id": str(dataset.id)}))
    db.commit(); db.refresh(rule)
    return serialize_rule(rule)


@router.post("/monitoring-rules/{rule_id}/run")
def execute_monitoring_rule(rule_id: UUID, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    rule = db.get(MonitoringRule, rule_id)
    if not rule: raise HTTPException(404, "Monitoring rule not found")
    ensure_workspace_role(db, rule.workspace_id, user, "editor")
    run = run_rule(db, rule)
    db.commit(); db.refresh(run)
    return {"id": str(run.id), "status": run.status, "summary": run.summary, "details": run.details, "started_at": run.started_at, "finished_at": run.finished_at}


@router.get("/workspaces/{workspace_id}/insights")
def workspace_insights(workspace_id: UUID, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    rows = db.scalars(select(Insight).where(Insight.workspace_id == workspace_id).order_by(Insight.created_at.desc()).limit(limit)).all()
    return {"items": [{"id": str(x.id), "dataset_id": str(x.dataset_id), "kind": x.kind, "severity": x.severity, "title": x.title, "message": x.message, "details": x.details, "created_at": x.created_at} for x in rows]}


@router.get("/workspaces/{workspace_id}/automation-runs")
def automation_runs(workspace_id: UUID, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ensure_workspace_role(db, workspace_id, user, "viewer")
    rows = db.scalars(select(AutomationRun, AnalysisJob).where(AutomationRun, AnalysisJob.workspace_id == workspace_id).order_by(AutomationRun, AnalysisJob.finished_at.desc()).limit(limit)).all()
    return {"items": [{"id": str(x.id), "rule_id": str(x.rule_id) if x.rule_id else None, "status": x.status, "summary": x.summary, "details": x.details, "started_at": x.started_at, "finished_at": x.finished_at} for x in rows]}
