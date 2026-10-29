from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import router as api_router
from app.config import get_settings
from app.worker.tasks import run_scheduled_cycle

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_task = None
    if settings.scheduler_enabled and not settings.celery_enabled:

        async def local_scheduler() -> None:
            while True:
                await asyncio.to_thread(run_scheduled_cycle)
                await asyncio.sleep(30)

        scheduler_task = asyncio.create_task(local_scheduler(), name="daily-briefing-scheduler")
    yield
    if scheduler_task:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="每日 AI 新闻助手",
    version="0.1.0",
    description="来源可追溯、按个人偏好整理的 AI 新闻简报 Agent。",
    docs_url="/docs" if settings.app_env.lower() != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.app_env.lower() != "production" else None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)
app.include_router(api_router)


@app.middleware("http")
async def request_size_guard(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_request_body_bytes:
                return JSONResponse(
                    status_code=413, content={"detail": "The request body is too large."}
                )
        except ValueError:
            return JSONResponse(
                status_code=400, content={"detail": "The request content length is invalid."}
            )
    return await call_next(request)


@app.exception_handler(HTTPException)
async def safe_http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
    )
