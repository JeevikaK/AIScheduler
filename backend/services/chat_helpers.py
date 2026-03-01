from datetime import date, datetime, time, timedelta
from typing import Any, Callable, List
import re
import json
import uuid

from sqlalchemy.orm import Session

from app.models import ConversationThread, Mood, Task
from app.schemas import (
    ChatMeta,
    ChatResponse,
    ClarificationOption,
    ClarificationPrompt,
    ConflictPrompt,
    ConflictTaskRef,
    SlotSuggestion,
)
from services.planner import get_historical_avg_duration, infer_task_type, rebalance_day
from services.scheduling import extract_date_from_text, infer_duration_minutes, parse_time
from services.tools import create_task, get_schedule, infer_priority

STOP_WORDS = {
    "the", "a", "an", "to", "for", "and", "or", "is", "at", "on", "in", "of", "my",
    "our", "your", "with", "from", "by", "it", "this", "that", "please", "plan", "day",
    "move", "moved", "earlier", "later", "before", "after", "around", "about",
}

def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            out = []
            for item in data:
                try:
                    out.append(int(item))
                except (TypeError, ValueError):
                    continue
            return out
    except Exception:
        return []
    return []


def _dump_ids(ids: list[int]) -> str:
    return json.dumps([int(i) for i in ids])


def _parse_json_obj(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dump_json_obj(obj: dict | None) -> str | None:
    if not obj:
        return None
    return json.dumps(obj)


def resolve_thread_key(chat_thread_id: str | None, thread_date: date) -> str:
    base = (chat_thread_id or "").strip()
    if base:
        return f"{base}:{thread_date.isoformat()}"
    return f"auto:{thread_date.isoformat()}"


def load_thread_state(db: Session, thread_key: str) -> dict:
    rec = db.query(ConversationThread).filter(ConversationThread.thread_key == thread_key).first()
    if not rec:
        return {
            "thread_key": thread_key,
            "chat_thread_id": None,
            "thread_date": None,
            "last_intent_type": None,
            "last_user_message": None,
            "last_created_task_ids": [],
            "last_updated_task_ids": [],
            "last_referenced_task_ids": [],
            "pending_intent_id": None,
            "pending_state_type": None,
            "pending_state": {},
        }
    return {
        "thread_key": rec.thread_key,
        "chat_thread_id": rec.chat_thread_id,
        "thread_date": rec.thread_date,
        "last_intent_type": rec.last_intent_type,
        "last_user_message": rec.last_user_message,
        "last_created_task_ids": _parse_ids(rec.last_created_task_ids),
        "last_updated_task_ids": _parse_ids(rec.last_updated_task_ids),
        "last_referenced_task_ids": _parse_ids(rec.last_referenced_task_ids),
        "pending_intent_id": rec.pending_intent_id,
        "pending_state_type": rec.pending_state_type,
        "pending_state": _parse_json_obj(rec.pending_state_json),
    }


def save_thread_state(db: Session, thread_key: str, payload: dict) -> None:
    rec = db.query(ConversationThread).filter(ConversationThread.thread_key == thread_key).first()
    if not rec:
        rec = ConversationThread(
            thread_key=thread_key,
            thread_date=payload.get("thread_date") or date.today(),
            chat_thread_id=payload.get("chat_thread_id"),
        )
        db.add(rec)

    if payload.get("thread_date"):
        rec.thread_date = payload["thread_date"]
    rec.chat_thread_id = payload.get("chat_thread_id")
    rec.last_intent_type = payload.get("last_intent_type")
    rec.last_user_message = payload.get("last_user_message")
    rec.last_created_task_ids = _dump_ids(payload.get("last_created_task_ids", []))
    rec.last_updated_task_ids = _dump_ids(payload.get("last_updated_task_ids", []))
    rec.last_referenced_task_ids = _dump_ids(payload.get("last_referenced_task_ids", []))
    rec.pending_intent_id = payload.get("pending_intent_id")
    rec.pending_state_type = payload.get("pending_state_type")
    rec.pending_state_json = _dump_json_obj(payload.get("pending_state"))
    rec.updated_at = datetime.utcnow()
    db.commit()


def _thread_summary_for_prompt(db: Session, thread_key: str, thread_state: dict) -> str:
    def _titles_for(ids: list[int]) -> str:
        if not ids:
            return "none"
        limited = ids[:3]
        tasks = db.query(Task).filter(Task.id.in_(limited)).all()
        title_map = {t.id: t.title for t in tasks}
        parts = []
        for i in limited:
            title = title_map.get(i, "missing")
            parts.append(f"{i}:{title}")
        return ", ".join(parts) if parts else "none"

    return (
        f"Thread key: {thread_key}\n"
        f"Last intent: {thread_state.get('last_intent_type') or 'none'}\n"
        f"Last created: {_titles_for(thread_state.get('last_created_task_ids', []))}\n"
        f"Last updated: {_titles_for(thread_state.get('last_updated_task_ids', []))}\n"
        f"Last referenced: {_titles_for(thread_state.get('last_referenced_task_ids', []))}"
    )


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


def _is_hard_commitment(task: Task) -> bool:
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
        "mandatory",
    ]
    return any(k in text for k in hard_keywords)


def _slot_overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and end_a > start_b


def _task_datetime_range(task: Task) -> tuple[datetime | None, datetime | None]:
    if not task.start_time:
        return None, None
    start_dt = datetime.combine(task.date, task.start_time)
    if task.end_time:
        end_dt = datetime.combine(task.date, task.end_time)
    else:
        end_dt = start_dt + timedelta(minutes=task_duration_minutes(task))
    return start_dt, end_dt


def _profile_from_text(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["leetcode", "coding", "study", "revision", "read", "learn", "project"]):
        return "cognitive"
    if any(k in t for k in ["walk", "workout", "gym", "exercise"]):
        return "physical"
    if any(k in t for k in ["relax", "rest", "break", "meditate", "nap", "call", "talk"]):
        return "recovery"
    return "admin"


