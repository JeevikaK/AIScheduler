from datetime import time, date
from typing import Optional
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

class NLTaskCreate(BaseModel):
    text: str

class ChatInput(BaseModel):
    message: str

Base.metadata.create_all(bind=engine)