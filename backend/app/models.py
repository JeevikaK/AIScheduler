from datetime import time, date
from typing import Optional
from pydantic import BaseModel
from db import Base, engine
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
    Date,
    Time,
)

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    completed = Column(Boolean, default=False)

class Mood(Base):
    __tablename__ = "moods"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True)
    mood = Column(Integer)    # 1–5
    energy = Column(Integer)  # 1–5
    note = Column(String)

class Reflection(Base):
    __tablename__ = "reflections"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True)
    text = Column(String)

class TaskBase(BaseModel):
    title: str
    description: Optional[str] = None
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None

class NLTaskCreate(BaseModel):
    text: str

Base.metadata.create_all(bind=engine)