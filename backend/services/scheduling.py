from datetime import date, datetime, time, timedelta
import re
from dateparser.search import search_dates
from requests import Session
from app.models import DurationStats, Task

WORK_START = time(9, 0)  # 9:00 AM
WORK_END = time(21, 0)   # 9:00 PM
DEFAULT_DURATION_MINUTES = 60  # default task duration

def find_time_slot(task_date: date, db: Session, duration_minutes: int = DEFAULT_DURATION_MINUTES):
    """
    Find the first available time slot for the task on task_date.
    Simple greedy algorithm: fills earliest free slot.
    """
    existing_tasks = db.query(Task).filter(Task.date == task_date).all()
    existing_tasks = sorted(existing_tasks, key=lambda t: t.start_time or WORK_START)

    # Start searching from WORK_START
    current_start = datetime.combine(task_date, WORK_START)
    work_end_dt = datetime.combine(task_date, WORK_END)
    duration_td = timedelta(minutes=duration_minutes)

    for task in existing_tasks:
        task_start = datetime.combine(task_date, task.start_time or WORK_START)
        task_end = datetime.combine(task_date, task.end_time) if task.end_time else task_start + duration_td
        if current_start + duration_td <= task_start:
            # Found a gap
            return current_start.time(), (current_start + duration_td).time()
        current_start = max(current_start, task_end)
        
        # If no gaps, place at the end of workday
    if current_start + duration_td <= work_end_dt:
        return current_start.time(), (current_start + duration_td).time()

    # If fully booked, just return WORK_START slot (overlap)
    return WORK_START, (datetime.combine(task_date, WORK_START) + duration_td).time()

def extract_date_from_text(text: str):
    result = search_dates(
        text,
        settings={
            'PREFER_DATES_FROM': 'future',
            'RELATIVE_BASE': datetime.now(), # Use current time for better relative accuracy
            'TIMEZONE': 'America/Los_Angeles',
            'RETURN_AS_TIMEZONE_AWARE': False
        }
    )

    if result:
        _, parsed_date = result[0]
        return parsed_date.date()

    return date.today()


from datetime import datetime, timedelta, time

def find_available_slot(task_date, energy, db, duration_min=60, deadline=None, priority=None):
    existing_tasks = db.query(Task).filter(Task.date == task_date).all()

    # Determine ideal starting point based on energy/deadline
    if deadline and (deadline - task_date).days <= 1:
        search_start = WORK_START  # Urgent: check entire day from start
    elif energy <= 2:
        search_start = time(16, 0) # Low energy: late start
    elif energy == 3:
        search_start = time(14, 0) # Medium energy: mid-day
    else:
        search_start = time(9, 0)  # High energy: morning

    current_time = datetime.combine(task_date, search_start)
    end_of_day = datetime.combine(task_date, WORK_END)

    while current_time + timedelta(minutes=duration_min) <= end_of_day:
        slot_start = current_time.time()
        slot_end = (current_time + timedelta(minutes=duration_min)).time()

        if not has_conflict(slot_start, slot_end, existing_tasks):
            return slot_start, slot_end

        # Move forward in 30-minute increments
        current_time += timedelta(minutes=30)
        
        # Safety: If we hit WORK_END and found nothing, 
        # consider a "fallback" to search the morning if we haven't already.
    
    return None, None

def has_conflict(start, end, existing_tasks):
    for task in existing_tasks:
        if not task.start_time or not task.end_time:
            continue

        if start < task.end_time and end > task.start_time:
            return True
    return False

def infer_duration_minutes(
    text: str,
    task_type: str | None = None,
    energy_level: int | None = None,   # 1–5
    historical_avg: int | None = None  # minutes from reflection loop
):
    text = text.lower()

    # -----------------------------
    # 1. Explicit durations (highest priority)
    # -----------------------------
    hours = re.findall(r"(\d+(?:\.\d+)?)\s*hour", text)
    minutes = re.findall(r"(\d+)\s*min", text)

    if hours:
        return int(float(hours[0]) * 60)

    if minutes:
        return int(minutes[0])

    # -----------------------------
    # 2. Deadline / urgency hints
    # -----------------------------
    if any(word in text for word in ["quiz", "exam", "test", "deadline", "tomorrow"]):
        base = 120
    else:
        base = 60

    # -----------------------------
    # 3. Task-type heuristics
    # -----------------------------
    task_keywords = {
        "study": 90,
        "revision": 60,
        "assignment": 120,
        "project": 150,
        "coding": 120,
        "reading": 45,
        "meeting": 30,
        "presentation": 90,
        "exercise": 45,
        "errand": 30
    }

    for key, duration in task_keywords.items():
        if key in text:
            base = duration
            break

    # Optional explicit task_type from NLP
    if task_type and task_type in task_keywords:
        base = task_keywords[task_type]

    # -----------------------------
    # 4. Intensity modifiers
    # -----------------------------
    if any(word in text for word in ["quick", "light", "skim"]):
        base -= 20

    if any(word in text for word in ["deep", "focus", "intense", "thorough"]):
        base += 30

    # -----------------------------
    # 5. Energy-based adjustment
    # -----------------------------
    if energy_level:
        if energy_level <= 2:
            base *= 1.25   # low energy → slower
        elif energy_level >= 4:
            base *= 0.85   # high energy → faster

    # -----------------------------
    # 6. Reflection-based override (MOST IMPORTANT long-term)
    # -----------------------------
    if historical_avg:
        # Weighted blend: learned behavior > heuristics
        base = int(0.7 * historical_avg + 0.3 * base)

    # -----------------------------
    # 7. Safety bounds
    # -----------------------------
    base = max(15, min(int(base), 240))  # 15 min – 4 hours

    return int(base)

def store_duration_delta(
    db: Session,
    task_type: str,
    energy: int,
    delta: int,
):
    stat = (
        db.query(DurationStats)
        .filter(
            DurationStats.task_type == task_type,
            DurationStats.energy == energy,
        )
        .first()
    )

    if not stat:
        stat = DurationStats(
            task_type=task_type,
            energy=energy,
            avg_delta=delta,
            count=1,
        )
        db.add(stat)
    else:
        # running average
        stat.avg_delta = int(
            (stat.avg_delta * stat.count + delta) / (stat.count + 1)
        )
        stat.count += 1

    db.commit()
