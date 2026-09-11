from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import AnalysisJob, Dataset
from app.services.intelligence import automl, deep_analysis, executive_insights, executive_report, scenario_analysis
from pathlib import Path

from app.services.profiling import read_dataset

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ai-analytics-worker")

SUPPORTED = {"deep_analysis", "insights", "report", "scenario", "automl"}

def claim_job(db):
    job = db.scalar(
        select(AnalysisJob)
        .where(AnalysisJob.status == "queued")
        .order_by(AnalysisJob.created_at)
        .with_for_update(skip_locked=True)
    )
    if not job:
        return None
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job

def execute(job, db):
    dataset = db.get(Dataset, job.dataset_id)
    if not dataset:
        raise ValueError("Dataset not found")
    frame = read_dataset(Path(dataset.storage_path))
    req = job.request or {}
    if job.kind == "deep_analysis":
        return deep_analysis(frame)
    if job.kind == "insights":
        return executive_insights(frame)
    if job.kind == "report":
        return executive_report(frame, dataset.name)
    if job.kind == "scenario":
        return scenario_analysis(frame, req["column"], float(req["change_percent"]), req.get("group_by"))
    if job.kind == "automl":
        return automl(frame, req["target"], req.get("task", "auto"), float(req.get("test_size", 0.2)))
    raise ValueError(f"Unsupported job type: {job.kind}")

def main():
    logger.info("Analysis worker started")
    while True:
        with SessionLocal() as db:
            job = claim_job(db)
            if not job:
                time.sleep(1)
                continue
            try:
                result = execute(job, db)
                job.result = result
                job.status = "completed"
                job.error = None
            except Exception as exc:
                logger.exception("Analysis job %s failed", job.id)
                job.status = "failed"
                job.error = str(exc)[:2000]
            finally:
                job.finished_at = datetime.now(timezone.utc)
                db.commit()

if __name__ == "__main__":
    main()
