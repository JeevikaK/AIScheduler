from datetime import date, datetime, time, timedelta
import re
from dateparser.search import search_dates
from requests import Session
from app.models import Task


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


def find_available_slot(task_date, energy, db, duration_min=60, deadline=None):
    existing_tasks = (
        db.query(Task)
        .filter(Task.date == task_date)
        .all()
    )

    if deadline:
        days_left = (deadline - task_date).days
        if days_left <= 1:
            current_start = WORK_START  # urgent → early
        else:
            current_start = time(9, 0)
    elif energy <= 2:
        current_start = time(16, 0)
    elif energy == 3:
        current_start = time(14, 0)
    else:
        current_start = time(9, 0)

    while current_start < WORK_END:
        start_dt = datetime.combine(task_date, current_start)
        end_dt = start_dt + timedelta(minutes=duration_min)

        if end_dt.time() > WORK_END:
            break

        if not has_conflict(
            start_dt.time(),
            end_dt.time(),
            existing_tasks
        ):
            return start_dt.time(), end_dt.time()

        # Move forward by 30 mins
        current_start = (start_dt + timedelta(minutes=30)).time()

    return None, None

def has_conflict(start, end, existing_tasks):
    for task in existing_tasks:
        if not task.start_time or not task.end_time:
            continue

        if start < task.end_time and end > task.start_time:
            return True
    return False

def infer_duration_minutes(text: str):
    text = text.lower()

    # Explicit durations
    hour_match = re.search(r"(\d+)\s*hour", text)
    if hour_match:
        return int(hour_match.group(1)) * 60

    minute_match = re.search(r"(\d+)\s*min", text)
    if minute_match:
        return int(minute_match.group(1))

    # Keyword-based heuristics
    if any(word in text for word in ["quick", "light"]):
        return 30

    if any(word in text for word in ["deep", "focus", "intense"]):
        return 120

    if any(word in text for word in ["study", "assignment", "project"]):
        return 90

    return 60