def _score_slot(
    slot_start: datetime,
    slot_end: datetime,
    occupied: list[tuple[datetime, datetime]],
    energy: int,
    profile: str,
) -> tuple[float, str]:
    # Distance from nearest busy interval (prefer less fragmentation)
    min_gap = 9999
    for occ_s, occ_e in occupied:
        gap = min(abs(int((slot_start - occ_e).total_seconds() // 60)), abs(int((occ_s - slot_end).total_seconds() // 60)))
        min_gap = min(min_gap, gap)
    if min_gap == 9999:
        min_gap = 120

    hour = slot_start.time().hour
    energy_bonus = 0.0
    reason = "Balanced slot."
    if profile == "cognitive":
        if energy >= 4 and 9 <= hour <= 14:
            energy_bonus = 1.2
            reason = "High-energy hours suit focused work."
        elif energy <= 2 and hour >= 15:
            energy_bonus = 0.8
            reason = "Later slot fits lower-energy pacing."
    elif profile == "recovery":
        if energy <= 2 and hour >= 12:
            energy_bonus = 1.1
            reason = "Recovery task placed in low-energy friendly period."
        elif energy >= 4 and hour >= 18:
            energy_bonus = 0.9
            reason = "Evening slot keeps peak hours free."
    else:
        if 10 <= hour <= 18:
            energy_bonus = 0.7
            reason = "Midday slot fits general tasks."

    fit_bonus = min(1.0, max(0.0, min_gap / 60.0))
    score = round(energy_bonus + fit_bonus, 3)
    return score, reason


def suggest_alternative_slots(
    db: Session,
    task_date: date,
    duration_min: int,
    task_text: str,
    limit: int = 3,
) -> list[dict]:
    day_start = datetime.combine(task_date, time(9, 0))
    day_end = datetime.combine(task_date, time(21, 0))
    now = datetime.now()
    today = now.date()
    if task_date < today:
        return []
    if task_date == today:
        # Start from the next 15-minute boundary so we only suggest future times.
        minute_bucket = ((now.minute // 15) + 1) * 15
        aligned_now = now.replace(second=0, microsecond=0)
        if minute_bucket >= 60:
            aligned_now = aligned_now.replace(minute=0) + timedelta(hours=1)
        else:
            aligned_now = aligned_now.replace(minute=minute_bucket)
        day_start = max(day_start, aligned_now)
    mood = db.query(Mood).filter(Mood.date == task_date).first()
    energy = mood.energy if mood else 3

    day_tasks = (
        db.query(Task)
        .filter(Task.date == task_date, Task.completed == False)
        .all()
    )
    occupied = []
    for t in day_tasks:
        s, e = _task_datetime_range(t)
        if s and e:
            occupied.append((s, e))

    profile = _profile_from_text(task_text)
    suggestions = []
    step = timedelta(minutes=15)
    cur = day_start
    while cur + timedelta(minutes=duration_min) <= day_end:
        slot_start = cur
        slot_end = cur + timedelta(minutes=duration_min)
        conflict = any(_slot_overlaps(slot_start, slot_end, o_s, o_e) for o_s, o_e in occupied)
        if not conflict:
            score, reason = _score_slot(slot_start, slot_end, occupied, energy, profile)
            suggestions.append(
                {
                    "slot_id": f"{task_date.isoformat()}@{slot_start.time().strftime('%H:%M:%S')}",
                    "date": task_date,
                    "start_time": slot_start.time(),
                    "end_time": slot_end.time(),
                    "score": score,
                    "reason": reason,
                }
            )
        cur += step

    suggestions.sort(key=lambda s: (-s["score"], s["start_time"]))
    return suggestions[:limit]


def detect_conflicts_for_draft(db: Session, draft: dict) -> tuple[list[Task], list[dict]]:
    task_date = draft.get("date")
    start_t = draft.get("start_time")
    end_t = draft.get("end_time")
    duration_min = int(draft.get("duration_minutes") or 60)
    if not task_date or not start_t:
        return [], []
    if not end_t:
        end_t = (datetime.combine(task_date, start_t) + timedelta(minutes=duration_min)).time()

    start_dt = datetime.combine(task_date, start_t)
    end_dt = datetime.combine(task_date, end_t)
    day_tasks = (
        db.query(Task)
        .filter(Task.date == task_date, Task.completed == False)
        .all()
    )
    conflicts = []
    for task in day_tasks:
        t_start, t_end = _task_datetime_range(task)
        if not t_start or not t_end:
            continue
        if _slot_overlaps(start_dt, end_dt, t_start, t_end):
            conflicts.append(task)
    suggestions = suggest_alternative_slots(
        db,
        task_date=task_date,
        duration_min=duration_min,
        task_text=f"{draft.get('title', '')} {draft.get('description', '')}",
        limit=3,
    ) if conflicts else []
    return conflicts, suggestions


def _build_clarification_prompt(intent_id: str, field: str, question: str, options: list[dict] | None = None) -> ClarificationPrompt:
    prompt_options = []
    for idx, item in enumerate(options or []):
        prompt_options.append(
            ClarificationOption(
                id=str(item.get("id") or f"opt-{idx+1}"),
                label=str(item.get("label") or item.get("value") or f"Option {idx+1}"),
                value=str(item.get("value") or item.get("label") or ""),
            )
        )
    return ClarificationPrompt(
        intent_id=intent_id,
        question=question,
        field=field,
        options=prompt_options,
        allow_free_text=True,
    )


def _dynamic_time_options_for_draft(db: Session, draft: dict, limit: int = 3) -> list[dict]:
    task_date = draft.get("date")
    duration = int(draft.get("duration_minutes") or 60)
    text = f"{draft.get('title', '')} {draft.get('description', '')}".strip()
    suggestions = suggest_alternative_slots(
        db=db,
        task_date=task_date,
        duration_min=duration,
        task_text=text,
        limit=limit,
    )
    if suggestions:
        return [
            {
                "id": s["slot_id"],
                "label": s["start_time"].strftime("%I:%M %p").lstrip("0"),
                "value": s["start_time"].strftime("%H:%M"),
            }
            for s in suggestions
        ]

    # Fallback: propose near-future times on the same day at 15-min grid.
    now = datetime.now()
    base = datetime.combine(task_date, time(9, 0))
    if task_date == now.date():
        minute_bucket = ((now.minute // 15) + 1) * 15
        aligned_now = now.replace(second=0, microsecond=0)
        if minute_bucket >= 60:
            aligned_now = aligned_now.replace(minute=0) + timedelta(hours=1)
        else:
            aligned_now = aligned_now.replace(minute=minute_bucket)
        base = max(base, aligned_now)

    out = []
    cur = base
    while len(out) < limit and cur.time() <= time(21, 0):
        out.append(
            {
                "id": f"fallback-{cur.strftime('%H%M')}",
                "label": cur.strftime("%I:%M %p").lstrip("0"),
                "value": cur.strftime("%H:%M"),
            }
        )
        cur += timedelta(minutes=15)
    return out


def handle_followup_replan(
    message: str,
    db: Session,
    thread_state: dict | None = None,
    reference_date: date | None = None,
    return_metadata: bool = False,
):
    today = reference_date or date.today()
    text = (message or "").lower()
    if not text.strip():
        return ([], {"memory_used": False, "referenced_task_ids": []}) if return_metadata else []
    requested_time = extract_time_from_text(message)

    memory_order = []
    if thread_state:
        memory_order = (
            thread_state.get("last_referenced_task_ids", [])
            + thread_state.get("last_updated_task_ids", [])
            + thread_state.get("last_created_task_ids", [])
        )
    seen_memory = set()
    ordered_memory_ids = []
    for tid in memory_order:
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            continue
        if tid_int in seen_memory:
            continue
        seen_memory.add(tid_int)
        ordered_memory_ids.append(tid_int)

    upcoming = (
        db.query(Task)
        .filter(Task.date >= today, Task.completed == False)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.priority.desc())
        .all()
    )
    memory_tasks = []
    if ordered_memory_ids:
        memory_rows = (
            db.query(Task)
            .filter(Task.id.in_(ordered_memory_ids), Task.completed == False)
            .all()
        )
        by_id = {t.id: t for t in memory_rows}
        memory_tasks = [by_id[i] for i in ordered_memory_ids if i in by_id]

    pool_by_id = {}
    for t in upcoming + memory_tasks:
        pool_by_id[t.id] = t
    candidate_tasks = list(pool_by_id.values())

    if not candidate_tasks:
        return ([], {"memory_used": False, "referenced_task_ids": []}) if return_metadata else []

    meta = {"memory_used": False, "referenced_task_ids": []}

    def _tokenize(s: str) -> set[str]:
        tokens = set()
        for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
            if len(w) <= 1 or w in STOP_WORDS:
                continue
            tokens.add(w)
            # lightweight normalization for gerunds/plurals to improve matching.
            if len(w) > 4 and w.endswith("ing"):
                tokens.add(w[:-3])
            if len(w) > 3 and w.endswith("s"):
                tokens.add(w[:-1])
        return tokens

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
    anchor = _pick_best_task(candidate_tasks, all_tokens) if all_tokens else None
    pronoun_ref = bool(re.search(r"\b(it|that|this|one)\b", text))

    if not anchor and thread_state:
        id_to_task = {t.id: t for t in candidate_tasks}
        # Prefer same-day references first, then any-date fallback.
        same_day_candidates = []
        any_day_candidates = []
        for tid in memory_order:
            candidate = id_to_task.get(tid)
            if not candidate:
                continue
            if candidate.date == today:
                same_day_candidates.append(candidate)
            else:
                any_day_candidates.append(candidate)
        if same_day_candidates:
            anchor = same_day_candidates[0]
            meta["memory_used"] = True
        elif any_day_candidates:
            anchor = any_day_candidates[0]
            meta["memory_used"] = True

    if not anchor and all_tokens:
        anchor = _pick_best_task(candidate_tasks, all_tokens)

    if not anchor and pronoun_ref:
        # Immediate-context fallback: treat pronouns as most recently created/updated task.
        anchor = max(candidate_tasks, key=lambda t: t.id)

    if not requested_time:
        requested_time = _extract_change_to_time(message, anchor)

    if not anchor:
        if requested_time:
            # If only a time is given, use most recent scheduled task as likely referent.
            scheduled = [t for t in candidate_tasks if t.start_time]
            anchor = max(scheduled, key=lambda t: t.id) if scheduled else max(candidate_tasks, key=lambda t: t.id)
        else:
            return ([], meta) if return_metadata else []

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

    # Support patterns like "move talking to mom from 6 to 7:30"
    move_from_to_match = re.search(
        r"\bmove\s+(?:the\s+)?(.+?)\s+from\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    from_to_from_token = None
    from_to_to_token = None
    if move_from_to_match:
        move_target_tokens = _tokenize(move_from_to_match.group(1))
        from_to_from_token = move_from_to_match.group(2)
        from_to_to_token = move_from_to_match.group(3)

    def _parse_from_time_variants(raw: str):
        if not raw:
            return []
        m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", raw, flags=re.IGNORECASE)
        if not m:
            return []
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridian = (m.group(3) or "").lower()
        variants = []
        if meridian:
            if meridian == "pm" and hour != 12:
                hour += 12
            elif meridian == "am" and hour == 12:
                hour = 0
            variants.append(time(hour, minute))
        else:
            if 0 <= hour <= 23:
                variants.append(time(hour, minute))
            if 1 <= hour <= 11:
                variants.append(time(hour + 12, minute))
        # dedupe
        uniq = []
        for v in variants:
            if v not in uniq:
                uniq.append(v)
        return uniq

    # If user explicitly says "move <task> ...", force anchor by that phrase first.
    if move_target_tokens:
        targeted_anchor = _pick_best_task(candidate_tasks, move_target_tokens)
        if targeted_anchor:
            anchor = targeted_anchor
        elif from_to_from_token:
            from_variants = _parse_from_time_variants(from_to_from_token)
            if from_variants:
                candidates = [
                    t for t in candidate_tasks
                    if t.start_time and any((t.start_time.hour == fv.hour and t.start_time.minute == fv.minute) for fv in from_variants)
                ]
                if candidates:
                    # If multiple tasks at same from-time, pick best semantic match.
                    semantic = _pick_best_task(candidates, move_target_tokens)
                    anchor = semantic or max(candidates, key=lambda t: t.id)
    elif from_to_from_token and not anchor:
        # Fallback for "move ... from A to B" when text tokens are noisy.
        from_variants = _parse_from_time_variants(from_to_from_token)
        if from_variants:
            candidates = [
                t for t in candidate_tasks
                if t.start_time and any((t.start_time.hour == fv.hour and t.start_time.minute == fv.minute) for fv in from_variants)
            ]
            if candidates:
                anchor = max(candidates, key=lambda t: t.id)

    # In "move X from A to B", resolve destination time after anchor is known so AM/PM can inherit from anchor.
    if from_to_to_token:
        requested_time = _extract_change_to_time(
            f"change to {from_to_to_token}",
            anchor,
        ) or requested_time

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
        meta["referenced_task_ids"].append(anchor.id)

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
            meta["referenced_task_ids"].append(related.id)

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
        meta["referenced_task_ids"].append(anchor.id)

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

    if not meta["referenced_task_ids"]:
        meta["referenced_task_ids"] = [t.id for t in changed][:10]
    if return_metadata:
        return changed, meta
    return changed


def _build_response(
    mode: str,
    message: str,
    created_tasks=None,
    updated_tasks=None,
    unchanged_tasks=None,
    used_fallback_parser: bool = False,
    used_replan_handler: bool = False,
    resolved_thread_key: str | None = None,
    memory_used: bool = False,
    requires_user_input: bool = False,
    pending_intent_id: str | None = None,
    applied_after_confirmation: bool = False,
    affected_dates=None,
    warnings=None,
    clarification: ClarificationPrompt | None = None,
    conflict_info: ConflictPrompt | None = None,
) -> ChatResponse:
    created_tasks = created_tasks or []
    updated_tasks = updated_tasks or []
    unchanged_tasks = unchanged_tasks or []
    affected_dates = sorted(list(affected_dates or []))
    warnings = warnings or []

    created_ids = {t.id for t in created_tasks}
    updated_ids = {t.id for t in updated_tasks}
    unscheduled = []
    unscheduled_ids = set()
    for task in created_tasks + updated_tasks + unchanged_tasks:
        if task.id in unscheduled_ids:
            continue
        if (not task.start_time) or (not task.end_time):
            unscheduled.append(task)
            unscheduled_ids.add(task.id)

    unchanged_filtered = [t for t in unchanged_tasks if t.id not in created_ids and t.id not in updated_ids]

    return ChatResponse(
        mode=mode,
        message=message,
        created_tasks=created_tasks,
        updated_tasks=updated_tasks,
        unchanged_tasks=unchanged_filtered,
        unscheduled_tasks=unscheduled,
        clarification=clarification,
        conflict_info=conflict_info,
        meta=ChatMeta(
            used_fallback_parser=used_fallback_parser,
            used_replan_handler=used_replan_handler,
            resolved_thread_key=resolved_thread_key,
            memory_used=memory_used,
            requires_user_input=requires_user_input,
            pending_intent_id=pending_intent_id,
            applied_after_confirmation=applied_after_confirmation,
            affected_dates=affected_dates,
            warnings=warnings,
        ),
    )


def _collect_updated_tasks_for_dates(db: Session, before_state_by_date, dates):
    updated = []
    seen = set()
    for d in dates:
        before_state = before_state_by_date.get(d, {})
        refreshed = (
            db.query(Task)
            .filter(Task.date == d, Task.completed == False)
            .order_by(Task.start_time.asc(), Task.priority.desc(), Task.id.asc())
            .all()
        )
        for task in refreshed:
            if task.id not in before_state:
                continue
            before = before_state[task.id]
            after = (task.date, task.start_time, task.end_time)
            if before != after and task.id not in seen:
                updated.append(task)
                seen.add(task.id)
    return updated


def _clear_pending_state(
    db: Session,
    thread_key: str,
    thread_date: date,
    chat_thread_id: str | None,
    thread_state: dict,
    last_intent_type: str,
    last_user_message: str,
) -> None:
    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": last_intent_type,
            "last_user_message": last_user_message,
            "last_created_task_ids": thread_state.get("last_created_task_ids", []),
            "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
            "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
            "pending_intent_id": None,
            "pending_state_type": None,
            "pending_state": {},
        },
    )


def _normalize_task_draft(
    args: dict,
    message: str,
    effective_date: date,
    db: Session,
) -> dict:
    raw_text = args.get("title") or args.get("description") or message
    title = title_from_text(raw_text)
    description = description_from_text(args.get("description") or raw_text)
    priority = int(args.get("priority", 1))
    task_type = args.get("task_type", "other")
    total_effort = args.get("total_effort_minutes")
    deadline_str = args.get("deadline")

    task_date = None
    if args.get("date"):
        try:
            task_date = datetime.strptime(args.get("date"), "%Y-%m-%d").date()
        except Exception:
            task_date = effective_date
    else:
        task_date = effective_date

    start_time_val = None
    end_time_val = None
    start_time_str = args.get("start_time")
    end_time_str = args.get("end_time")
    if isinstance(start_time_str, str) and start_time_str.strip():
        start_time_val = parse_time(start_time_str.strip())
    if isinstance(end_time_str, str) and end_time_str.strip():
        end_time_val = parse_time(end_time_str.strip())
    if not start_time_val:
        start_time_val = extract_time_from_text(
            f"{args.get('title') or ''} {args.get('description') or ''} {message}"
        )

    deadline_date = None
    if deadline_str:
        try:
            deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()
        except Exception:
            deadline_date = None

    if not total_effort:
        mood = db.query(Mood).filter(Mood.date == task_date).first()
        energy = mood.energy if mood else 3
        historical_avg = get_historical_avg_duration(db, task_type, energy)
        total_effort = infer_duration_minutes(
            text=title,
            task_type=task_type,
            energy_level=energy,
            historical_avg=historical_avg,
            deadline_date=deadline_date,
        )
    duration_minutes = int(total_effort or 60)
    if start_time_val and not end_time_val:
        end_time_val = (datetime.combine(task_date, start_time_val) + timedelta(minutes=duration_minutes)).time()

    return {
        "title": title,
        "description": description,
        "date": task_date,
        "start_time": start_time_val,
        "end_time": end_time_val,
        "priority": priority,
        "duration_minutes": duration_minutes,
        "task_type": task_type,
    }


def _build_conflict_prompt(intent_id: str, draft: dict, conflicts: list[Task], suggestions: list[dict]) -> ConflictPrompt:
    conflict_items = [
        ConflictTaskRef(
            task_id=t.id,
            title=t.title,
            date=t.date,
            start_time=t.start_time,
            end_time=t.end_time,
            is_fixed=_is_hard_commitment(t),
        )
        for t in conflicts
    ]
    suggestion_items = [
        SlotSuggestion(
            slot_id=s["slot_id"],
            date=s["date"],
            start_time=s["start_time"],
            end_time=s["end_time"],
            score=float(s["score"]),
            reason=s["reason"],
        )
        for s in suggestions[:3]
    ]
    draft_payload = {
        "title": draft.get("title"),
        "description": draft.get("description"),
        "date": draft.get("date").isoformat() if draft.get("date") else None,
        "start_time": draft.get("start_time").strftime("%H:%M:%S") if draft.get("start_time") else None,
        "end_time": draft.get("end_time").strftime("%H:%M:%S") if draft.get("end_time") else None,
        "duration_minutes": draft.get("duration_minutes"),
        "priority": draft.get("priority"),
    }
    return ConflictPrompt(
        intent_id=intent_id,
        new_task_draft=draft_payload,
        conflicting_tasks=conflict_items,
        suggested_slots=suggestion_items,
        actions=[
            "choose_slot:<slot_id>",
            "keep_original_and_move_conflicts",
            "cancel",
        ],
    )


def _parse_pending_action(message: str, pending_response: dict | None) -> str | None:
    if pending_response and isinstance(pending_response, dict):
        raw_action = pending_response.get("action")
        if isinstance(raw_action, str) and raw_action.strip():
            return raw_action.strip()
    text = (message or "").strip().lower()
    if not text:
        return None
    if text == "cancel":
        return "cancel"
    if "you decide" in text:
        return "choose_best"
    m = re.search(r"choose[_\s-]*slot[:\s]+([0-9]{4}-[0-9]{2}-[0-9]{2}@[0-9:]{8})", text)
    if m:
        return f"choose_slot:{m.group(1)}"
    return None


def handle_pending_resolution(
    message: str,
    pending_response: dict | None,
    confirm: bool | None,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
) -> ChatResponse | None:
    pending_type = thread_state.get("pending_state_type")
    pending_state = thread_state.get("pending_state") or {}
    pending_intent_id = thread_state.get("pending_intent_id")
    if not pending_type or not pending_intent_id:
        return None

    # Clarification resolution: only date and duration for now.
    if pending_type == "clarification":
        action_value = None
        action_from_payload = False
        if pending_response and isinstance(pending_response, dict):
            action_value = pending_response.get("value")
            action_from_payload = bool(action_value)
        if not action_value and message:
            action_value = message.strip()
        if not action_value:
            prompt = _build_clarification_prompt(
                intent_id=pending_intent_id,
                field=pending_state.get("field", "date"),
                question=pending_state.get("question", "Please provide the missing value."),
                options=pending_state.get("options", []),
            )
            return _build_response(
                mode="ask_user",
                message="I still need your answer to continue.",
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                clarification=prompt,
                warnings=["Pending clarification is unresolved."],
            )
        draft = pending_state.get("draft") or {}
        field = pending_state.get("field")
        if draft and field == "start_time":
            parsed_time = None
            if isinstance(action_value, str):
                # For free-text replies, first check if it even looks like a time.
                parsed_time = extract_time_from_text(action_value)
                if not parsed_time and action_from_payload:
                    try:
                        parsed_time = parse_time(action_value)
                    except ValueError:
                        parsed_time = None
            if not parsed_time:
                prompt = _build_clarification_prompt(
                    intent_id=pending_intent_id,
                    field=field,
                    question=pending_state.get("question") or "What time should I schedule this task?",
                    options=pending_state.get("options", []),
                )
                return _build_response(
                    mode="ask_user",
                    message="I still need a time to continue. Please reply with something like 18:00 or 6pm.",
                    resolved_thread_key=thread_key,
                    requires_user_input=True,
                    pending_intent_id=pending_intent_id,
                    clarification=prompt,
                    warnings=["Pending clarification is unresolved."],
                )
            task_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
            duration = int(draft.get("duration_minutes") or 60)
            end_time = (datetime.combine(task_date, parsed_time) + timedelta(minutes=duration)).time()
            before_state = {
                t.id: (t.date, t.start_time, t.end_time)
                for t in db.query(Task).filter(Task.date == task_date, Task.completed == False).all()
            }
            created = create_task(
                db=db,
                title=draft.get("title") or "Task",
                description=draft.get("description") or "Task created from clarification.",
                date=task_date,
                start_time=parsed_time,
                end_time=end_time,
                priority=int(draft.get("priority") or 1),
                duration_minutes=duration,
            )
            rebalance_day(db, task_date, pinned_task_ids={created.id})
            updated = _collect_updated_tasks_for_dates(db, {task_date: before_state}, {task_date})
            updated = [t for t in updated if t.id != created.id]
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "create",
                    "last_user_message": message,
                    "last_created_task_ids": [created.id],
                    "last_updated_task_ids": [t.id for t in updated][:10],
                    "last_referenced_task_ids": [created.id] + [t.id for t in updated][:9],
                    "pending_intent_id": None,
                    "pending_state_type": None,
                    "pending_state": {},
                },
            )
            return _build_response(
                mode="create",
                message="Created and scheduled task after clarification.",
                created_tasks=[created],
                updated_tasks=updated,
                resolved_thread_key=thread_key,
                applied_after_confirmation=True,
                affected_dates={task_date},
            )

        _clear_pending_state(
            db,
            thread_key=thread_key,
            thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            thread_state=thread_state,
            last_intent_type="clarification_resolved",
            last_user_message=message,
        )
        return _build_response(
            mode="schedule",
            message="Thanks, clarification captured.",
            resolved_thread_key=thread_key,
            affected_dates={effective_thread_date},
        )

    if pending_type != "conflict_resolution":
        return None

    draft = pending_state.get("draft") or {}
    if not draft:
        _clear_pending_state(
            db,
            thread_key=thread_key,
            thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            thread_state=thread_state,
            last_intent_type="pending_invalid",
            last_user_message=message,
        )
        return _build_response(
            mode="replan",
            message="Pending request expired; please resend the task.",
            resolved_thread_key=thread_key,
            warnings=["Pending conflict state was invalid."],
        )

    action = _parse_pending_action(message, pending_response)
    if action is None and confirm is False:
        action = "cancel"
    if action is None and confirm is True:
        action = "choose_best"

    suggestions = pending_state.get("suggestions", [])
    conflicts = (
        db.query(Task)
        .filter(Task.id.in_(pending_state.get("conflict_ids", [])))
        .all()
        if pending_state.get("conflict_ids")
        else []
    )
    conflict_prompt = _build_conflict_prompt(
        intent_id=pending_intent_id,
        draft={
            "title": draft.get("title"),
            "description": draft.get("description"),
            "date": datetime.strptime(draft["date"], "%Y-%m-%d").date() if draft.get("date") else None,
            "start_time": parse_time(draft["start_time"]) if draft.get("start_time") else None,
            "end_time": parse_time(draft["end_time"]) if draft.get("end_time") else None,
            "duration_minutes": int(draft.get("duration_minutes") or 60),
            "priority": int(draft.get("priority") or 1),
        },
        conflicts=conflicts,
        suggestions=[
            {
                "slot_id": s["slot_id"],
                "date": datetime.strptime(s["date"], "%Y-%m-%d").date(),
                "start_time": parse_time(s["start_time"]),
                "end_time": parse_time(s["end_time"]),
                "score": float(s.get("score", 0)),
                "reason": s.get("reason", "Suggested alternative"),
            }
            for s in suggestions
        ],
    )

    if action in (None, ""):
        return _build_response(
            mode="conflict",
            message="There is a scheduling conflict. Choose one of the suggested actions.",
            resolved_thread_key=thread_key,
            requires_user_input=True,
            pending_intent_id=pending_intent_id,
            conflict_info=conflict_prompt,
            warnings=["Pending conflict resolution requires your choice."],
        )

    if action == "cancel":
        _clear_pending_state(
            db,
            thread_key=thread_key,
            thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            thread_state=thread_state,
            last_intent_type="cancel",
            last_user_message=message,
        )
        return _build_response(
            mode="replan",
            message="Okay, I canceled that scheduling change.",
            resolved_thread_key=thread_key,
        )

    choose_best = action == "choose_best"
    selected_slot = None
    if choose_best and suggestions:
        selected_slot = suggestions[0]
    elif action.startswith("choose_slot:"):
        slot_id = action.split(":", 1)[1]
        selected_slot = next((s for s in suggestions if s.get("slot_id") == slot_id), None)
        if not selected_slot:
            return _build_response(
                mode="conflict",
                message="I couldn't find that slot. Please choose one of the suggested slots.",
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=conflict_prompt,
                warnings=["Selected slot was not found in suggestions."],
            )

    if selected_slot:
        draft["date"] = selected_slot["date"]
        draft["start_time"] = selected_slot["start_time"]
        draft["end_time"] = selected_slot["end_time"]
    elif action != "keep_original_and_move_conflicts":
        return _build_response(
            mode="conflict",
            message="Please choose a slot, keep original and move conflicts, or cancel.",
            resolved_thread_key=thread_key,
            requires_user_input=True,
            pending_intent_id=pending_intent_id,
            conflict_info=conflict_prompt,
        )

    draft_date = datetime.strptime(draft["date"], "%Y-%m-%d").date()
    draft_start = parse_time(draft["start_time"]) if draft.get("start_time") else None
    draft_end = parse_time(draft["end_time"]) if draft.get("end_time") else None
    duration = int(draft.get("duration_minutes") or 60)
    if draft_start and not draft_end:
        draft_end = (datetime.combine(draft_date, draft_start) + timedelta(minutes=duration)).time()

    before_state = {
        t.id: (t.date, t.start_time, t.end_time)
        for t in db.query(Task).filter(Task.date == draft_date, Task.completed == False).all()
    }
    created = create_task(
        db=db,
        title=draft.get("title") or "Task",
        description=draft.get("description") or "Task created from pending conflict resolution.",
        date=draft_date,
        start_time=draft_start,
        end_time=draft_end,
        priority=int(draft.get("priority") or 1),
        duration_minutes=duration,
    )
    pinned_ids = {created.id} if draft_start else set()
    rebalance_day(db, draft_date, pinned_task_ids=pinned_ids)
    updated = _collect_updated_tasks_for_dates(db, {draft_date: before_state}, {draft_date})
    updated = [t for t in updated if t.id != created.id]

    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": effective_thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": "create",
            "last_user_message": message,
            "last_created_task_ids": [created.id],
            "last_updated_task_ids": [t.id for t in updated][:10],
            "last_referenced_task_ids": [created.id] + [t.id for t in updated][:9],
            "pending_intent_id": None,
            "pending_state_type": None,
            "pending_state": {},
        },
    )
    return _build_response(
        mode="create",
        message="Scheduled after your confirmation.",
        created_tasks=[created],
        updated_tasks=updated,
        resolved_thread_key=thread_key,
        applied_after_confirmation=bool(choose_best),
        affected_dates={draft_date},
    )


def _build_model_input_for_decision(db: Session, thread_key: str, thread_state: dict, effective_thread_date: date, message: str) -> str:
    context = build_schedule_context(db, today=effective_thread_date)
    thread_summary = _thread_summary_for_prompt(db, thread_key, thread_state)
    return (
        "Existing schedule context (use this for follow-up commands):\n"
        f"{context}\n\n"
        "Thread memory summary:\n"
        f"{thread_summary}\n\n"
        f"User message:\n{message}"
    )


def _save_ask_user_pending_state(
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    message: str,
    field: str,
    question: str,
    options_raw: list[dict],
    extra_pending_state: dict | None = None,
) -> str:
    pending_intent_id = str(uuid.uuid4())
    pending_state = {
        "field": field,
        "question": question,
        "options": options_raw,
    }
    if extra_pending_state:
        pending_state.update(extra_pending_state)
    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": effective_thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": "ask_user",
            "last_user_message": message,
            "last_created_task_ids": thread_state.get("last_created_task_ids", []),
            "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
            "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
            "pending_intent_id": pending_intent_id,
            "pending_state_type": "clarification",
            "pending_state": pending_state,
        },
    )
    return pending_intent_id


