# app/services/tools.py
from datetime import date, datetime, timedelta, time
from typing import Optional
import json
import uuid
from app.db import get_db
from services.planner import get_historical_avg_duration, get_overdue_tasks, infer_task_type, reschedule_task, update_duration_model
from services.scheduling import extract_date_from_text, infer_duration_minutes
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.models import ConversationMessage, Reflection, Task, Mood, ConversationThread
from app.schemas import ReflectionCreate, TaskReflectionInput
from fastapi import Depends, Query


def _format_chat_task_line(task: dict) -> str:
    task_date = task.get("date") or ""
    start = (task.get("start_time") or "")[:5] if task.get("start_time") else "No time"
    end = (task.get("end_time") or "")[:5] if task.get("end_time") else ""
    time_label = f"{start}-{end}" if end else start
    return f"- {task.get('title') or 'Task'} ({task_date} {time_label})"


def _build_assistant_message_text(message_text: str, payload_json: str | None) -> str:
    if not payload_json:
        return message_text

    try:
        payload = json.loads(payload_json)
    except Exception:
        return message_text

    sections = [payload.get("message") or message_text]
    lines = []

    if payload.get("mode") == "schedule" and payload.get("unchanged_tasks"):
        lines.extend(_format_chat_task_line(task) for task in payload.get("unchanged_tasks", []))
    else:
        created = payload.get("created_tasks") or []
        updated = payload.get("updated_tasks") or []
        if created:
            lines.append("Created:")
            lines.extend(_format_chat_task_line(task) for task in created)
        if updated:
            if lines:
                lines.append("")
            lines.append("Updated:")
            lines.extend(_format_chat_task_line(task) for task in updated)

    if lines:
        sections.append("\n".join(lines))

    return "\n\n".join(part for part in sections if part)

def infer_priority(text: str):
    text = text.lower()

    if any(word in text for word in ["urgent", "asap", "important", "critical"]):
        return 5
    if any(word in text for word in ["high priority", "must", "deadline"]):
        return 4
    if any(word in text for word in ["low priority", "optional", "if possible"]):
        return 2

    return 3

def create_task(db: Session, title: str, description: str, date, start_time=None, end_time=None, priority=1, completed=False, duration_minutes=None):
    """
    Create a task in the DB. Mirrors /tasks endpoint.
    """
    task_data = {
        "title": title,
        "description": description,
        "date": date,
        "start_time": start_time,
        "end_time": end_time,
        "priority": priority,
        "completed": completed,
        "duration_minutes": duration_minutes,
    }
    existing = (
        db.query(Task)
        .filter(
            Task.title == title,
            Task.date == date,
            Task.start_time == start_time,
            Task.end_time == end_time,
            Task.completed == False,
        )
        .first()
    )
    if existing:
        return existing

    db_task = Task(**task_data)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task

def create_mood(db: Session, date, energy: int, mood_label: str):
    """
    Create a mood record in the DB. Mirrors /mood endpoint.
    """
    existing = db.query(Mood).filter(Mood.date == date).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Mood for this date already exists."
        )
    db_mood = Mood(date=date, energy=energy, mood=mood_label)
    db.add(db_mood)
    db.commit()
    db.refresh(db_mood)
    return db_mood

def create_reflection(
    reflection: ReflectionCreate,
    db: Session = Depends(get_db)
):
    db_reflection = Reflection(**reflection.dict())
    db.add(db_reflection)
    db.commit()
    db.refresh(db_reflection)
    return db_reflection


def get_tasks(task_date: date, db: Session = Depends(get_db)):
    return db.query(Task).filter(Task.date == task_date).all()

def get_mood(date_given: date, db: Session = Depends(get_db)):

    db_mood = db.query(Mood).filter(Mood.date == date_given).first()
    if not db_mood:
        raise HTTPException(status_code=404, detail="Mood not found")
    return db_mood

def get_reflection(date: date, db: Session = Depends(get_db)):
    reflection = db.query(Reflection).filter(Reflection.date == date).first()
    if not reflection:
        raise HTTPException(status_code=404, detail="Reflection not found")
    return reflection

def get_today_schedule(db: Session = Depends(get_db)):
    today = date.today()

    # 1. Fetch today's mood / energy
    mood = db.query(Mood).filter(Mood.date == today).first()
    energy = mood.energy if mood else 3

    # 2. Fetch today's tasks (no auto-reschedule)
    tasks = (
        db.query(Task)
        .filter(Task.date == today)
        .order_by(Task.priority.desc(), Task.start_time)
        .all()
    )

    # 3. Agent-style summary message
    if energy <= 2:
        message = "Low-energy day detected. Focus on light or high-priority tasks."
    elif energy >= 4:
        message = "High-energy day! Great time for deep or important work."
    else:
        message = "Balanced day ahead. Stay consistent."

    return {
        "date": today,
        "energy": energy,
        "message": message,
        "tasks": tasks,
    }

