import pytest
from app.config import Settings


def _production_settings(database_url: str, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "app_env": "production",
        "demo_mode": False,
        "database_url": database_url,
        "auth_secret": "production-auth-secret-" + "a" * 40,
        "llm_base_url": "https://model.example/v1",
        "llm_api_key": "test-model-key",
        "llm_model": "test-model",
        "smtp_host": "smtp.example",
        "smtp_from": "login@example.test",
        "celery_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_rejects_compose_fallback_postgres_password():
    with pytest.raises(ValueError, match="random POSTGRES_PASSWORD"):
        _production_settings(
            "postgresql+psycopg://daily_ai_news:local-only-change-me@db:5432/daily_ai_news"
        )


def test_production_rejects_non_postgresql_database():
    with pytest.raises(ValueError, match="PostgreSQL DATABASE_URL"):
        _production_settings("sqlite:///./data/runtime/daily_ai_news.db")


def test_production_accepts_postgresql_with_long_password():
    settings = _production_settings(
        "postgresql+psycopg://daily_ai_news:"
        + "a" * 43
        + "@db:5432/daily_ai_news"
    )

    assert settings.database_url.startswith("postgresql+psycopg://")


def test_production_rejects_plaintext_model_endpoint():
    with pytest.raises(ValueError, match="HTTPS LLM_BASE_URL"):
        Settings(
            _env_file=None,
            app_env="production",
            demo_mode=False,
            database_url="postgresql+psycopg://daily_ai_news:" + "a" * 43 + "@db:5432/app",
            auth_secret="production-auth-secret-" + "a" * 40,
            llm_base_url="http://model.example/v1",
            llm_api_key="test-model-key",
            llm_model="test-model",
            smtp_host="smtp.example",
            smtp_from="login@example.test",
            celery_enabled=True,
        )


def test_production_rejects_unencrypted_smtp():
    with pytest.raises(ValueError, match="SMTP_STARTTLS=true or SMTP_PORT=465"):
        _production_settings(
            "postgresql+psycopg://daily_ai_news:" + "a" * 43 + "@db:5432/app",
            smtp_starttls=False,
            smtp_port=587,
        )


def test_production_allows_implicit_tls_smtp():
    settings = _production_settings(
        "postgresql+psycopg://daily_ai_news:" + "a" * 43 + "@db:5432/app",
        smtp_starttls=False,
        smtp_port=465,
    )

    assert settings.smtp_port == 465
    assert settings.smtp_starttls is False


def test_unrecognized_environment_cannot_skip_production_validation():
    with pytest.raises(ValueError, match="APP_ENV must be either development or production"):
        Settings(_env_file=None, app_env="prod")


def test_development_keeps_local_sqlite_default():
    settings = Settings(_env_file=None, app_env="development")

    assert settings.database_url.startswith("sqlite:")