def _build_ask_user_response(thread_key: str, pending_intent_id: str, field: str, question: str, options_raw: list[dict], used_replan_handler: bool = False) -> ChatResponse:
    return _build_response(
        mode="ask_user",
        message=question,
        used_replan_handler=used_replan_handler,
        resolved_thread_key=thread_key,
        requires_user_input=True,
        pending_intent_id=pending_intent_id,
        clarification=_build_clarification_prompt(
            intent_id=pending_intent_id,
            field=field,
            question=question,
            options=options_raw,
        ),
    )


def _normalize_args_list(decision: Any, message: str) -> tuple[list[dict], bool]:
    fallback_multi = split_tasks_from_message(message)
    used_fallback_parser = False
    args_list = decision.arguments if decision.action == "create_task" else fallback_multi
    if decision.action != "create_task":
        used_fallback_parser = bool(fallback_multi)
    if isinstance(args_list, dict):
        args_list = [args_list]
    if not args_list:
        args_list = fallback_multi
        used_fallback_parser = True
    elif len(args_list) == 1:
        only = args_list[0] or {}
        only_title = (only.get("title") or "").strip().lower()
        input_clean = message.strip().lower()
        if len(fallback_multi) > 1 and (not only_title or only_title == input_clean):
            args_list = fallback_multi
            used_fallback_parser = True
    return args_list or [], used_fallback_parser


