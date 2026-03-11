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

def rebalance_day(db, task_date, flexible_tasks=None, start_after=None, pinned_task_ids=None, latest_end_by_task_id=None):
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

    now_dt = datetime.now()
    day_start_dt = datetime.combine(task_date, work_start)
    day_end_dt = datetime.combine(task_date, work_end)
    earliest_start_dt = day_start_dt
    if task_date == now_dt.date():
        aligned_now = now_dt.replace(second=0, microsecond=0)
        rem = aligned_now.minute % buffer_min
        if rem:
            aligned_now += timedelta(minutes=(buffer_min - rem))
        earliest_start_dt = max(day_start_dt, aligned_now)

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
            "breakfast",
            "lunch",
            "dinner",
            "sleep",
            "bedtime",
            "go to bed",
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
    latest_pinned_end = day_end_dt
    pinned_rows = []
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

        pinned_rows.append((start_dt, end_dt, task))
        if end_dt > latest_pinned_end:
            latest_pinned_end = end_dt

    # Normalize pinned overlaps by shifting only non-hard pinned tasks forward.
    pinned_rows.sort(key=lambda x: x[0])
    normalized_pinned = []
    for start_dt, end_dt, task in pinned_rows:
        if not normalized_pinned:
            normalized_pinned.append((start_dt, end_dt, task))
            continue
        prev_start, prev_end, _prev_task = normalized_pinned[-1]
        if start_dt < prev_end:
            if not is_hard_commitment(task):
                dur = max(15, int((end_dt - start_dt).total_seconds() // 60))
                start_dt = prev_end + timedelta(minutes=buffer_min)
                end_dt = start_dt + timedelta(minutes=dur)
                task.start_time = start_dt.time()
                task.end_time = end_dt.time()
            # If both are hard commitments, preserve original hard slot and accept conflict;
            # follow-up conflict workflow should handle this explicitly.
        normalized_pinned.append((start_dt, end_dt, task))

    occupied = [(s, e, t.id) for s, e, t in normalized_pinned]
    occupied.sort(key=lambda x: x[0])
    # If user pinned a later task (e.g., "till 11pm"), keep scheduling horizon wide enough
    # so other same-day tasks are not unnecessarily spilled to the next day.
    latest_normalized_end = max((e for _s, e, _t in normalized_pinned), default=latest_pinned_end)
    day_end_dt = max(day_end_dt, latest_normalized_end)

    movable_tasks = [t for t in tasks if t.id not in pinned_ids]
    duration_cache = {t.id: duration_minutes(t) for t in movable_tasks}

    def find_slot_start(duration: int, initial_start: datetime, day_end: datetime):
        """
        Deterministic slot search across occupied intervals.
        Never loops indefinitely.
        """
        start = max(initial_start, earliest_start_dt)

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

    def has_overlap(start_dt: datetime, end_dt: datetime) -> bool:
        for occ_start, occ_end, _ in occupied:
            if start_dt < occ_end and end_dt > occ_start:
                return True
        return False

    def within_day_window(start_dt: datetime, end_dt: datetime, max_end: datetime | None = None) -> bool:
        upper = min(day_end_dt, max_end) if max_end else day_end_dt
        return start_dt >= earliest_start_dt and end_dt <= upper and end_dt > start_dt

    # Preserve already-valid flexible slots first; don't compact to fill empty time.
    timed_movable = [t for t in movable_tasks if t.start_time and t.end_time]
    untimed_movable = [t for t in movable_tasks if not (t.start_time and t.end_time)]
    timed_movable.sort(key=lambda t: (t.start_time, -t.priority, t.id))
    untimed_movable.sort(key=lambda t: (-t.priority, t.id))

    tasks_to_place = []
    for task in timed_movable:
        duration = duration_cache[task.id]
        start_dt = datetime.combine(task_date, task.start_time)
        end_dt = datetime.combine(task_date, task.end_time)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=duration)
        max_end = latest_end_by_task_id.get(task.id) if latest_end_by_task_id else None
        if within_day_window(start_dt, end_dt, max_end=max_end) and not has_overlap(start_dt, end_dt):
            occupied.append((start_dt, end_dt, task.id))
            occupied.sort(key=lambda x: x[0])
            continue
        tasks_to_place.append(task)
    tasks_to_place.extend(untimed_movable)

    for task in tasks_to_place:
        duration = duration_cache[task.id]
        day_end = day_end_dt
        if latest_end_by_task_id and task.id in latest_end_by_task_id:
            day_end = min(day_end_dt, latest_end_by_task_id[task.id])
        if task.start_time:
            preferred_start = datetime.combine(task_date, task.start_time)
        else:
            preferred_start = earliest_start_dt
            profile = task_profile(task)
            if profile == "cognitive" and energy >= 4:
                preferred_start = max(preferred_start, datetime.combine(task_date, time(9, 0)))
            elif profile == "recovery" and energy <= 2:
                preferred_start = max(preferred_start, datetime.combine(task_date, time(12, 0)))

        proposed_start = find_slot_start(duration, preferred_start, day_end)
        if proposed_start is None:
            # No valid slot on this day; keep unscheduled instead of spilling automatically.
            task.start_time = None
            task.end_time = None
            continue

        proposed_end = proposed_start + timedelta(minutes=duration)

        task.start_time = proposed_start.time()
        task.end_time = proposed_end.time()

        occupied.append((proposed_start, proposed_end, task.id))
        occupied.sort(key=lambda x: x[0])

    db.commit()
