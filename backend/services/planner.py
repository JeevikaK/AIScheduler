from typing import Optional
from services.scheduling import optimize_day_schedule, infer_duration_minutes, store_duration_delta
from fastapi import Depends, Query
from app.models import DurationStats, Task, Mood, Reflection
from app.db import get_db

import app
from datetime import date, datetime, timedelta, time
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
    """
    Reschedule a single task intelligently based on mood, energy,
    other tasks, and priority. Tries today + next 3 days.
    """
    today = date.today()

    # Try today first, then next 3 days
    for offset in range(0, 4):
        target_date = today + timedelta(days=offset)

        # Get mood/energy
        mood = db.query(Mood).filter(Mood.date == target_date).first()
        energy = mood.energy if mood else 3

        # Determine duration
        duration_min = (
            task.actual_duration
            or infer_duration_minutes(
                text=task.title,
                task_type=infer_task_type(task.title),
                energy_level=energy,
                historical_avg=get_historical_avg_duration(
                    db,
                    infer_task_type(task.title),
                    energy
                ),
                deadline_date=task.deadline,
            )
        )

        # Use the intelligent scheduler
        start_time, end_time = optimize_day_schedule(
            db=db,
            task_date=target_date,
            new_task_data={
                "title": task.title,
                "description": task.description,
                "priority": task.priority,
            },
            duration_min=duration_min,
        )

        if start_time and end_time:
            task.date = target_date
            task.start_time = start_time
            task.end_time = end_time
            db.commit()
            return True

    return False


def update_duration_model(db: Session, task: Task):
    """
    Update the duration learning model based on actual time taken
    versus estimated duration.
    """
    if not task.actual_duration or not task.start_time or not task.end_time:
        return

    # Calculate actual duration in minutes
    actual_minutes = (
        (task.end_time.hour * 60 + task.end_time.minute) -
        (task.start_time.hour * 60 + task.start_time.minute)
    )

    error = task.actual_duration - actual_minutes

    store_duration_delta(
        db,
        task_type=infer_task_type(task.title),
        energy=task.reflection_energy,
        delta=error
    )

def rebalance_day(db, task_date, flexible_tasks=None, start_after=None, pinned_task_ids=None):
    """
    Rebalance tasks for a day.

    - Tasks in `pinned_task_ids` keep their explicit start/end slots.
    - Other tasks are moved to the next available non-overlapping slot.
    - `flexible_tasks` and `start_after` are accepted for backward compatibility.
    """
    del flexible_tasks, start_after  # Backward-compat args; scheduling is id-driven.

    work_start = time(9, 0)
    work_end = time(21, 0)
    buffer_min = 5
    pinned_ids = set(pinned_task_ids or set())
    mood = db.query(Mood).filter(Mood.date == task_date).first()
    energy = mood.energy if mood else 3

    tasks = (
        db.query(Task)
        .filter(Task.date == task_date, Task.completed == False)
        .order_by(Task.priority.desc(), Task.id.asc())
        .all()
    )

    if not tasks:
        return

    def duration_minutes(task: Task) -> int:
        if task.duration_minutes and task.duration_minutes > 0:
            return int(task.duration_minutes)
        if task.start_time and task.end_time:
            start_dt = datetime.combine(task_date, task.start_time)
            end_dt = datetime.combine(task_date, task.end_time)
            return max(15, int((end_dt - start_dt).total_seconds() // 60))
        return 60

    occupied = []

    # Keep pinned tasks where they are and reserve their slots.
    for task in tasks:
        if task.id not in pinned_ids:
            continue
        if not task.start_time:
            continue

        start_dt = datetime.combine(task_date, task.start_time)
        if task.end_time:
            end_dt = datetime.combine(task_date, task.end_time)
        else:
            end_dt = start_dt + timedelta(minutes=duration_minutes(task))
            task.end_time = end_dt.time()

        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=duration_minutes(task))
            task.end_time = end_dt.time()

        occupied.append((start_dt, end_dt, task.id))

    occupied.sort(key=lambda x: x[0])

    movable_tasks = [t for t in tasks if t.id not in pinned_ids]

    # Always account for energy before scheduling slots for the day.
    if energy <= 2:
        cursor = datetime.combine(task_date, time(12, 0))
    else:
        cursor = datetime.combine(task_date, work_start)

    duration_cache = {t.id: duration_minutes(t) for t in movable_tasks}
    if energy <= 2:
        movable_tasks.sort(key=lambda t: (-t.priority, duration_cache[t.id], t.id))
    elif energy >= 4:
        movable_tasks.sort(key=lambda t: (-t.priority, -duration_cache[t.id], t.id))
    else:
        movable_tasks.sort(key=lambda t: (-t.priority, t.id))

    for task in movable_tasks:
        duration = duration_cache[task.id]
        day_end = datetime.combine(task_date, work_end)
        proposed_start = cursor

        while True:
            conflict = None
            proposed_end = proposed_start + timedelta(minutes=duration)

            if proposed_end > day_end:
                proposed_end = day_end
                proposed_start = max(
                    datetime.combine(task_date, work_start),
                    proposed_end - timedelta(minutes=duration),
                )

            for occ_start, occ_end, _ in occupied:
                if proposed_start < occ_end and proposed_end > occ_start:
                    conflict = (occ_start, occ_end)
                    break

            if not conflict:
                break

            proposed_start = conflict[1] + timedelta(minutes=buffer_min)

        task.start_time = proposed_start.time()
        task.end_time = proposed_end.time()

        occupied.append((proposed_start, proposed_end, task.id))
        occupied.sort(key=lambda x: x[0])
        cursor = proposed_end + timedelta(minutes=buffer_min)

    db.commit()
