from typing import Optional
from services.scheduling import find_available_slot, infer_duration_minutes, store_duration_delta
from fastapi import Depends, Query
from app.models import DurationStats, Task, Mood, Reflection
from app.db import get_db

import app
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session

def compute_adjustment_factor(task: Task, reflection: Reflection, mood: Mood):
    """
    Returns a multiplier for future duration estimates.
    >1 = take more time, <1 = take less time
    """
    factor = 1.0

    # Mood-based adjustment: low energy → take more time
    if mood.energy <= 2:
        factor += 0.2
    elif mood.energy == 3:
        factor += 0.1

    # Reflection keywords
    text = reflection.text.lower()
    if any(word in text for word in ["tired", "slow", "struggle", "hard"]):
        factor += 0.2
    elif any(word in text for word in ["fast", "easy", "quick"]):
        factor -= 0.1

    # Actual duration vs planned
    if task.actual_duration and task.end_time and task.start_time:
        planned_duration = int(
            (datetime.combine(task.date, task.end_time) - datetime.combine(task.date, task.start_time)).total_seconds() / 60
        )
        ratio = task.actual_duration / planned_duration
        factor *= ratio

    return factor

def get_historical_avg_duration(
    db: Session,
    task_type: str,
    energy: Optional[int] = None,
):
    query = db.query(DurationStats).filter(
        DurationStats.task_type == task_type
    )

    if energy:
        query = query.filter(DurationStats.energy == energy)

    stat = query.first()
    return stat.avg_delta if stat else 0


def infer_task_type(text: str) -> str:
    text = text.lower()

    if any(word in text for word in ["study", "revise", "read", "learn"]):
        return "study"

    if any(word in text for word in ["assignment", "homework", "problem", "quiz"]):
        return "academic_work"

    if any(word in text for word in ["meeting", "call", "sync"]):
        return "meeting"

    if any(word in text for word in ["exercise", "workout", "gym"]):
        return "exercise"

    if any(word in text for word in ["write", "essay", "report", "blog"]):
        return "writing"

    return "general"

def get_overdue_tasks(db: Session):
    now = datetime.now()

    return (
        db.query(Task)
        .filter(
            Task.completed == False,
            (
                (Task.date < now.date()) |
                (
                    (Task.date == now.date()) &
                    (Task.end_time < now.time())
                )
            )
        )
        .order_by(Task.priority.desc())
        .all()
    )

def reschedule_task(task: Task, db: Session):
    today = date.today()

    # Try today first, then next 3 days
    for offset in range(0, 4):
        target_date = today + timedelta(days=offset)

        mood = db.query(Mood).filter(Mood.date == target_date).first()
        energy = mood.energy if mood else 3

        start, end = find_available_slot(
            target_date,
            energy,
            db,
            duration_min=(
                task.actual_duration
                or infer_duration_minutes(
                    text=task.title,
                    task_type=infer_task_type(task.title),
                    historical_avg=get_historical_avg_duration(
                        db,
                        infer_task_type(task.title),
                        energy
                    ),
                )
            ),
            deadline=task.deadline,
        )

        if start:
            task.date = target_date
            task.start_time = start
            task.end_time = end
            db.commit()
            return True

    return False

def update_duration_model(db: Session, task: Task):
    if not task.actual_duration:
        return

    error = task.actual_duration - (
        (task.end_time.hour * 60 + task.end_time.minute) -
        (task.start_time.hour * 60 + task.start_time.minute)
    )

    store_duration_delta(
        db,
        task_type=infer_task_type(task.title),
        energy=task.reflection_energy,
        delta=error
    )