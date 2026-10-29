from functools import lru_cache
from urllib.parse import unquote, urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    demo_mode: bool = True
    database_url: str = "sqlite:///./data/runtime/daily_ai_news.db"
    db_pool_size: int = Field(default=10, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_timeout_seconds: int = Field(default=5, ge=1, le=60)
    redis_url: str = "redis://localhost:6379/0"
    celery_enabled: bool = False
    scheduler_enabled: bool = True
    auth_secret: str = "local-demo-secret-set-a-random-secret-before-public-use"
    admin_emails: str = ""
    base_url: str = "http://localhost:8080"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_enable_thinking: bool = True
    llm_max_task_tokens: int = Field(default=30_000, ge=2_000, le=100_000)
    llm_max_completion_tokens: int = Field(default=8_192, ge=512, le=32_768)
    llm_timeout_seconds: int = Field(default=120, ge=5, le=180)
    llm_max_turns: int = Field(default=12, ge=1, le=20)
    llm_max_tool_calls: int = Field(default=20, ge=1, le=30)
    login_code_email_hour_limit: int = Field(default=5, ge=1, le=30)
    login_code_ip_hour_limit: int = Field(default=30, ge=1, le=300)
    daily_dispatch_batch_size: int = Field(default=10_000, ge=100, le=20_000)
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    rss_refresh_minutes: int = Field(default=30, ge=5, le=1440)
    feed_request_timeout_seconds: int = Field(default=15, ge=3, le=60)
    tool_task_root: str = "data/workspaces"
    max_request_body_bytes: int = Field(default=1_048_576, ge=16_384, le=10_485_760)

    @model_validator(mode="after")
    def enforce_public_service_requirements(self) -> "Settings":
        self.app_env = self.app_env.strip().lower()
        if self.app_env not in {"development", "production"}:
            raise ValueError("APP_ENV must be either development or production.")
        if self.app_env == "production":
            missing = []
            if self.demo_mode:
                missing.append("DEMO_MODE=false")
            if len(self.auth_secret) < 32 or "local-demo-secret" in self.auth_secret:
                missing.append("a random AUTH_SECRET of at least 32 characters")
            if not self.llm_api_key or not self.llm_model or not self.llm_base_url:
                missing.append("LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL")
            else:
                try:
                    model_endpoint = urlsplit(self.llm_base_url)
                    model_endpoint_is_secure = (
                        model_endpoint.scheme.casefold() == "https"
                        and bool(model_endpoint.hostname)
                        and not model_endpoint.username
                        and not model_endpoint.password
                        and not model_endpoint.fragment
                    )
                except ValueError:
                    model_endpoint_is_secure = False
                if not model_endpoint_is_secure:
                    missing.append("an HTTPS LLM_BASE_URL without embedded credentials")
            if not self.smtp_host or not self.smtp_from:
                missing.append("SMTP_HOST and SMTP_FROM for verified email sign-in")
            elif self.smtp_port != 465 and not self.smtp_starttls:
                missing.append("SMTP_STARTTLS=true or SMTP_PORT=465 for encrypted email")
            if not self.celery_enabled:
                missing.append("CELERY_ENABLED=true and an external worker")
            try:
                database = urlsplit(self.database_url)
                database_password = unquote(database.password or "")
            except ValueError:
                database = None
                database_password = ""
            if database is None or database.scheme not in {
                "postgresql",
                "postgresql+psycopg",
            }:
                missing.append("PostgreSQL DATABASE_URL")
            elif len(database_password) < 32 or database_password == "local-only-change-me":
                missing.append("a random POSTGRES_PASSWORD of at least 32 characters")
            if missing:
                raise ValueError("Production configuration is incomplete: " + "; ".join(missing))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
