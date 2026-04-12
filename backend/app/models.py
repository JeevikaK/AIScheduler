from app.db import Base
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    Time,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
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


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_key = Column(String, index=True, nullable=False)
    thread_date = Column(Date, index=True, nullable=False)
    chat_thread_id = Column(String, index=True, nullable=True)
    role = Column(String, nullable=False)
    message_text = Column(String, nullable=False)
    meta_text = Column(String, nullable=True)
    payload_json = Column(String, nullable=True)
    created_at = Column(DateTime, index=True, nullable=False, default=datetime.utcnow)


class IntegrationAuthSession(Base):
    __tablename__ = "integration_auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, unique=True, index=True)
    return_path = Column(String, nullable=False, default="/recentactivities")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ConnectedAccount(Base):
    __tablename__ = "connected_accounts"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, index=True)
    external_account_id = Column(String, nullable=False, index=True)
    email = Column(String, nullable=True, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime, nullable=True)
    connection_status = Column(String, nullable=False, default="connected")
    account_meta_json = Column(Text, nullable=True, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("provider", "external_account_id", name="uq_connected_account_provider_external_id"),
    )


class ConnectedCalendar(Base):
    __tablename__ = "connected_calendars"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("connected_accounts.id"), nullable=False, index=True)
    provider_calendar_id = Column(String, nullable=False, index=True)
    calendar_name = Column(String, nullable=False)
    color = Column(String, nullable=True)
    selected_for_sync = Column(Boolean, nullable=False, default=False)
    sync_state_json = Column(Text, nullable=True, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("account_id", "provider_calendar_id", name="uq_connected_calendar_account_provider_id"),
    )


class ExternalEvent(Base):
    __tablename__ = "external_events"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("connected_accounts.id"), nullable=False, index=True)
    connected_calendar_id = Column(Integer, ForeignKey("connected_calendars.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    provider_event_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    location = Column(String, nullable=True)
    start_at = Column(DateTime, nullable=False, index=True)
    end_at = Column(DateTime, nullable=False, index=True)
    timezone = Column(String, nullable=True)
    all_day = Column(Boolean, nullable=False, default=False)
    status = Column(String, nullable=False, default="confirmed")
    cancelled = Column(Boolean, nullable=False, default=False)
    etag = Column(String, nullable=True)
    source_url = Column(String, nullable=True)
    calendar_label = Column(String, nullable=True)
    event_meta_json = Column(Text, nullable=True, default="{}")
    last_synced_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("connected_calendar_id", "provider_event_id", name="uq_external_event_calendar_provider_id"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    setting_key = Column(String, nullable=False, unique=True, index=True)
    setting_value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
