from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import polars as pl
from sqlalchemy import select

from app.services.scheduler_lock import MONITORING_JOB_LOCK, release_job_lock, try_job_lock
from sqlalchemy.orm import Session

from app.db.models import AutomationRun, Dataset, DatasetVersion, Insight, MonitoringRule, QualitySnapshot
from app.services.profiling import read_dataset


def quality_report(frame: pl.DataFrame, previous: QualitySnapshot | None = None) -> dict:
    profile = profile_dataset_from_frame(frame)
    checks = {
        "schema_valid": bool(frame.width),
        "empty_dataset": frame.height == 0,
        "missing_percent": profile["missing_percent"],
        "duplicate_rows": profile["duplicate_rows"],
    }
    if previous:
        checks["row_change_percent"] = round((frame.height - previous.row_count) / max(previous.row_count, 1) * 100, 2)
        checks["quality_change"] = round(profile["quality_score"] - previous.quality_score, 2)
    return {"profile": profile, "checks": checks}


def profile_dataset_from_frame(frame: pl.DataFrame) -> dict:
    # Reuse the canonical profiler without duplicating its rules.
    from app.services.profiling import profile_frame
    return profile_frame(frame)


def create_snapshot(db: Session, dataset: Dataset) -> QualitySnapshot:
    frame = read_dataset(Path(dataset.storage_path))
    version = db.scalar(select(DatasetVersion.version).where(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version.desc()).limit(1)) or 1
    previous = db.scalar(select(QualitySnapshot).where(QualitySnapshot.dataset_id == dataset.id).order_by(QualitySnapshot.created_at.desc()).limit(1))
    report = quality_report(frame, previous)
    snapshot = QualitySnapshot(
        dataset_id=dataset.id, version=version, quality_score=report["profile"]["quality_score"],
        row_count=frame.height, missing_percent=report["profile"]["missing_percent"],
        duplicate_rows=report["profile"]["duplicate_rows"],
        schema={c: str(t) for c, t in zip(frame.columns, frame.dtypes)}, checks=report["checks"],
    )
    db.add(snapshot)
    return snapshot


def evaluate_rule(db: Session, rule: MonitoringRule) -> tuple[str, str, dict]:
    dataset = db.get(Dataset, rule.dataset_id)
    if not dataset:
        return "error", "Dataset no longer exists", {}
    latest = db.scalar(select(QualitySnapshot).where(QualitySnapshot.dataset_id == dataset.id).order_by(QualitySnapshot.created_at.desc()).limit(1))
    snapshot = create_snapshot(db, dataset)
    config = rule.config or {}
    kind = rule.kind
    details = {"quality_score": snapshot.quality_score, "row_count": snapshot.row_count, "missing_percent": snapshot.missing_percent}
    if latest:
        details["quality_change"] = round(snapshot.quality_score - latest.quality_score, 2)
        details["row_change_percent"] = round((snapshot.row_count - latest.row_count) / max(latest.row_count, 1) * 100, 2)

    if kind == "quality":
        threshold = float(config.get("min_score", 80))
        if snapshot.quality_score < threshold:
            return "alert", f"Data quality is {snapshot.quality_score}% (below {threshold}%).", details
        return "ok", f"Data quality is healthy at {snapshot.quality_score}%.", details
    if kind == "freshness":
        max_age = float(config.get("max_age_hours", 24))
        age_hours = (datetime.now(timezone.utc) - dataset.updated_at).total_seconds() / 3600 if dataset.updated_at else 0
        details["age_hours"] = round(age_hours, 2)
        if age_hours > max_age:
            return "alert", f"Dataset is {age_hours:.1f} hours old (limit {max_age:g}h).", details
        return "ok", f"Dataset freshness is healthy at {age_hours:.1f} hours.", details
    if kind == "row_change":
        maximum = abs(float(config.get("max_percent", 30)))
        change = abs(float(details.get("row_change_percent", 0)))
        if latest and change > maximum:
            return "alert", f"Row count changed {details['row_change_percent']:.1f}% (limit ±{maximum:g}%).", details
        return "ok", f"Row count change is {details.get('row_change_percent', 0):.1f}%.", details
    if kind == "schema":
        if latest and snapshot.schema != latest.schema:
            return "alert", "Dataset schema changed since the previous snapshot.", {**details, "previous_schema": latest.schema, "schema": snapshot.schema}
        return "ok", "Dataset schema is unchanged.", details
    return "error", f"Unsupported monitoring rule: {kind}", details


def run_rule(db: Session, rule: MonitoringRule) -> AutomationRun:
    started = datetime.now(timezone.utc)
    try:
        status, message, details = evaluate_rule(db, rule)
    except Exception as exc:
        status, message, details = "error", str(exc)[:1000], {}
    finished = datetime.now(timezone.utc)
    previous_status = rule.last_status
    rule.last_status, rule.last_message, rule.last_run_at = status, message, finished
    if status == "alert" and previous_status != "alert":
        db.add(Insight(workspace_id=rule.workspace_id, dataset_id=rule.dataset_id, kind=rule.kind,
                       severity="warning", title=rule.name, message=message, details=details))
    run = AutomationRun(workspace_id=rule.workspace_id, rule_id=rule.id, status=status, summary=message,
                        details=details, started_at=started, finished_at=finished)
    db.add(run)
    return run


def run_monitoring_rules() -> None:
    from app.db.database import SessionLocal
    db = SessionLocal()
    locked = False
    try:
        locked = try_job_lock(db, MONITORING_JOB_LOCK)
        if not locked:
            return
        rules = db.scalars(select(MonitoringRule).where(MonitoringRule.enabled.is_(True)).limit(100)).all()
        for rule in rules:
            run_rule(db, rule)
        db.commit()
    finally:
        db.close()
