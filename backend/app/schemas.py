from datetime import time, date
from typing import List, Optional
from pydantic import BaseModel
from app.db import Base, engine

class TaskBase(BaseModel):
    title: str
    description: Optional[str] = None
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None

class TaskCreate(TaskBase):
    pass

class TaskResponse(TaskBase):
    id: int
    completed: bool

    class Config:
        orm_mode = True

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

    class Config:
        orm_mode = True

class ReflectionCreate(BaseModel):
    date: date
    text: str

class ReflectionResponse(ReflectionCreate):
    id: int

    class Config:
        orm_mode = True

class ChatInput(BaseModel):
    message: str

class DailyScheduleResponse(BaseModel):
    date: date
    energy: int
    message: str
    tasks: List[TaskResponse]

Base.metadata.create_all(bind=engine)