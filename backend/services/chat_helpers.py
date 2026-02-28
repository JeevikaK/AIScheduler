from datetime import date, datetime, time, timedelta
from typing import List
import re

from sqlalchemy.orm import Session

from app.models import Task
from services.planner import infer_task_type, rebalance_day
from services.scheduling import extract_date_from_text
from services.tools import infer_priority

STOP_WORDS = {
    "the", "a", "an", "to", "for", "and", "or", "is", "at", "on", "in", "of", "my",
    "our", "your", "with", "from", "by", "it", "this", "that", "please", "plan", "day",
    "move", "moved", "earlier", "later", "before", "after", "around", "about",
}


def _has_explicit_date_cue(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(
            r"\b(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"next\s+\w+|on\s+\d{1,2}(?:st|nd|rd|th)?|"
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _parse_clock_token(token: str, prefer_pm: bool = False) -> time | None:
    if not token:
        return None
    token = token.strip().lower()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", token)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridian = m.group(3)

    if meridian:
        if meridian == "pm" and hour != 12:
            hour += 12
        elif meridian == "am" and hour == 12:
            hour = 0
    else:
        if prefer_pm and 1 <= hour <= 11:
            hour += 12

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour, minute)
    return None


def _extract_time_range(segment: str):
    if not segment:
        return None, None
    text = segment.lower()

    range_match = re.search(
        r"\bfrom\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:to|-)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not range_match:
        range_match = re.search(
            r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:to|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            text,
            flags=re.IGNORECASE,
        )
    if not range_match:
        return None, None

    start_raw = range_match.group(1).strip()
    end_raw = range_match.group(2).strip()
    explicit_meridian = ("am" in start_raw or "pm" in start_raw or "am" in end_raw or "pm" in end_raw)

    prefer_pm = False
    if not explicit_meridian:
        if any(k in text for k in ["class", "meeting", "evening", "night", "pm"]):
            prefer_pm = True

    start_t = _parse_clock_token(start_raw, prefer_pm=prefer_pm)
    end_t = _parse_clock_token(end_raw, prefer_pm=prefer_pm)
    if not start_t or not end_t:
        return None, None

    # If ambiguous and end <= start, push end to later by assuming same day progression.
    if datetime.combine(date.today(), end_t) <= datetime.combine(date.today(), start_t):
        end_dt = datetime.combine(date.today(), end_t) + timedelta(hours=12)
        end_t = end_dt.time()
    return start_t, end_t


def title_from_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip(" .,\n\t"))
    if not clean:
        return "Untitled task"
    words = clean.split(" ")
    short = " ".join(words[:8])
    return short[:1].upper() + short[1:]


def description_from_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return "Task generated from user request."
    return clean if clean.endswith((".", "!", "?")) else f"{clean}."


def split_tasks_from_message(message: str):
    if not message or not message.strip():
        return []

    parts = re.split(
        r"(?:\n|;|,|\band then\b|\bthen\b|\bplus\b|\balso\b|\s+and\s+)",
        message,
        flags=re.IGNORECASE,
    )
    candidates = [p.strip(" .\t") for p in parts if p and p.strip(" .\t")]

    if len(candidates) < 2:
        return []

    tasks = []
    default_date = date.today()
    for segment in candidates:
        start_t, end_t = _extract_time_range(segment)
        if _has_explicit_date_cue(segment):
            task_date = extract_date_from_text(segment)
        else:
            task_date = default_date

        duration_min = None
        if start_t and end_t:
            duration_min = int(
                (
                    datetime.combine(task_date, end_t) -
                    datetime.combine(task_date, start_t)
                ).total_seconds() // 60
            )
            if duration_min <= 0:
                duration_min = 60

        tasks.append(
            {
                "title": title_from_text(segment),
                "description": description_from_text(segment),
                "date": task_date.isoformat(),
                "start_time": start_t.strftime("%H:%M:%S") if start_t else None,
                "end_time": end_t.strftime("%H:%M:%S") if end_t else None,
                "priority": infer_priority(segment),
                "task_type": infer_task_type(segment),
                "total_effort_minutes": duration_min,
                "deadline": None,
                "spread": False,
            }
        )
    return tasks


def extract_time_from_text(text: str):
    if not text:
        return None
    lower = text.lower()

    ampm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, flags=re.IGNORECASE)
    if ampm:
        hour = int(ampm.group(1))
        minute = int(ampm.group(2) or 0)
        meridian = ampm.group(3).lower()
        if meridian == "pm" and hour != 12:
            hour += 12
        if meridian == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)

    hhmm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if hhmm:
        return time(int(hhmm.group(1)), int(hhmm.group(2)))

    # Bare-hour phrases like "at 7" or "by 9".
    bare = re.search(r"\b(?:at|by|around)\s+(\d{1,2})\b", lower)
    if bare:
        hour = int(bare.group(1))
        if 1 <= hour <= 12:
            # Heuristic: if no AM/PM, prefer PM for common evening/event cues.
            prefer_pm = any(
                k in lower
                for k in ["party", "dinner", "event", "class", "meeting", "evening", "night"]
            ) or (1 <= hour <= 7)
            if "morning" in lower:
                prefer_pm = False
            if "afternoon" in lower:
                prefer_pm = hour != 12

            if prefer_pm and hour != 12:
                hour += 12
            elif not prefer_pm and hour == 12:
                hour = 0
            return time(hour, 0)

    return None