def get_schedule(schedule_date: date, db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.date == schedule_date).all()
    tasks.sort(
        key=lambda t: (
            1 if t.start_time is None else 0,
            t.start_time or time(23, 59),
            t.priority,
            t.id,
        )
    )
    return tasks

def complete_task(task_id: int, actual_end: Optional[datetime] = None, db: Session = Depends(get_db)):
    db_task = db.query(Task).filter(Task.id == task_id).first()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    db_task.completed = True

    # Compute actual duration
    if db_task.start_time and actual_end:
        start_dt = datetime.combine(db_task.date, db_task.start_time)
        duration_min = int((actual_end - start_dt).total_seconds() / 60)
        db_task.actual_duration = duration_min

    db.commit()
    db.refresh(db_task)
    return db_task

def reflect_on_task(
    task_id: int,
    reflection: TaskReflectionInput,
    db: Session = Depends(get_db)
):
    task = db.query(Task).filter(Task.id == task_id).first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.completed = reflection.completed
    task.actual_duration = reflection.actual_duration
    task.reflection_energy = reflection.energy
    task.reflection_mood = reflection.mood

    db.commit()

    update_duration_model(db, task)

    return {"status": "reflection recorded"}

def delete_task(task_id: int, db: Session = Depends(get_db)):
    db_task = db.query(Task).filter(Task.id == task_id).first()

    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    db.delete(db_task)
    db.commit()
    return {"detail": "Task deleted successfully"}


