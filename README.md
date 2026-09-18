# AI Analytics Platform 

v10 is the production-hardening release built on the confirmed working v9 Enterprise Automation baseline.

## Included

- Production-aware configuration validation
- Mandatory authentication when `APP_ENV=production`
- Strong JWT secret validation
- Explicit CORS configuration
- Configurable upload limits and scheduler controls
- PostgreSQL advisory locking so scheduled refresh/monitoring work is not duplicated across API workers
- PostgreSQL connection-pool health settings
- Live and database readiness endpoints
- Request IDs, structured request logging, and safer security headers
- API exception containment for unexpected failures
- Container health checks and restart policies
- Production uvicorn workers, with optional development reload
- Frontend dependency installation with `npm install`
- Existing v1–v9 data, analytics, dashboard, AI, workspace, connector, monitoring and automation features retained

## Development

Use the supplied `.env.example` as a starting point. The default remains compatible with the local development workflow.

```powershell
docker compose up --build
```

API: `http://localhost:8000`
Web: `http://localhost:3000`
API docs: `http://localhost:8000/docs`

## Production baseline

Set these values in a real `.env` file or deployment secret store:

```text
APP_ENV=production
AUTH_REQUIRED=true
JWT_SECRET=<random secret of at least 32 bytes>
CORS_ORIGINS=https://your-dashboard.example.com
DEV_RELOAD=false
WEB_CONCURRENCY=2
# Scheduler runs in the dedicated scheduler container, not inside API workers.
```

Do not commit secrets. Do not use `docker compose down -v` unless you intentionally want to remove the PostgreSQL volume.

## Health

- `/health/live` — process liveness
- `/health/ready` — process + database readiness
- `/health` — compatibility endpoint

## Verification

Run from the repository root:

```powershell
python -m pytest -q
```

The production build uses the same database migrations as v9; no destructive migration is introduced by v10.

## Intelligence upgrade
The platform now includes a consolidated AI & Advanced Analytics workspace:
- evidence-based automated insights for anomalies, strong relationships, segment gaps and recent movements
- natural-language analysis through the existing safe AI planning layer
- what-if scenario analysis without modifying source data
- lightweight AutoML model comparison using reproducible scikit-learn pipelines
- audit logging for AI insight, scenario and AutoML actions

AutoML is intentionally a guided comparison rather than an unrestricted model-serving system. Heavy production workloads should be moved to a background worker as scale requirements grow.

## Enterprise access

Authenticated users can create personal API keys from `/api/auth/api-keys`. Keys are stored only as SHA-256 hashes; the plaintext value is returned once at creation. Keys support optional expiration and can be revoked. Use the returned key as a bearer token in the `Authorization` header for API integrations.


## Final release scope

This release consolidates the platform into one production-oriented modular monolith covering data ingestion and preparation, EDA, visualization, Dashboard Studio, AI analysis, automated insights, scenario analysis, AutoML, executive reporting, monitoring and scheduled refresh, workspaces/RBAC, audit logs, API keys, database connectors, exports, health checks, production configuration, and multi-worker-safe scheduled jobs.

The architecture intentionally reuses Polars, NumPy, scikit-learn, ECharts, SQLAlchemy/Alembic, APScheduler, FastAPI and established export libraries rather than implementing duplicate analytical or rendering engines.

For production, set `APP_ENV=production`, `AUTH_REQUIRED=true`, a strong `JWT_SECRET`, and explicit `CORS_ORIGINS`. Keep database backups and secrets outside source control.

## Final v1.0 completion additions
- Durable database-backed analysis jobs with a dedicated worker for deep analysis, insights, reports, scenarios, and AutoML.
- Job status/history APIs for long-running analytics.
- Statistical analysis using SciPy, including descriptive statistics and optional Welch's two-sample comparison.
- The API remains stateless; long-running analytics are isolated from web request workers.
- Production deployment can scale API workers independently from the scheduler and analysis worker.
