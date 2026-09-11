from pathlib import Path


def test_production_configuration_is_present():
    root = Path(__file__).resolve().parents[1]
    config = (root / "apps/api/app/core/config.py").read_text()
    compose = (root / "docker-compose.yml").read_text()
    dockerfile = (root / "apps/api/Dockerfile").read_text()
    assert "AUTH_REQUIRED must be true in production" in config
    assert "CORS_ORIGINS cannot be '*' in production" in config
    assert "/health/ready" in compose
    assert "HEALTHCHECK" in dockerfile
    assert "--reload" in dockerfile
    lock = (root / "apps/api/app/services/scheduler_lock.py").read_text()
    assert "pg_try_advisory_lock" in lock
    assert "pg_advisory_unlock" in lock
    assert "scheduler:" in compose
    assert "SCHEDULER_ENABLED: \"false\"" in compose
    assert "scheduler_worker" in (root / "apps/api/app/scheduler_worker.py").read_text()


def test_environment_example_has_production_controls():
    root = Path(__file__).resolve().parents[1]
    env = (root / ".env.example").read_text()
    for key in ["APP_ENV", "AUTH_REQUIRED", "JWT_SECRET", "CORS_ORIGINS", "SCHEDULER_ENABLED", "WEB_CONCURRENCY"]:
        assert f"{key}=" in env