def _handle_non_create_decision(
    message: str,
    decision: Any,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
) -> tuple[ChatResponse | None, list[dict], bool]:
    fallback_multi = split_tasks_from_message(message)
    if decision.action == "create_task":
        return None, [], False

    replanned, replan_meta = handle_followup_replan(
        message,
        db,
        thread_state=thread_state,
        reference_date=effective_thread_date,
        return_metadata=True,
    )
    if replanned:
        referenced_ids = [t.id for t in replanned][:10]
        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": effective_thread_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "replan",
                "last_user_message": message,
                "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": referenced_ids,
                "last_referenced_task_ids": referenced_ids,
            },
        )
        return (
            _build_response(
                mode="replan",
                message="Updated existing tasks based on your follow-up request.",
                updated_tasks=replanned,
                used_replan_handler=True,
                resolved_thread_key=thread_key,
                memory_used=bool(replan_meta.get("memory_used")),
                affected_dates={t.date for t in replanned},
            ),
            [],
            False,
        )

    if decision.action == "get_schedule":
        tasks = get_schedule(effective_thread_date, db)
        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": effective_thread_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "schedule",
                "last_user_message": message,
                "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
            },
        )
        return (
            _build_response(
                mode="schedule",
                message="Here is your schedule.",
                unchanged_tasks=tasks,
                resolved_thread_key=thread_key,
                memory_used=False,
                affected_dates={effective_thread_date},
            ),
            [],
            False,
        )

    if not fallback_multi:
        lower = (message or "").lower()
        if any(k in lower for k in ["move", "change", "shift", "reschedule"]):
            question = "Which task should I change? Share the task title or current time slot."
            options_raw = []
            refs = thread_state.get("last_referenced_task_ids", [])[:3]
            if refs:
                ref_tasks = db.query(Task).filter(Task.id.in_(refs)).all()
                by_id = {t.id: t for t in ref_tasks}
                for tid in refs:
                    t = by_id.get(tid)
                    if not t:
                        continue
                    options_raw.append(
                        {
                            "id": f"task-{t.id}",
                            "label": f"{t.title} ({t.date.isoformat()})",
                            "value": str(t.id),
                        }
                    )
            pending_intent_id = _save_ask_user_pending_state(
                db=db,
                thread_key=thread_key,
                thread_state=thread_state,
                effective_thread_date=effective_thread_date,
                chat_thread_id=chat_thread_id,
                message=message,
                field="target_task",
                question=question,
                options_raw=options_raw,
            )
            return (_build_ask_user_response(thread_key, pending_intent_id, "target_task", question, options_raw, used_replan_handler=True), [], False)

        save_thread_state(
            db,
            thread_key,
            {
                "thread_date": effective_thread_date,
                "chat_thread_id": chat_thread_id,
                "last_intent_type": "replan",
                "last_user_message": message,
                "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
            },
        )
        return (
            _build_response(
                mode="replan",
                message="No matching task updates were applied.",
                used_replan_handler=True,
                resolved_thread_key=thread_key,
                memory_used=bool(replan_meta.get("memory_used")),
                warnings=["No matching tasks found for this follow-up request."],
            ),
            [],
            False,
        )

    return None, fallback_multi, True


