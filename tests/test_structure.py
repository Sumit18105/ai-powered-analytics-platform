from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_required_files_exist():
    required = [
        "docker-compose.yml",
        ".env.example",
        "apps/api/Dockerfile",
        "apps/api/requirements.txt",
        "apps/api/app/main.py",
        "apps/api/app/services/profiling.py",
        "apps/web/Dockerfile",
        "apps/web/package.json",
        "apps/web/package-lock.json",
    ]
    assert all((ROOT / item).exists() for item in required)


def test_dashboard_studio_files():
    from pathlib import Path
    routes = Path("apps/api/app/api/routes.py").read_text()
    composer = Path("apps/api/app/services/composer.py").read_text()
    page = Path("apps/web/src/app/page.tsx").read_text()
    assert "/filter-options" in routes
    assert "/metrics/evaluate" in routes
    assert "/table-data" in routes
    assert "evaluate_metric" in composer
    assert "Dashboard Studio" in page
    assert "Global filters" in page
    assert "KPI builder" in page
