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

    def is_hard_commitment(task: Task) -> bool:
        """
        Hard commitments should not be moved during rebalance.
        """
        text = f"{(task.title or '').lower()} {(task.description or '').lower()}"
        hard_keywords = [
            "class",
            "lecture",
            "meeting",
            "appointment",
            "interview",
            "party",
            "event",
            "wedding",
            "doctor",
            "hospital",
            "flight",
            "train",
            "travel",
            "bus",
            "exam",
            "quiz",
            "test",
        ]
        return any(k in text for k in hard_keywords)

    # Always pin known hard commitments with explicit slots.
    auto_pinned_ids = {
        t.id
        for t in tasks
        if t.start_time and t.end_time and is_hard_commitment(t)
    }
    pinned_ids.update(auto_pinned_ids)

    def duration_minutes(task: Task) -> int:
        if task.duration_minutes and task.duration_minutes > 0:
            return int(task.duration_minutes)
        if task.start_time and task.end_time:
            start_dt = datetime.combine(task_date, task.start_time)
            end_dt = datetime.combine(task_date, task.end_time)
            return max(15, int((end_dt - start_dt).total_seconds() // 60))
        return 60

    def task_profile(task: Task) -> str:
        text = f"{(task.title or '').lower()} {(task.description or '').lower()}"
        if any(k in text for k in ["leetcode", "coding", "study", "revision", "read", "learn", "exam", "project"]):
            return "cognitive"
        if any(k in text for k in ["walk", "workout", "gym", "exercise"]):
            return "physical"
        if any(k in text for k in ["relax", "rest", "break", "meditate", "nap", "talk to", "call"]):
            return "recovery"
        return "admin"

    def break_after(task: Task, duration: int) -> int:
        br = 5
        if duration >= 120:
            br = 15
        elif duration >= 90:
            br = 10
        if energy <= 2:
            br += 5
        if task_profile(task) == "cognitive" and duration >= 60:
            br += 5
        return br

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
    profile_cache = {t.id: task_profile(t) for t in movable_tasks}
    if energy <= 2:
        profile_rank = {"recovery": 0, "admin": 1, "physical": 2, "cognitive": 3}
        movable_tasks.sort(
            key=lambda t: (
                -t.priority,
                profile_rank.get(profile_cache[t.id], 1),
                duration_cache[t.id],
                t.id,
            )
        )
    elif energy >= 4:
        profile_rank = {"cognitive": 0, "physical": 1, "admin": 2, "recovery": 3}
        movable_tasks.sort(
            key=lambda t: (
                -t.priority,
                profile_rank.get(profile_cache[t.id], 2),
                -duration_cache[t.id],
                t.id,
            )
        )
    else:
        profile_rank = {"cognitive": 0, "admin": 1, "physical": 2, "recovery": 3}
        movable_tasks.sort(
            key=lambda t: (
                -t.priority,
                profile_rank.get(profile_cache[t.id], 1),
                -duration_cache[t.id] if profile_cache[t.id] == "cognitive" else duration_cache[t.id],
                t.id,
            )
        )

    def find_slot_start(duration: int, initial_start: datetime, day_end: datetime):
        """
        Deterministic slot search across occupied intervals.
        Never loops indefinitely.
        """
        start = max(initial_start, datetime.combine(task_date, work_start))

        for occ_start, occ_end, _ in occupied:
            candidate_end = start + timedelta(minutes=duration)
            if candidate_end <= occ_start:
                return start
            if start < occ_end:
                start = occ_end + timedelta(minutes=buffer_min)
            if start >= day_end:
                break

        if start + timedelta(minutes=duration) <= day_end:
            return start

        # Day is full for this duration.
        return None

    for task in movable_tasks:
        duration = duration_cache[task.id]
        day_end = datetime.combine(task_date, work_end)
        proposed_start = find_slot_start(duration, cursor, day_end)
        if proposed_start is None:
            # Spill to next available day/slot instead of forcing overlap on this day.
            moved = False
            for offset in range(1, 8):
                target_date = task_date + timedelta(days=offset)
                start_time, end_time = optimize_day_schedule(
                    db=db,
                    task_date=target_date,
                    new_task_data={
                        "title": task.title,
                        "description": task.description,
                        "priority": task.priority,
                    },
                    duration_min=duration,
                )
                if start_time and end_time:
                    task.date = target_date
                    task.start_time = start_time
                    task.end_time = end_time
                    moved = True
                    break

            if not moved:
                # If no valid slot found in horizon, keep task unscheduled.
                task.start_time = None
                task.end_time = None
            continue

        proposed_end = proposed_start + timedelta(minutes=duration)

        task.start_time = proposed_start.time()
        task.end_time = proposed_end.time()

        occupied.append((proposed_start, proposed_end, task.id))
        occupied.sort(key=lambda x: x[0])
        cursor = proposed_end + timedelta(minutes=break_after(task, duration))

    db.commit()