def _maybe_return_interactive_create_gate(
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
    message: str,
    decision: Any,
    args_list: list[dict],
    args: dict,
    draft: dict,
) -> ChatResponse | None:
    if (
        len(args_list) == 1
        and decision.action == "create_task"
        and not draft.get("start_time")
        and not bool((args or {}).get("spread", False))
    ):
        question = "What time should I schedule this task?"
        options_raw = _dynamic_time_options_for_draft(db, draft, limit=3)
        pending_intent_id = _save_ask_user_pending_state(
            db=db,
            thread_key=thread_key,
            thread_state=thread_state,
            effective_thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            message=message,
            field="start_time",
            question=question,
            options_raw=options_raw,
            extra_pending_state={
                "draft": {
                    "title": draft["title"],
                    "description": draft["description"],
                    "date": draft["date"].isoformat(),
                    "start_time": None,
                    "end_time": None,
                    "duration_minutes": int(draft["duration_minutes"]),
                    "priority": int(draft["priority"]),
                }
            },
        )
        return _build_ask_user_response(thread_key, pending_intent_id, "start_time", question, options_raw)

    if len(args_list) == 1 and draft.get("start_time") and decision.action == "create_task":
        conflicts, suggestions = detect_conflicts_for_draft(db, draft)
        if conflicts:
            pending_intent_id = str(uuid.uuid4())
            pending_state = {
                "draft": {
                    "title": draft["title"],
                    "description": draft["description"],
                    "date": draft["date"].isoformat(),
                    "start_time": draft["start_time"].strftime("%H:%M:%S") if draft.get("start_time") else None,
                    "end_time": draft["end_time"].strftime("%H:%M:%S") if draft.get("end_time") else None,
                    "duration_minutes": int(draft["duration_minutes"]),
                    "priority": int(draft["priority"]),
                },
                "conflict_ids": [t.id for t in conflicts],
                "suggestions": [
                    {
                        "slot_id": s["slot_id"],
                        "date": s["date"].isoformat(),
                        "start_time": s["start_time"].strftime("%H:%M:%S"),
                        "end_time": s["end_time"].strftime("%H:%M:%S"),
                        "score": float(s["score"]),
                        "reason": s["reason"],
                    }
                    for s in suggestions
                ],
            }
            save_thread_state(
                db,
                thread_key,
                {
                    "thread_date": effective_thread_date,
                    "chat_thread_id": chat_thread_id,
                    "last_intent_type": "conflict",
                    "last_user_message": message,
                    "last_created_task_ids": thread_state.get("last_created_task_ids", []),
                    "last_updated_task_ids": thread_state.get("last_updated_task_ids", []),
                    "last_referenced_task_ids": thread_state.get("last_referenced_task_ids", []),
                    "pending_intent_id": pending_intent_id,
                    "pending_state_type": "conflict_resolution",
                    "pending_state": pending_state,
                },
            )
            return _build_response(
                mode="conflict",
                message="I found a scheduling conflict. Pick one of the suggested slots or actions.",
                resolved_thread_key=thread_key,
                requires_user_input=True,
                pending_intent_id=pending_intent_id,
                conflict_info=_build_conflict_prompt(
                    intent_id=pending_intent_id,
                    draft=draft,
                    conflicts=conflicts,
                    suggestions=suggestions,
                ),
            )
    return None


