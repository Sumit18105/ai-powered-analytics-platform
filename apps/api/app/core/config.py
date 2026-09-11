from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://analytics:analytics@localhost:5432/analytics"
    upload_dir: str = "./storage/uploads"
    processed_dir: str = "./storage/processed"
    max_upload_mb: int = 200
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.5"
    jwt_secret: str = "dev-only-change-this-jwt-secret-please-use-32-bytes-min"
    jwt_expire_minutes: int = 60 * 8
    auth_required: bool = False
    cors_origins: str = "http://localhost:3000"
    scheduler_enabled: bool = True
    refresh_interval_minutes: int = 1
    monitoring_interval_minutes: int = 15

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        # SQLAlchemy defaults plain PostgreSQL URLs to psycopg2.
        # This project intentionally uses psycopg 3, so normalize the
        # common plain URL form supplied by hosted PostgreSQL providers.
        if isinstance(value, str):
            if value.startswith("postgresql://"):
                return "postgresql+psycopg://" + value[len("postgresql://"):]
            if value.startswith("postgres://"):
                return "postgresql+psycopg://" + value[len("postgres://"):]
        return value

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        if len(value.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        return value

    @field_validator("max_upload_mb")
    @classmethod
    def validate_upload_limit(cls, value: int) -> int:
        if not 1 <= value <= 10_240:
            raise ValueError("MAX_UPLOAD_MB must be between 1 and 10240")
        return value

    @model_validator(mode="after")
    def validate_production(self):
        if self.app_env.lower() == "production":
            if self.jwt_secret.startswith("dev-only-change-this"):
                raise ValueError("Set a strong JWT_SECRET before running in production")
            if not self.auth_required:
                raise ValueError("AUTH_REQUIRED must be true in production")
            if self.cors_origins.strip() == "*":
                raise ValueError("CORS_ORIGINS cannot be '*' in production")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def processed_path(self) -> Path:
        path = Path(self.processed_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def upload_path(self) -> Path:
        path = Path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
