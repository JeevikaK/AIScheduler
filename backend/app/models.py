from app.db import Base
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
    priority = Column(Integer, default=3)   # 1 (low) → 5 (high)
    deadline = Column(Date, nullable=True)
    actual_duration = Column(Integer, nullable=True)
    reflection_energy = Column(Integer, nullable=True)  # 1–5
    reflection_mood = Column(String, nullable=True)

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