def _execute_create_args(
    message: str,
    args_list: list[dict],
    decision: Any,
    db: Session,
    thread_key: str,
    thread_state: dict,
    effective_thread_date: date,
    chat_thread_id: str | None,
) -> tuple[ChatResponse | None, list[Task], set[date], dict, dict]:
    created_tasks = []
    affected_dates = set()
    pinned_task_ids_by_date = {}
    before_state_by_date = {}

    def ensure_before_state(target_date):
        if target_date in before_state_by_date:
            return
        tasks = db.query(Task).filter(Task.date == target_date, Task.completed == False).all()
        before_state_by_date[target_date] = {t.id: (t.date, t.start_time, t.end_time) for t in tasks}

    for args in args_list or []:
        draft = _normalize_task_draft(args=args or {}, message=message, effective_date=effective_thread_date, db=db)
        interactive_gate = _maybe_return_interactive_create_gate(
            db=db,
            thread_key=thread_key,
            thread_state=thread_state,
            effective_thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            message=message,
            decision=decision,
            args_list=args_list,
            args=args or {},
            draft=draft,
        )
        if interactive_gate:
            return interactive_gate, created_tasks, affected_dates, pinned_task_ids_by_date, before_state_by_date

        raw_text = args.get("title") or args.get("description") or message
        text = title_from_text(raw_text)
        description = description_from_text(args.get("description") or raw_text)
        priority = args.get("priority", 1)
        task_date_str = args.get("date")
        start_time_str = args.get("start_time")
        end_time_str = args.get("end_time")
        task_type = args.get("task_type", "other")
        spread = args.get("spread", False)
        total_effort = args.get("total_effort_minutes")
        deadline_str = args.get("deadline")

        task_date = datetime.strptime(task_date_str, "%Y-%m-%d").date() if task_date_str else None
        deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date() if deadline_str else None
        base_date = effective_thread_date

        start_time_val = parse_time(start_time_str.strip()) if isinstance(start_time_str, str) and start_time_str.strip() else None
        end_time_val = parse_time(end_time_str.strip()) if isinstance(end_time_str, str) and end_time_str.strip() else None
        if not start_time_val:
            start_time_val = extract_time_from_text(f"{args.get('title') or ''} {args.get('description') or ''} {message}")

        effective_date = task_date or base_date
        if not total_effort:
            mood = db.query(Mood).filter(Mood.date == effective_date).first()
            energy = mood.energy if mood else 3
            historical_avg = get_historical_avg_duration(db, task_type, energy)
            total_effort = infer_duration_minutes(
                text=text,
                task_type=task_type,
                energy_level=energy,
                historical_avg=historical_avg,
                deadline_date=deadline_date,
            )

        if start_time_val and not end_time_val and total_effort:
            end_time_val = (datetime.combine(effective_date, start_time_val) + timedelta(minutes=total_effort)).time()

        if spread and deadline_date:
            if deadline_date <= base_date:
                session_date = base_date
                duration_min = total_effort
                ensure_before_state(session_date)
                task = create_task(
                    db=db,
                    title=f"{text} (Urgent)",
                    description=description,
                    date=session_date,
                    start_time=start_time_val,
                    end_time=end_time_val,
                    priority=priority + 1,
                    duration_minutes=duration_min,
                )
                affected_dates.add(session_date)
                if start_time_val:
                    pinned_task_ids_by_date.setdefault(session_date, set()).add(task.id)
                created_tasks.append(task)
                continue

            days_available = (deadline_date - base_date).days
            days_to_plan = min(max(days_available, 1), 7)
            base_session = max(45, total_effort // days_to_plan)
            remaining = total_effort
            for i in range(days_to_plan):
                if remaining <= 0:
                    break
                session_date = base_date + timedelta(days=i)
                duration_min = min(base_session, remaining)
                remaining -= duration_min
                ensure_before_state(session_date)
                task = create_task(
                    db=db,
                    title=f"{text} (Session {i+1})",
                    description=description,
                    date=session_date,
                    start_time=None,
                    end_time=None,
                    priority=priority,
                    duration_minutes=duration_min,
                )
                affected_dates.add(session_date)
                created_tasks.append(task)
            continue

        if not task_date:
            task_date = base_date
        ensure_before_state(task_date)
        task = create_task(
            db=db,
            title=text,
            description=description,
            date=task_date,
            start_time=start_time_val,
            end_time=end_time_val,
            priority=priority,
            duration_minutes=total_effort,
        )
        affected_dates.add(task_date)
        if start_time_val:
            pinned_task_ids_by_date.setdefault(task_date, set()).add(task.id)
        created_tasks.append(task)

    return None, created_tasks, affected_dates, pinned_task_ids_by_date, before_state_by_date


def process_chat_request(
    message: str,
    db: Session,
    decide_fn: Callable[[str], Any],
    chat_thread_id: str | None = None,
    thread_date: date | None = None,
    pending_response: dict | None = None,
    confirm: bool | None = None,
) -> ChatResponse:
    today = date.today()
    effective_thread_date = thread_date or today
    thread_key = resolve_thread_key(chat_thread_id, effective_thread_date)
    thread_state = load_thread_state(db, thread_key)

    pending_result = handle_pending_resolution(
        message=message,
        pending_response=pending_response,
        confirm=confirm,
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
    )
    if pending_result:
        return pending_result

    model_input = _build_model_input_for_decision(
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        message=message,
    )
    decision = decide_fn(model_input)

    if decision.action == "ask_user":
        args = decision.arguments if isinstance(decision.arguments, dict) else {}
        field = args.get("field") or "detail"
        question = args.get("question") or decision.message or "Please clarify so I can continue."
        options_raw = args.get("options") if isinstance(args.get("options"), list) else []
        pending_intent_id = _save_ask_user_pending_state(
            db=db,
            thread_key=thread_key,
            thread_state=thread_state,
            effective_thread_date=effective_thread_date,
            chat_thread_id=chat_thread_id,
            message=message,
            field=field,
            question=question,
            options_raw=options_raw,
        )
        return _build_ask_user_response(thread_key, pending_intent_id, field, question, options_raw)

    non_create_response, fallback_from_non_create, used_fallback_from_non_create = _handle_non_create_decision(
        message=message,
        decision=decision,
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
    )
    if non_create_response:
        return non_create_response

    args_list, used_fallback_parser = _normalize_args_list(decision, message)
    if used_fallback_from_non_create:
        args_list = fallback_from_non_create
        used_fallback_parser = True

    interactive_response, created_tasks, affected_dates, pinned_task_ids_by_date, before_state_by_date = _execute_create_args(
        message=message,
        args_list=args_list,
        decision=decision,
        db=db,
        thread_key=thread_key,
        thread_state=thread_state,
        effective_thread_date=effective_thread_date,
        chat_thread_id=chat_thread_id,
    )
    if interactive_response:
        return interactive_response

    for d in affected_dates:
        rebalance_day(
            db,
            d,
            pinned_task_ids=pinned_task_ids_by_date.get(d, set()),
        )

    updated_tasks = _collect_updated_tasks_for_dates(db, before_state_by_date, affected_dates)
    created_ids = {t.id for t in created_tasks}
    updated_tasks = [t for t in updated_tasks if t.id not in created_ids]

    created_ids = [t.id for t in created_tasks][:10]
    updated_ids = [t.id for t in updated_tasks][:10]
    referenced_ids = (updated_ids + created_ids)[:10]
    save_thread_state(
        db,
        thread_key,
        {
            "thread_date": effective_thread_date,
            "chat_thread_id": chat_thread_id,
            "last_intent_type": "create",
            "last_user_message": message,
            "last_created_task_ids": created_ids,
            "last_updated_task_ids": updated_ids,
            "last_referenced_task_ids": referenced_ids,
        },
    )

    return _build_response(
        mode="create",
        message="Created and scheduled tasks.",
        created_tasks=created_tasks,
        updated_tasks=updated_tasks,
        used_fallback_parser=used_fallback_parser,
        resolved_thread_key=thread_key,
        memory_used=False,
        affected_dates=affected_dates,
    )
