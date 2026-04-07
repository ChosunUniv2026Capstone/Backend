from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_port: int = 8000
    database_url: str = "postgresql+psycopg://smartclass:smartclass@postgres:5432/smartclass"
    presence_service_url: str = "http://presence-service:8001"
    cors_origins: str = "*"
    jwt_secret: str = "smart-class-dev-jwt-secret"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 7
    refresh_cookie_name: str = "smartclass_refresh"
    refresh_cookie_secure: bool = False
    refresh_cookie_samesite: str = "lax"
    refresh_cookie_path: str = "/api/auth"
    refresh_cookie_domain: str | None = None
    auth_allow_legacy_dev_tokens: bool = True
    local_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost,http://127.0.0.1"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
