from datetime import time, date, datetime
from typing import Any, List, Optional
from pydantic import BaseModel, Field
from app.db import Base, engine
from app import models as _models  # noqa: F401  # ensure model registration before create_all

class TaskBase(BaseModel):
    title: str
    description: Optional[str] = None
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None


class TaskCreate(BaseModel):
    title: str
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    priority: int = 1
    description: str
    
class TaskResponse(TaskBase):
    id: int
    completed: bool
    model_config = {
        "from_attributes": True
    }

class TaskReflectionInput(BaseModel):
    completed: bool
    actual_duration: int  # minutes
    energy: int           # 1–5
    mood: Optional[str] = None

class MoodCreate(BaseModel):
    date: date
    mood: int
    energy: int
    note: Optional[str] = None

class MoodResponse(MoodCreate):
    id: int
    model_config = {
        "from_attributes": True
    }

class ReflectionCreate(BaseModel):
    date: date
    text: str

class ReflectionResponse(ReflectionCreate):
    id: int
    model_config = {
        "from_attributes": True
    }

class ChatInput(BaseModel):
    message: str
    chat_thread_id: Optional[str] = None
    thread_date: Optional[date] = None
    pending_response: Optional[dict[str, Any]] = None
    confirm: Optional[bool] = None


class ClarificationOption(BaseModel):
    id: str
    label: str
    value: str


class ClarificationPrompt(BaseModel):
    intent_id: str
    question: str
    field: str
    options: List[ClarificationOption] = Field(default_factory=list)
    allow_free_text: bool = True


class ConflictTaskRef(BaseModel):
    task_id: int
    title: str
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    is_fixed: bool = False


class SlotSuggestion(BaseModel):
    slot_id: str
    date: date
    start_time: time
    end_time: time
    score: float
    reason: str


class ConflictPrompt(BaseModel):
    intent_id: str
    new_task_draft: dict[str, Any]
    conflicting_tasks: List[ConflictTaskRef] = Field(default_factory=list)
    suggested_slots: List[SlotSuggestion] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)

class ChatMeta(BaseModel):
    used_fallback_parser: bool = False
    used_replan_handler: bool = False
    resolved_thread_key: Optional[str] = None
    memory_used: bool = False
    requires_user_input: bool = False
    pending_intent_id: Optional[str] = None
    applied_after_confirmation: bool = False
    affected_dates: List[date] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

class ChatResponse(BaseModel):
    mode: str
    message: str
    created_tasks: List[TaskResponse] = Field(default_factory=list)
    updated_tasks: List[TaskResponse] = Field(default_factory=list)
    unchanged_tasks: List[TaskResponse] = Field(default_factory=list)
    unscheduled_tasks: List[TaskResponse] = Field(default_factory=list)
    clarification: Optional[ClarificationPrompt] = None
    conflict_info: Optional[ConflictPrompt] = None
    meta: ChatMeta

class ChatThreadSummaryResponse(BaseModel):
    chat_thread_id: str
    thread_date: date
    title: str
    preview: str
    pending_intent_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class ConversationMessageResponse(BaseModel):
    role: str
    text: str
    meta: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    created_at: datetime


class ChatThreadCreateResponse(BaseModel):
    chat_thread_id: str
    thread_date: date
    title: str
    preview: str
    pending_intent_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ChatThreadDetailResponse(BaseModel):
    chat_thread_id: str
    thread_date: date
    title: str
    preview: str
    pending_intent_id: Optional[str] = None
    messages: List[ConversationMessageResponse] = Field(default_factory=list)
    latest_response: Optional[ChatResponse] = None
    created_at: datetime
    updated_at: datetime

class DailyScheduleResponse(BaseModel):
    date: date
    energy: int
    message: str
    tasks: List[TaskResponse]


class TrendPoint(BaseModel):
    date: date
    mood: Optional[int] = None
    energy: Optional[int] = None
    completed_tasks: int = 0
    total_tasks: int = 0
    completion_rate: float = 0.0


class ProductivityHour(BaseModel):
    hour: int
    label: str
    completed_tasks: int = 0


class EfficiencyMetrics(BaseModel):
    completion_rate_7d: float = 0.0
    completion_rate_30d: float = 0.0
    duration_accuracy: float = 0.0
    avg_actual_vs_planned_ratio: float = 0.0


class DashboardOverviewResponse(BaseModel):
    last_7_days: List[TrendPoint] = Field(default_factory=list)
    last_30_days: List[TrendPoint] = Field(default_factory=list)
    productivity_hours: List[ProductivityHour] = Field(default_factory=list)
    best_productivity_window: str
    efficiency: EfficiencyMetrics


class IntegrationProviderResponse(BaseModel):
    provider: str
    label: str
    configured: bool
    auth_path: str
    status: str = "available"


class IntegrationStartResponse(BaseModel):
    provider: str
    authorization_url: str


class ConnectedCalendarSelectionInput(BaseModel):
    calendar_ids: List[str] = Field(default_factory=list)
    sync_immediately: bool = True


class ConnectedCalendarResponse(BaseModel):
    id: int
    provider_calendar_id: str
    calendar_name: str
    color: Optional[str] = None
    selected_for_sync: bool
    last_synced_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ConnectedAccountResponse(BaseModel):
    id: int
    provider: str
    email: Optional[str] = None
    external_account_id: str
    connection_status: str
    last_synced_at: Optional[datetime] = None
    calendars: List[ConnectedCalendarResponse] = Field(default_factory=list)


class IntegrationSyncResponse(BaseModel):
    account_id: int
    provider: str
    synced_events: int
    selected_calendars: int
    status: str = "ok"


class ExternalEventResponse(BaseModel):
    id: int
    account_id: int
    connected_calendar_id: int
    provider: str
    provider_event_id: str
    title: str
    description: Optional[str] = None
    location: Optional[str] = None
    start_at: datetime
    end_at: datetime
    timezone: Optional[str] = None
    all_day: bool = False
    status: str
    cancelled: bool = False
    readonly: bool = True
    calendar_label: Optional[str] = None
    account_email: Optional[str] = None
    color: Optional[str] = None
    source_url: Optional[str] = None
    organizer: Optional[str] = None
    attendee_count: int = 0


class WeeklyCalendarDayResponse(BaseModel):
    date: date
    tasks: List[TaskResponse] = Field(default_factory=list)
    external_events: List[ExternalEventResponse] = Field(default_factory=list)


class WeeklyCalendarResponse(BaseModel):
    start_date: date
    days: List[WeeklyCalendarDayResponse] = Field(default_factory=list)


class IntegrationSettingsResponse(BaseModel):
    app_base_url: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = ""


class IntegrationSettingsUpdate(BaseModel):
    app_base_url: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = ""

Base.metadata.create_all(bind=engine)
