from contextlib import asynccontextmanager
import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.auth import router as auth_router
from app.api.routes import router
from app.core.config import get_settings
from app.db.database import SessionLocal

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ai-analytics")

# Keep the hosted demo frontend reachable even if an older Render
# CORS_ORIGINS environment variable is still set to localhost.
DEPLOYED_FRONTEND_ORIGINS = {
    "https://ai-powered-analytics-platform-1.onrender.com",
    "https://ai-powered-analytics-web.onrender.com",
}
CORS_ORIGINS = list(dict.fromkeys(settings.cors_origin_list + sorted(DEPLOYED_FRONTEND_ORIGINS)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Scheduled jobs run in the dedicated scheduler container. Keeping the
    # scheduler out of Uvicorn workers prevents one scheduler per worker.
    logger.info("API started env=%s scheduler=dedicated", settings.app_env)
    try:
        yield
    finally:
        logger.info("API stopped")


app = FastAPI(
    title="AI Analytics Platform API",
    version="1.0.0",
    description="Production-ready dataset ingestion, analytics, dashboards, AI and automation APIs.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def security_and_request_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled request error request_id=%s path=%s", request_id, request.url.path)
        response = JSONResponse(status_code=500, content={"detail": "Internal server error", "request_id": request_id})
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    logger.info("%s %s -> %s %.2fms request_id=%s", request.method, request.url.path, response.status_code, elapsed_ms, request_id)
    return response


@app.get("/")
def root():
    return {"name": "AI Analytics Platform API", "version": "1.0.0", "environment": settings.app_env}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ready", "database": "ok"}
    except Exception:
        logger.exception("Readiness check failed")
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable"})


app.include_router(auth_router)
app.include_router(router)
