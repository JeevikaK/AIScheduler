from app.db import Base
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    Time,
    DateTime,
)
from datetime import datetime


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    completed = Column(Boolean, default=False)
    priority = Column(Integer, default=3)   # 1 (low) → 5 (high)
    deadline = Column(Date, nullable=True)
    actual_duration = Column(Integer, nullable=True)
    reflection_energy = Column(Integer, nullable=True)  # 1–5
    reflection_mood = Column(String, nullable=True)
    duration_minutes = Column(Integer)

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

class DurationStats(Base):
    __tablename__ = "duration_stats"

    id = Column(Integer, primary_key=True)
    task_type = Column(String, index=True)
    energy = Column(Integer)  # 1–5
    avg_delta = Column(Integer, default=0)
    count = Column(Integer, default=0)


class ConversationThread(Base):
    __tablename__ = "conversation_threads"

    id = Column(Integer, primary_key=True, index=True)
    thread_key = Column(String, unique=True, index=True, nullable=False)
    thread_date = Column(Date, index=True, nullable=False)
    chat_thread_id = Column(String, index=True, nullable=True)
    last_intent_type = Column(String, nullable=True)
    last_user_message = Column(String, nullable=True)
    last_created_task_ids = Column(String, nullable=True)      # JSON string
    last_updated_task_ids = Column(String, nullable=True)      # JSON string
    last_referenced_task_ids = Column(String, nullable=True)   # JSON string
    pending_intent_id = Column(String, nullable=True)
    pending_state_type = Column(String, nullable=True)         # clarification | conflict_resolution
    pending_state_json = Column(String, nullable=True)         # JSON object
    updated_at = Column(DateTime, index=True, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
