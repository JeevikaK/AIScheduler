from datetime import time, date
from typing import List, Optional
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

class ChatMeta(BaseModel):
    used_fallback_parser: bool = False
    used_replan_handler: bool = False
    resolved_thread_key: Optional[str] = None
    memory_used: bool = False
    affected_dates: List[date] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

class ChatResponse(BaseModel):
    mode: str
    message: str
    created_tasks: List[TaskResponse] = Field(default_factory=list)
    updated_tasks: List[TaskResponse] = Field(default_factory=list)
    unchanged_tasks: List[TaskResponse] = Field(default_factory=list)
    unscheduled_tasks: List[TaskResponse] = Field(default_factory=list)
    meta: ChatMeta

class DailyScheduleResponse(BaseModel):
    date: date
    energy: int
    message: str
    tasks: List[TaskResponse]

Base.metadata.create_all(bind=engine)
