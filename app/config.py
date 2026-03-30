from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_port: int = 8000
    database_url: str = "postgresql+psycopg://smartclass:smartclass@postgres:5432/smartclass"
    presence_service_url: str = "http://presence-service:8001"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
