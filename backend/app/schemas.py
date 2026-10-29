from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class SendCodeRequest(ApiModel):
    email: EmailStr


class VerifyCodeRequest(SendCodeRequest):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    display_name: str = Field(default="", max_length=80)


class SessionResponse(ApiModel):
    access_token: str
    token_type: str = "bearer"
    user: dict
    demo_mode: bool = False


class SubscriptionUpsert(ApiModel):
    topics: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=20)
    match_all: bool = False
    semantic_expansion: bool = False
    item_limit: Literal[5, 10, 15] = 10
    detail_level: Literal["concise", "detailed"] = "detailed"
    send_time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    enabled: bool = True
    notify_enabled: bool = False
    version: int = Field(default=0, ge=0)

    @field_validator("topics", "keywords", "excluded_keywords")
    @classmethod
    def normalise_terms(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip()[:80] for value in values if value.strip()))


class TaskCreate(ApiModel):
    kind: Literal["preview", "daily"] = "preview"


class ReadStateUpsert(ApiModel):
    is_read: bool | None = None
    is_bookmarked: bool | None = None


class FeedbackCreate(ApiModel):
    item_index: int | None = Field(default=None, ge=0, le=30)
    kind: Literal["irrelevant", "incorrect", "source_issue", "other"]
    detail: str = Field(default="", max_length=1000)


class FeedbackUpdate(ApiModel):
    status: Literal["open", "in_review", "resolved", "dismissed"]
    reply: str = Field(default="", max_length=1000)


class TaskSummary(ApiModel):
    id: str
    status: str
    kind: str
    provider: str
    stop_reason: str
    error_message: str = ""
    created_at: datetime
    finished_at: datetime | None = None
    tools: list[dict] = Field(default_factory=list)
    token_usage: dict[str, int] = Field(default_factory=dict)


class ArticleResult(ApiModel):
    id: str
    title: str
    summary: str
    source_id: str
    url: str
    published_at: datetime
    is_demo: bool


class BriefingResult(ApiModel):
    id: str
    task_id: str
    business_date: str
    status: str
    provider: str
    is_demo: bool
    title: str
    summary: str
    items: list[dict]
    created_at: datetime
    is_read: bool = False
    is_bookmarked: bool = False
