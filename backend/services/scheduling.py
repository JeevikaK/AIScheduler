from datetime import date, datetime, time, timedelta
import re
from typing import List, Tuple
from sqlalchemy.orm import Session
from app.models import DurationStats, Task

WORK_START = time(9, 0)  # 9:00 AM
WORK_END = time(23, 0)   # 11:00 PM
DEFAULT_DURATION_MINUTES = 60  # default task duration
TASK_BUFFER_MIN = 5

DEFAULT_DURATION_BY_TYPE = {
    "fitness": 60,
    "assessment": 120,
    "study": 90,
    "meeting": 30,
    "general": 60,
}


def optimize_day_schedule(
    db: Session,
    task_date: date,
    new_task_data: dict,
    duration_min: int,
) -> Tuple[time, time]:
    """
    Schedule a new task intelligently within the day, using existing tasks,
    energy levels, priorities, and fixed events.
    
    Returns start_time and end_time for the new task.
    """
    from app.models import Task, Mood

    # Fetch all existing tasks for the day
    existing_tasks: List[Task] = db.query(Task).filter(Task.date == task_date).all()
    
    # Fetch mood/energy for the day
    mood = db.query(Mood).filter(Mood.date == task_date).first()
    energy_level = mood.energy if mood else 3

    # Create the new task object
    new_task = Task(
        title=new_task_data.get("title"),
        description=new_task_data.get("description"),
        priority=new_task_data.get("priority", 1),
        start_time=None,
        end_time=None,
    )

    all_tasks = existing_tasks + [new_task]
    
    # Convert all tasks to dict with actual duration
    task_list = []
    for t in all_tasks:
        if t.start_time and t.end_time:
            start_dt = datetime.combine(task_date, t.start_time)
            end_dt = datetime.combine(task_date, t.end_time)
            duration = int((end_dt - start_dt).total_seconds() / 60)
        else:
            # fallback: use known duration_minutes or input duration for the new task
            duration = int(getattr(t, "duration_minutes", 0) or duration_min or 60)
        task_list.append({
            "task": t,
            "duration": duration,
            "fixed": bool(t.start_time),  # cannot move if fixed
            "priority": getattr(t, "priority", 1)
        })

    # Fixed tasks should be respected first, then higher priority.
    task_list.sort(key=lambda x: (not x["fixed"], -x["priority"]))
    
    # Assign slots sequentially
    current_dt = datetime.combine(task_date, WORK_START)
    for item in task_list:
        task = item["task"]
        duration = item["duration"]

        if item["fixed"]:
            # Already has start/end, move current_dt forward if needed
            task_start = datetime.combine(task_date, task.start_time)
            task_end = datetime.combine(task_date, task.end_time)
            if current_dt > task_start:
                # overlap: shift start after current_dt
                task_start = current_dt
                task_end = task_start + timedelta(minutes=duration)
        else:
            # Schedule based on energy: prefer high-energy first half day
            if energy_level >= 4:
                # high energy: schedule in morning if possible
                task_start = current_dt
            elif energy_level <= 2:
                # low energy: schedule in later slot
                if current_dt.time() < time(12, 0):
                    task_start = datetime.combine(task_date, time(12, 0))
                else:
                    task_start = current_dt
            else:
                task_start = current_dt

            task_end = task_start + timedelta(minutes=duration)
            # Ensure task_end does not exceed workday
            if task_end.time() > WORK_END:
                task_end = datetime.combine(task_date, WORK_END)

        # Assign times
        task.start_time = task_start.time()
        task.end_time = task_end.time()

        # Move current_dt forward
        current_dt = task_end + timedelta(minutes=TASK_BUFFER_MIN)

    # Return new task times
    return new_task.start_time, new_task.end_time

