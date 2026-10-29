import os

os.environ["APP_ENV"] = "development"
os.environ["DEMO_MODE"] = "true"
os.environ["AUTH_SECRET"] = "test-secret-not-for-production-0123456789"
os.environ["DATABASE_URL"] = "sqlite:///./data/runtime/test_daily_ai_news.db"
os.environ["CELERY_ENABLED"] = "false"

import pytest
from app.db import Base, SessionLocal, engine
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
