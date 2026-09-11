FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD sh -c 'python -c "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv(\"PORT\", \"8000\")}/health/live\", timeout=3)"' || exit 1
CMD ["sh", "-c", "alembic upgrade head && if [ \"${DEV_RELOAD:-false}\" = \"true\" ]; then exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --reload; else exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2}; fi"]