def extract_date_from_text(text: str):
    lower = (text or "").lower().strip()
    today = date.today()

    # Deterministic handling for common relative cues.
    if "day after tomorrow" in lower:
        return today + timedelta(days=2)
    if "tomorrow" in lower:
        return today + timedelta(days=1)
    if "today" in lower or "tonight" in lower:
        return today

    weekday_map = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    for weekday, idx in weekday_map.items():
        if re.search(rf"\bnext\s+{weekday}\b", lower):
            days_ahead = (idx - today.weekday() + 7) % 7 or 7
            return today + timedelta(days=days_ahead)
        if re.search(rf"\bthis\s+{weekday}\b", lower) or re.search(rf"\b{weekday}\b", lower):
            days_ahead = (idx - today.weekday() + 7) % 7
            return today + timedelta(days=days_ahead)

    iso_match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lower)
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            pass

    slash_match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lower)
    if slash_match:
        month = int(slash_match.group(1))
        day = int(slash_match.group(2))
        year_raw = slash_match.group(3)
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        else:
            year = today.year
        try:
            candidate = date(year, month, day)
            if candidate < today and not year_raw:
                candidate = date(year + 1, month, day)
            return candidate
        except ValueError:
            pass

    month_names = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    for prefix, month_num in month_names.items():
        m = re.search(rf"\b{prefix}[a-z]*\.?\s+(\d{{1,2}})(?:,?\s*(\d{{4}}))?\b", lower)
        if not m:
            continue
        day = int(m.group(1))
        year = int(m.group(2) or today.year)
        try:
            candidate = date(year, month_num, day)
            if candidate < today and not m.group(2):
                candidate = date(year + 1, month_num, day)
            return candidate
        except ValueError:
            continue

    return today


def has_conflict(start, end, existing_tasks):
    for task in existing_tasks:
        if not task.start_time or not task.end_time:
            continue

        if end == task.start_time or start == task.end_time:
            continue
        if start < task.end_time and end > task.start_time:
            return True
    return False

def infer_duration_minutes(
    text: str,
    task_type: str | None = None,
    energy_level: int | None = None,   # 1–5
    historical_avg: int | None = None,  # minutes from reflection loop
    deadline_date: date | None = None
):
    text = text.lower()
    word_to_num = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
    }

    # -----------------------------
    # 1. Explicit durations (highest priority)
    # -----------------------------
    hours = re.findall(r"(\d+(?:\.\d+)?)\s*(?:hour|hours|hr|hrs)\b", text)
    minutes = re.findall(r"(\d+)\s*(?:min|mins|minute|minutes)\b", text)
    word_hours = re.findall(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:hour|hours|hr|hrs)\b",
        text,
    )

    if hours:
        return int(float(hours[0]) * 60)
    if word_hours:
        return int(word_to_num[word_hours[0]] * 60)

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
    # 4.5 Deadline proximity scaling
    # -----------------------------
    if deadline_date:
        today = date.today()
        days_remaining = (deadline_date - today).days

        if days_remaining < 0:
            # overdue → high urgency
            base *= 1.5

        elif days_remaining == 0:
            # due today
            base *= 1.4

        elif days_remaining == 1:
            base *= 1.3

        elif days_remaining <= 3:
            base *= 1.2

        elif days_remaining <= 7:
            base *= 1.1

    # -----------------------------
    # 5. Energy-based adjustment
    # -----------------------------
    if energy_level is not None:
        cognitive_tasks = [
            "study", "revision", "assignment", "project",
            "coding", "reading", "presentation"
        ]

        physical_tasks = [
            "exercise", "gym", "workout"
        ]

        admin_tasks = [
            "meeting", "errand"
        ]

        is_cognitive = any(word in text for word in cognitive_tasks)
        is_physical = any(word in text for word in physical_tasks)
        is_admin = any(word in text for word in admin_tasks)

        # Low energy
        if energy_level <= 2:
            if is_cognitive:
                base *= 1.35   # brain slower
            elif is_physical:
                base *= 1.15   # still harder physically
            else:
                base *= 1.20

        # Medium-high energy
        elif energy_level == 3:
            pass  # no change

        # High energy
        elif energy_level >= 4:
            if is_cognitive:
                base *= 0.80   # deep focus compresses
            elif is_physical:
                base *= 0.90
            else:
                base *= 0.95

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

def parse_time(time_str: str) -> time:
    """
    Parse common time formats:
    - 24h: HH:MM, HH:MM:SS
    - 12h: hAM/PM, h:MM AM/PM (case-insensitive, optional space)
    """
    raw = (time_str or "").strip()
    normalized = re.sub(r"\s+", " ", raw).lower()
    # Normalize "6pm" -> "6 pm", "6:30pm" -> "6:30 pm"
    normalized = re.sub(r"^(\d{1,2}(?::\d{2})?)\s*(am|pm)$", r"\1 \2", normalized)

    fmts = [
        "%H:%M:%S",
        "%H:%M",
        "%I:%M %p",
        "%I %p",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(normalized, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unsupported time format: {time_str!r}")