def clear_today_tasks(db: Session = Depends(get_db)):
    today = date.today()
    deleted_count = (
        db.query(Task)
        .filter(Task.date == today)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {
        "date": today.isoformat(),
        "deleted_count": int(deleted_count),
        "detail": "Today's tasks cleared successfully",
    }

def reschedule_overdue_tasks(db: Session = Depends(get_db)):
    overdue_tasks = get_overdue_tasks(db)

    rescheduled = 0
    failed = []

    for task in overdue_tasks:
        success = reschedule_task(task, db)
        if success:
            rescheduled += 1
        else:
            failed.append(task.id)

    return {
        "rescheduled": rescheduled,
        "failed_tasks": failed,
    }

def get_summary(summary_date: date = Query(...), db: Session = Depends(get_db)):
    # Fetch tasks for the day
    tasks = db.query(Task).filter(Task.date == summary_date).all()
    total_tasks = len(tasks)
    completed_tasks = len([t for t in tasks if t.completed])
    completion_rate = total_tasks and completed_tasks / total_tasks or 0

    # Fetch mood for the day
    mood_entry = db.query(Mood).filter(Mood.date == summary_date).first()
    mood = mood_entry.mood if mood_entry else None
    energy = mood_entry.energy if mood_entry else None

    # Optional: fetch reflection
    reflection_entry = db.query(Reflection).filter(Reflection.date == summary_date).first()
    reflection = reflection_entry.text if reflection_entry else None


def get_chat_threads(thread_date: date, db: Session = Depends(get_db)):
    records = (
        db.query(ConversationThread)
        .filter(
            ConversationThread.thread_date == thread_date,
            ConversationThread.chat_thread_id.isnot(None),
            ConversationThread.chat_thread_id != "",
        )
        .order_by(ConversationThread.updated_at.desc(), ConversationThread.created_at.desc())
        .all()
    )

    results = []
    for rec in records:
        preview = (rec.last_user_message or "").strip()
        title = preview[:48] if preview else "New chat"
        results.append(
            {
                "chat_thread_id": rec.chat_thread_id,
                "thread_date": rec.thread_date,
                "title": title,
                "preview": preview or "No messages yet",
                "pending_intent_id": rec.pending_intent_id,
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
            }
        )

    return results


def create_chat_thread(thread_date: date, db: Session = Depends(get_db)):
    chat_thread_id = f"web-{uuid.uuid4()}"
    thread_key = f"{chat_thread_id}:{thread_date.isoformat()}"
    record = ConversationThread(
        thread_key=thread_key,
        thread_date=thread_date,
        chat_thread_id=chat_thread_id,
        last_intent_type="new_thread",
        last_user_message=None,
        last_created_task_ids="[]",
        last_updated_task_ids="[]",
        last_referenced_task_ids="[]",
        pending_intent_id=None,
        pending_state_type=None,
        pending_state_json="{}",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "chat_thread_id": record.chat_thread_id,
        "thread_date": record.thread_date,
        "title": "New chat",
        "preview": "No messages yet",
        "pending_intent_id": record.pending_intent_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def delete_chat_thread(thread_date: date, chat_thread_id: str, db: Session = Depends(get_db)):
    thread_key = f"{chat_thread_id}:{thread_date.isoformat()}"
    record = db.query(ConversationThread).filter(ConversationThread.thread_key == thread_key).first()
    if not record:
        raise HTTPException(status_code=404, detail="Chat thread not found")

    (
        db.query(ConversationMessage)
        .filter(ConversationMessage.thread_key == thread_key)
        .delete(synchronize_session=False)
    )
    db.delete(record)
    db.commit()
    return {"detail": "Chat thread deleted successfully", "chat_thread_id": chat_thread_id, "thread_date": thread_date.isoformat()}


def get_chat_thread_detail(thread_date: date, chat_thread_id: str, db: Session = Depends(get_db)):
    thread_key = f"{chat_thread_id}:{thread_date.isoformat()}"
    record = db.query(ConversationThread).filter(ConversationThread.thread_key == thread_key).first()
    if not record:
        raise HTTPException(status_code=404, detail="Chat thread not found")

    messages = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.thread_key == thread_key)
        .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
        .all()
    )
    latest_assistant = (
        db.query(ConversationMessage)
        .filter(
            ConversationMessage.thread_key == thread_key,
            ConversationMessage.role == "assistant",
            ConversationMessage.payload_json.isnot(None),
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .first()
    )
    latest_response = None
    if latest_assistant and latest_assistant.payload_json:
        latest_response = json.loads(latest_assistant.payload_json)

    preview = (record.last_user_message or "").strip() or "No messages yet"
    title = preview[:48] if record.last_user_message else "New chat"

    return {
        "chat_thread_id": record.chat_thread_id,
        "thread_date": record.thread_date,
        "title": title,
        "preview": preview,
        "pending_intent_id": record.pending_intent_id,
        "messages": [
            {
                "role": message.role,
                "text": _build_assistant_message_text(message.message_text, message.payload_json) if message.role == "assistant" else message.message_text,
                "meta": message.meta_text,
                "payload": json.loads(message.payload_json) if message.payload_json else None,
                "created_at": message.created_at,
            }
            for message in messages
        ],
        "latest_response": latest_response,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }

    return {
        "date": summary_date,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "completion_rate": completion_rate,
        "mood": mood,
        "energy": energy,
        "reflection": reflection,
    }


def get_dashboard_overview(db: Session = Depends(get_db)):
    today = date.today()

    def build_trend(days: int):
        start_date = today - timedelta(days=days - 1)
        points = []
        current = start_date
        while current <= today:
            tasks = db.query(Task).filter(Task.date == current).all()
            mood_entry = db.query(Mood).filter(Mood.date == current).first()
            total_tasks = len(tasks)
            completed_tasks = len([task for task in tasks if task.completed])
            completion_rate = (completed_tasks / total_tasks) if total_tasks else 0.0
            points.append(
                {
                    "date": current,
                    "mood": mood_entry.mood if mood_entry else None,
                    "energy": mood_entry.energy if mood_entry else None,
                    "completed_tasks": completed_tasks,
                    "total_tasks": total_tasks,
                    "completion_rate": round(completion_rate, 3),
                }
            )
            current += timedelta(days=1)
        return points

    completed_tasks_30d = (
        db.query(Task)
        .filter(Task.date >= today - timedelta(days=29), Task.date <= today, Task.completed == True)
        .all()
    )
    productivity_counts = {hour: 0 for hour in range(24)}
    duration_ratios = []
    for task in completed_tasks_30d:
        if task.start_time:
            productivity_counts[task.start_time.hour] += 1
        if task.actual_duration and task.duration_minutes and task.duration_minutes > 0:
            duration_ratios.append(task.actual_duration / task.duration_minutes)

    productivity_hours = [
        {
            "hour": hour,
            "label": f"{hour:02d}:00",
            "completed_tasks": productivity_counts[hour],
        }
        for hour in range(24)
    ]
    best_hour = max(productivity_hours, key=lambda item: item["completed_tasks"], default={"hour": 9})
    best_window = f"{best_hour['hour']:02d}:00 - {(best_hour['hour'] + 1) % 24:02d}:00"

    trend_7 = build_trend(7)
    trend_30 = build_trend(30)
    total_tasks_7 = sum(point["total_tasks"] for point in trend_7)
    total_completed_7 = sum(point["completed_tasks"] for point in trend_7)
    total_tasks_30 = sum(point["total_tasks"] for point in trend_30)
    total_completed_30 = sum(point["completed_tasks"] for point in trend_30)
    avg_ratio = (sum(duration_ratios) / len(duration_ratios)) if duration_ratios else 1.0
    duration_accuracy = max(0.0, 1.0 - abs(1.0 - avg_ratio))

    return {
        "last_7_days": trend_7,
        "last_30_days": trend_30,
        "productivity_hours": productivity_hours,
        "best_productivity_window": best_window,
        "efficiency": {
            "completion_rate_7d": round((total_completed_7 / total_tasks_7), 3) if total_tasks_7 else 0.0,
            "completion_rate_30d": round((total_completed_30 / total_tasks_30), 3) if total_tasks_30 else 0.0,
            "duration_accuracy": round(duration_accuracy, 3),
            "avg_actual_vs_planned_ratio": round(avg_ratio, 3),
        },
    }
