from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_background_worker_and_jobs_are_wired():
    compose = (ROOT / "docker-compose.yml").read_text()
    models = (ROOT / "apps/api/app/db/models.py").read_text()
    worker = (ROOT / "apps/api/app/analysis_worker.py").read_text()
    routes = (ROOT / "apps/api/app/api/routes.py").read_text()
    migration = (ROOT / "apps/api/alembic/versions/0011_analysis_jobs.py").read_text()
    assert "worker:" in compose
    assert "AnalysisJob" in models
    assert "skip_locked" in worker
    assert "/analysis-jobs" in routes
    assert "0010_api_keys" in migration

def test_statistics_dependency_and_endpoint():
    req = (ROOT / "apps/api/requirements.txt").read_text()
    routes = (ROOT / "apps/api/app/api/routes.py").read_text()
    assert "scipy>=" in req
    assert "/statistics" in routes
    assert "ttest_ind" in routes
