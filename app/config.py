from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_port: int = 8000
    database_url: str = "postgresql+psycopg://smartclass:smartclass@postgres:5432/smartclass"
    presence_service_url: str = "http://presence-service:8001"
    assignment_upload_dir: str = "storage/assignments"
    object_storage_provider: str = "local"
    object_storage_local_dir: str = "storage/objects"
    object_storage_endpoint: str | None = None
    object_storage_bucket: str = "smart-class"
    object_storage_region: str = "us-east-1"
    object_storage_access_key: str | None = None
    object_storage_secret_key: str | None = None
    object_storage_force_path_style: bool = True
    object_storage_proxy_chunk_size_bytes: int = 1024 * 1024
    assignment_upload_max_files: int = 5
    assignment_upload_max_file_size_bytes: int = 10 * 1024 * 1024
    cors_origins: str = "*"
    jwt_secret: str = "smart-class-dev-jwt-secret"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 7
    access_cookie_name: str = "smartclass_access"
    access_cookie_secure: bool = False
    access_cookie_samesite: str = "lax"
    access_cookie_path: str = "/"
    access_cookie_domain: str | None = None
    refresh_cookie_name: str = "smartclass_refresh"
    refresh_cookie_secure: bool = False
    refresh_cookie_samesite: str = "lax"
    refresh_cookie_path: str = "/api/auth"
    refresh_cookie_domain: str | None = None
    auth_allow_legacy_dev_tokens: bool = True
    ap_token_hash_secret: str = "smart-class-dev-ap-token-pepper"
    presence_internal_token: str = "smart-class-dev-internal-token"
    local_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost,http://127.0.0.1"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
