from datetime import date, datetime, time, timedelta
from typing import Any, Callable, List
import re
import json

from sqlalchemy.orm import Session

from app.models import ConversationThread, Mood, Task
from app.schemas import ChatMeta, ChatResponse
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
    affected_dates=None,
    warnings=None,
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
        meta=ChatMeta(
            used_fallback_parser=used_fallback_parser,
            used_replan_handler=used_replan_handler,
            resolved_thread_key=resolved_thread_key,
            memory_used=memory_used,
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


def process_chat_request(
    message: str,
    db: Session,
    decide_fn: Callable[[str], Any],
    chat_thread_id: str | None = None,
    thread_date: date | None = None,
) -> ChatResponse:
    today = date.today()
    effective_thread_date = thread_date or today
    thread_key = resolve_thread_key(chat_thread_id, effective_thread_date)
    thread_state = load_thread_state(db, thread_key)
    context = build_schedule_context(db, today=effective_thread_date)
    thread_summary = _thread_summary_for_prompt(db, thread_key, thread_state)
    model_input = (
        "Existing schedule context (use this for follow-up commands):\n"
        f"{context}\n\n"
        "Thread memory summary:\n"
        f"{thread_summary}\n\n"
        f"User message:\n{message}"
    )
    decision = decide_fn(model_input)
    fallback_multi = split_tasks_from_message(message)
    used_fallback_parser = False

    if decision.action != "create_task":
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
            return _build_response(
                mode="replan",
                message="Updated existing tasks based on your follow-up request.",
                updated_tasks=replanned,
                used_replan_handler=True,
                resolved_thread_key=thread_key,
                memory_used=bool(replan_meta.get("memory_used")),
                affected_dates={t.date for t in replanned},
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
            return _build_response(
                mode="schedule",
                message="Here is your schedule.",
                unchanged_tasks=tasks,
                resolved_thread_key=thread_key,
                memory_used=False,
                affected_dates={effective_thread_date},
            )
        # If model action drifts but input clearly contains task items, continue with local extraction.
        if not fallback_multi:
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
            return _build_response(
                mode="replan",
                message="No matching task updates were applied.",
                used_replan_handler=True,
                resolved_thread_key=thread_key,
                memory_used=bool(replan_meta.get("memory_used")),
                warnings=["No matching tasks found for this follow-up request."],
            )
        args_list = fallback_multi
        used_fallback_parser = True
    else:
        args_list = decision.arguments

    created_tasks = []

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

    # Track which dates need rebalancing and tasks that should stay fixed
    affected_dates = set()
    pinned_task_ids_by_date = {}
    before_state_by_date = {}

    def ensure_before_state(target_date):
        if target_date in before_state_by_date:
            return
        tasks = db.query(Task).filter(Task.date == target_date, Task.completed == False).all()
        before_state_by_date[target_date] = {
            t.id: (t.date, t.start_time, t.end_time)
            for t in tasks
        }

    for args in args_list or []:
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

        task_date = None
        if task_date_str:
            task_date = datetime.strptime(task_date_str, "%Y-%m-%d").date()

        deadline_date = None
        if deadline_str:
            deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()

        base_date = effective_thread_date

        # Parse explicit times when provided
        start_time_val = None
        end_time_val = None
        if isinstance(start_time_str, str) and start_time_str.strip():
            start_time_val = parse_time(start_time_str.strip())
        if isinstance(end_time_str, str) and end_time_str.strip():
            end_time_val = parse_time(end_time_str.strip())

        # Fallback: if model omitted explicit start_time, infer from text.
        if not start_time_val:
            start_time_val = extract_time_from_text(
                f"{args.get('title') or ''} {args.get('description') or ''} {message}"
            )

        effective_date = task_date or base_date

        # Duration fallback
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
            end_time_val = (
                datetime.combine(effective_date, start_time_val) +
                timedelta(minutes=total_effort)
            ).time()

        # Spread logic
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

        # Single-day task
        if not task_date:
            task_date = base_date
        duration_min = total_effort
        ensure_before_state(task_date)
        task = create_task(
            db=db,
            title=text,
            description=description,
            date=task_date,
            start_time=start_time_val,
            end_time=end_time_val,
            priority=priority,
            duration_minutes=duration_min,
        )
        affected_dates.add(task_date)
        if start_time_val:
            pinned_task_ids_by_date.setdefault(task_date, set()).add(task.id)
        created_tasks.append(task)

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