def task_duration_minutes(task: Task) -> int:
    if task.duration_minutes and task.duration_minutes > 0:
        return int(task.duration_minutes)
    if task.start_time and task.end_time:
        return max(
            15,
            int(
                (
                    datetime.combine(task.date, task.end_time)
                    - datetime.combine(task.date, task.start_time)
                ).total_seconds()
                // 60
            ),
        )
    return 60


def build_schedule_context(db: Session, today: date, days: int = 7) -> str:
    end_date = today + timedelta(days=days)
    tasks = (
        db.query(Task)
        .filter(Task.date >= today, Task.date <= end_date, Task.completed == False)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.priority.desc())
        .limit(30)
        .all()
    )
    if not tasks:
        return "No upcoming tasks."

    lines = []
    for t in tasks:
        st = t.start_time.strftime("%H:%M") if t.start_time else "unscheduled"
        et = t.end_time.strftime("%H:%M") if t.end_time else "unscheduled"
        lines.append(f"- {t.date.isoformat()} | {st}-{et} | {t.title}")
    return "\n".join(lines)


def handle_followup_replan(message: str, db: Session) -> List[Task]:
    today = date.today()
    text = (message or "").lower()
    if not text.strip():
        return []
    requested_time = extract_time_from_text(message)

    upcoming = (
        db.query(Task)
        .filter(Task.date >= today, Task.completed == False)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.priority.desc())
        .all()
    )
    if not upcoming:
        return []

    def _tokenize(s: str) -> set[str]:
        return {
            w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) > 1 and w not in STOP_WORDS
        }

    def _task_tokens(task: Task) -> set[str]:
        return _tokenize(f"{task.title or ''} {task.description or ''}")

    def _score_task(task: Task, query_tokens: set[str]) -> float:
        if not query_tokens:
            return 0.0
        tt = _task_tokens(task)
        overlap = query_tokens.intersection(tt)
        if not overlap:
            return 0.0
        score = float(len(overlap) * 2) + (len(overlap) / max(1, len(query_tokens)))
        if task.start_time:
            score += 0.2
        if "session" in tt and "session" not in query_tokens:
            score -= 0.25
        return score

    def _pick_best_task(tasks: List[Task], query_tokens: set[str]):
        scored = []
        for t in tasks:
            s = _score_task(t, query_tokens)
            if s > 0:
                scored.append((s, t))
        if not scored:
            return None
        scored.sort(
            key=lambda x: (
                -x[0],
                x[1].date,
                x[1].start_time or time(23, 59),
                -x[1].priority,
                x[1].id,
            )
        )
        return scored[0][1]

    def _extract_change_to_time(raw_text: str, anchor_task: Task | None):
        """
        Parse phrases like "change that to 12" or "move it to 14:30".
        """
        if not raw_text:
            return None
        m = re.search(
            r"\b(?:change|move|set|shift)\b.*?\bto\b\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
            raw_text,
            flags=re.IGNORECASE,
        )
        if not m:
            return None

        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridian = (m.group(3) or "").lower() or None

        if not (0 <= minute <= 59):
            return None

        if meridian:
            if meridian == "pm" and hour != 12:
                hour += 12
            elif meridian == "am" and hour == 12:
                hour = 0
        else:
            # Inherit AM/PM tendency from anchor if possible.
            if anchor_task and anchor_task.start_time:
                if anchor_task.start_time.hour >= 12 and 1 <= hour <= 11:
                    hour += 12
            else:
                # Event-like cues default to evening when ambiguous.
                if any(k in raw_text.lower() for k in ["party", "dinner", "event", "class", "meeting"]) and 1 <= hour <= 11:
                    hour += 12

        if 0 <= hour <= 23:
            return time(hour, minute)
        return None

    all_tokens = _tokenize(text)
    anchor = _pick_best_task(upcoming, all_tokens)
    pronoun_ref = bool(re.search(r"\b(it|that|this|one)\b", text))

    if not anchor and pronoun_ref:
        # Immediate-context fallback: treat pronouns as most recently created/upated task.
        anchor = max(upcoming, key=lambda t: t.id)

    if not requested_time:
        requested_time = _extract_change_to_time(message, anchor)

    if not anchor:
        if requested_time:
            # If only a time is given, use most recent scheduled task as likely referent.
            scheduled = [t for t in upcoming if t.start_time]
            anchor = max(scheduled, key=lambda t: t.id) if scheduled else max(upcoming, key=lambda t: t.id)
        else:
            return []

    direction = None
    if any(k in text for k in ["earlier", "before", "ahead"]):
        direction = "earlier"
    elif any(k in text for k in ["later", "after", "postpone", "push"]):
        direction = "later"

    move_target_tokens = set()
    move_match = re.search(
        r"\bmove\s+(?:the\s+)?(.+?)\s+(earlier|later|before|after)\b",
        text,
        flags=re.IGNORECASE,
    )
    if move_match:
        move_target_tokens = _tokenize(move_match.group(1))

    target_date = anchor.date
    # Snapshot day state so we can return only changed tasks.
    before_day_tasks = (
        db.query(Task)
        .filter(Task.date == target_date, Task.completed == False)
        .all()
    )
    before_state = {t.id: (t.date, t.start_time, t.end_time) for t in before_day_tasks}

    pinned_ids = set()

    # Absolute time update for the anchor task.
    if requested_time:
        duration = task_duration_minutes(anchor)
        anchor.start_time = requested_time
        anchor.end_time = (
            datetime.combine(anchor.date, requested_time) + timedelta(minutes=duration)
        ).time()
        pinned_ids.add(anchor.id)

    # Relative move request for an explicitly mentioned task phrase.
    if direction and move_target_tokens:
        same_day = (
            db.query(Task)
            .filter(
                Task.date == target_date,
                Task.completed == False,
                Task.id != anchor.id,
            )
            .order_by(Task.priority.desc(), Task.start_time.asc(), Task.id.asc())
            .all()
        )
        related = _pick_best_task(same_day, move_target_tokens)
        if related:
            rel_duration = task_duration_minutes(related)
            buffer_min = 5
            day_start = datetime.combine(target_date, time(9, 0))
            day_end = datetime.combine(target_date, time(21, 0))

            anchor_start = anchor.start_time or requested_time or time(12, 0)
            anchor_end = anchor.end_time or (
                datetime.combine(target_date, anchor_start) + timedelta(minutes=task_duration_minutes(anchor))
            ).time()

            if direction == "earlier":
                end_dt = datetime.combine(target_date, anchor_start) - timedelta(minutes=buffer_min)
                start_dt = end_dt - timedelta(minutes=rel_duration)
            else:
                start_dt = datetime.combine(target_date, anchor_end) + timedelta(minutes=buffer_min)
                end_dt = start_dt + timedelta(minutes=rel_duration)

            if start_dt < day_start:
                start_dt = day_start
                end_dt = start_dt + timedelta(minutes=rel_duration)
            if end_dt > day_end:
                end_dt = day_end
                start_dt = end_dt - timedelta(minutes=rel_duration)
                if start_dt < day_start:
                    start_dt = day_start

            related.start_time = start_dt.time()
            related.end_time = end_dt.time()
            pinned_ids.add(related.id)

    # Generic "move earlier/later" without explicit phrase: shift anchor by 30 mins.
    elif direction and not requested_time and anchor.start_time:
        shift_min = -30 if direction == "earlier" else 30
        duration = task_duration_minutes(anchor)
        day_start = datetime.combine(target_date, time(9, 0))
        day_end = datetime.combine(target_date, time(21, 0))
        shifted_start = datetime.combine(target_date, anchor.start_time) + timedelta(minutes=shift_min)
        shifted_start = max(day_start, min(shifted_start, day_end - timedelta(minutes=duration)))
        shifted_end = shifted_start + timedelta(minutes=duration)
        anchor.start_time = shifted_start.time()
        anchor.end_time = shifted_end.time()
        pinned_ids.add(anchor.id)

    db.commit()

    # Rebalance around pinned tasks.
    rebalance_day(db, target_date, pinned_task_ids=pinned_ids)

    refreshed = (
        db.query(Task)
        .filter(Task.date == target_date, Task.completed == False)
        .order_by(Task.start_time.asc(), Task.priority.desc())
        .all()
    )
    changed = []
    for task in refreshed:
        after = (task.date, task.start_time, task.end_time)
        before = before_state.get(task.id)
        if before != after:
            changed.append(task)

    return changed
