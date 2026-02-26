from app.models import Mood, Task
from agents.scheduler_agent import scheduler_agent
from agents.scheduler_executor import execute_decision
from services.planner import get_historical_avg_duration, infer_task_type, rebalance_day
from services.scheduling import extract_date_from_text, infer_duration_minutes, optimize_day_schedule, parse_time
from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import date, datetime, time, timedelta
import re
from sqlalchemy.orm import Session
from app.db import get_db
from app.schemas import ChatInput, DailyScheduleResponse, MoodCreate, MoodResponse, ReflectionCreate, ReflectionResponse, TaskCreate, TaskResponse, TaskReflectionInput
from services.tools import create_mood, create_reflection, create_task, get_mood, get_tasks, get_reflection, get_today_schedule, get_schedule, complete_task, infer_priority, reflect_on_task, delete_task, reschedule_overdue_tasks, get_summary

app = FastAPI(title="AI Life Planner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check():
    return {"status": "ok"}

def _title_from_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip(" .,\n\t"))
    if not clean:
        return "Untitled task"
    words = clean.split(" ")
    short = " ".join(words[:8])
    return short[:1].upper() + short[1:]

def _description_from_text(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return "Task generated from user request."
    return clean if clean.endswith((".", "!", "?")) else f"{clean}."

def _split_tasks_from_message(message: str):
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
    for segment in candidates:
        task_date = extract_date_from_text(segment)
        tasks.append(
            {
                "title": _title_from_text(segment),
                "description": _description_from_text(segment),
                "date": task_date.isoformat(),
                "start_time": None,
                "end_time": None,
                "priority": infer_priority(segment),
                "task_type": infer_task_type(segment),
                "total_effort_minutes": None,
                "deadline": None,
                "spread": False,
            }
        )
    return tasks

def _extract_time_from_text(text: str):
    if not text:
        return None

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

    return None

def _task_duration_minutes(task: Task) -> int:
    if task.duration_minutes and task.duration_minutes > 0:
        return int(task.duration_minutes)
    if task.start_time and task.end_time:
        return max(
            15,
            int(
                (
                    datetime.combine(task.date, task.end_time) -
                    datetime.combine(task.date, task.start_time)
                ).total_seconds() // 60
            ),
        )
    return 60

def _build_schedule_context(db: Session, today: date, days: int = 7) -> str:
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

def _handle_followup_replan(message: str, db: Session) -> List[Task]:
    today = date.today()
    text = (message or "").lower()
    requested_time = _extract_time_from_text(message)

    exam_keywords = ["exam", "quiz", "test", "assessment"]
    revision_keywords = ["revision", "revise", "review", "study", "prep", "prepare"]
    session_keywords = ["session", "chapter", "practice"]

    upcoming = (
        db.query(Task)
        .filter(Task.date >= today, Task.completed == False)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.priority.desc())
        .all()
    )

    def _task_hay(task: Task) -> str:
        return f"{(task.title or '').lower()} {(task.description or '').lower()}"

    def _is_revision_task(task: Task) -> bool:
        hay = _task_hay(task)
        return any(k in hay for k in revision_keywords + session_keywords)

    def _is_exam_event_task(task: Task) -> bool:
        hay = _task_hay(task)
        has_exam_word = any(k in hay for k in exam_keywords)
        return has_exam_word and not _is_revision_task(task)

    exam_candidates = [t for t in upcoming if _is_exam_event_task(t)]
    exam_task = exam_candidates[0] if exam_candidates else None

    if not exam_task:
        # Fallback: no explicit exam task exists, infer target day from study/revision sessions.
        if not requested_time:
            return []

        revision_pool = [t for t in upcoming if _is_revision_task(t)]

        if not revision_pool:
            return []

        # Prefer the latest upcoming revision day (typically deadline/exam day).
        target_date = max(t.date for t in revision_pool)
        day_revisions = [
            t for t in revision_pool
            if t.date == target_date
        ]
        if not day_revisions:
            return []

        before_state = {
            t.id: (t.date, t.start_time, t.end_time)
            for t in db.query(Task).filter(Task.date == target_date, Task.completed == False).all()
        }

        rev = sorted(
            day_revisions,
            key=lambda t: (-t.priority, t.start_time or time(23, 59), t.id)
        )[0]
        rev_duration = _task_duration_minutes(rev)
        buffer_min = 5
        rev_end_dt = datetime.combine(target_date, requested_time) - timedelta(minutes=buffer_min)
        rev_start_dt = rev_end_dt - timedelta(minutes=rev_duration)
        day_start = datetime.combine(target_date, time(9, 0))
        if rev_start_dt < day_start:
            rev_start_dt = day_start
            rev_end_dt = rev_start_dt + timedelta(minutes=rev_duration)

        rev.start_time = rev_start_dt.time()
        rev.end_time = rev_end_dt.time()
        db.commit()
        db.refresh(rev)

        rebalance_day(db, target_date, pinned_task_ids={rev.id})

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

    # Snapshot day state so we can return only changed tasks.
    before_day_tasks = (
        db.query(Task)
        .filter(Task.date == exam_task.date, Task.completed == False)
        .all()
    )
    before_state = {
        t.id: (t.date, t.start_time, t.end_time)
        for t in before_day_tasks
    }

    # If user specified exam time in follow-up, update exam slot.
    if requested_time:
        duration = _task_duration_minutes(exam_task)
        exam_task.start_time = requested_time
        exam_task.end_time = (
            datetime.combine(exam_task.date, requested_time) +
            timedelta(minutes=duration)
        ).time()
        db.commit()
        db.refresh(exam_task)

    pinned_ids = {exam_task.id}
    wants_revision_adjust = any(k in text for k in revision_keywords)

    if wants_revision_adjust:
        # Candidate revision tasks on exam day.
        same_day = (
            db.query(Task)
            .filter(
                Task.date == exam_task.date,
                Task.completed == False,
                Task.id != exam_task.id,
            )
            .order_by(Task.priority.desc(), Task.start_time.asc(), Task.id.asc())
            .all()
        )

        revisions = [t for t in same_day if _is_revision_task(t)]
        if revisions:
            exam_start = exam_task.start_time or time(9, 0)
            buffer_min = 5

            # Move the highest-priority revision task to finish right before exam.
            rev = revisions[0]
            rev_duration = _task_duration_minutes(rev)
            rev_end_dt = datetime.combine(exam_task.date, exam_start) - timedelta(minutes=buffer_min)
            rev_start_dt = rev_end_dt - timedelta(minutes=rev_duration)
            day_start = datetime.combine(exam_task.date, time(9, 0))
            if rev_start_dt < day_start:
                rev_start_dt = day_start
                rev_end_dt = rev_start_dt + timedelta(minutes=rev_duration)

            rev.start_time = rev_start_dt.time()
            rev.end_time = rev_end_dt.time()
            db.commit()
            db.refresh(rev)
            pinned_ids.add(rev.id)

    # Rebalance around pinned tasks (at minimum the exam task).
    rebalance_day(db, exam_task.date, pinned_task_ids=pinned_ids)

    refreshed = (
        db.query(Task)
        .filter(Task.date == exam_task.date, Task.completed == False)
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

@app.post("/tasks", response_model=TaskResponse)
def create_task_endpoint(
    task: TaskCreate,
    db: Session = Depends(get_db)
):
    return create_task(
        db=db,
        title=task.title,
        description=task.description,
        date=task.date,
        start_time=task.start_time,
        end_time=task.end_time,
        priority=task.priority,
    )

@app.post("/mood", response_model=MoodResponse)
def create_mood_endpoint(
    mood: MoodCreate,
    db: Session = Depends(get_db)
):
    return create_mood(
        db=db,
        date=mood.date,
        energy=mood.energy,
        mood_label=mood.mood
    )
    

@app.post("/reflection", response_model=ReflectionResponse)
def create_reflection_endpoint(
    reflection: ReflectionCreate,
    db: Session = Depends(get_db)
):
    return create_reflection(
        reflection,
        db=db,
    )

@app.post("/chat", response_model=List[TaskResponse])
def chat_create_task_endpoint(
    input: ChatInput,
    db: Session = Depends(get_db)
):
    today = date.today()
    context = _build_schedule_context(db, today=today)
    model_input = (
        "Existing schedule context (use this for follow-up commands):\n"
        f"{context}\n\n"
        f"User message:\n{input.message}"
    )
    decision = scheduler_agent.decide(model_input)

    if decision.action != "create_task":
        replanned = _handle_followup_replan(input.message, db)
        if replanned:
            return replanned
        if decision.action == "get_schedule":
            return get_schedule(today, db)
        # Do not hard-fail on model action drift for follow-up commands.
        return []

    tasks_out = []
    args_list = decision.arguments

    if isinstance(args_list, dict):
        args_list = [args_list]

    fallback_multi = _split_tasks_from_message(input.message)
    if not args_list:
        args_list = fallback_multi
    elif len(args_list) == 1:
        only = args_list[0] or {}
        only_title = (only.get("title") or "").strip().lower()
        input_clean = input.message.strip().lower()
        if len(fallback_multi) > 1 and (not only_title or only_title == input_clean):
            args_list = fallback_multi

    # Track which dates need rebalancing and tasks that should stay fixed
    affected_dates = set()
    pinned_task_ids_by_date = {}

    for args in args_list or []:

        raw_text = args.get("title") or args.get("description") or input.message
        text = _title_from_text(raw_text)
        description = _description_from_text(args.get("description") or raw_text)
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

        today = date.today()

        # Parse explicit times when provided
        start_time_val = None
        end_time_val = None
        if isinstance(start_time_str, str) and start_time_str.strip():
            start_time_val = parse_time(start_time_str.strip())
        if isinstance(end_time_str, str) and end_time_str.strip():
            end_time_val = parse_time(end_time_str.strip())

        effective_date = task_date or today

        # -----------------------------
        # Duration fallback
        # -----------------------------
        if not total_effort:
            mood = db.query(Mood).filter(Mood.date == effective_date).first()
            energy = mood.energy if mood else 3

            historical_avg = get_historical_avg_duration(db, task_type, energy)

            total_effort = infer_duration_minutes(
                text=text,
                task_type=task_type,
                energy_level=energy,
                historical_avg=historical_avg,
                deadline_date=deadline_date
            )

        if start_time_val and not end_time_val and total_effort:
            end_time_val = (
                datetime.combine(effective_date, start_time_val) +
                timedelta(minutes=total_effort)
            ).time()

        # ======================================================
        # 🧠 SPREAD LOGIC
        # ======================================================
        if spread and deadline_date:

            if deadline_date <= today:
                session_date = today
                duration_min = total_effort

                task = create_task(
                    db=db,
                    title=f"{text} (Urgent)",
                    description=description,
                    date=session_date,
                    start_time=start_time_val,
                    end_time=end_time_val,
                    priority=priority + 1,
                    duration_minutes=duration_min
                )

                affected_dates.add(session_date)
                if start_time_val:
                    pinned_task_ids_by_date.setdefault(session_date, set()).add(task.id)
                tasks_out.append(task)
                continue

            days_available = (deadline_date - today).days
            days_to_plan = min(max(days_available, 1), 7)

            base_session = max(45, total_effort // days_to_plan)
            remaining = total_effort

            for i in range(days_to_plan):

                if remaining <= 0:
                    break

                session_date = today + timedelta(days=i)
                duration_min = min(base_session, remaining)
                remaining -= duration_min

                task = create_task(
                    db=db,
                    title=f"{text} (Session {i+1})",
                    description=description,
                    date=session_date,
                    start_time=None,
                    end_time=None,
                    priority=priority,
                    duration_minutes=duration_min
                )

                affected_dates.add(session_date)
                tasks_out.append(task)

            continue

        # ======================================================
        # 📅 SINGLE-DAY TASK
        # ======================================================

        if not task_date:
            task_date = today

        duration_min = total_effort

        task = create_task(
            db=db,
            title=text,
            description=description,
            date=task_date,
            start_time=start_time_val,
            end_time=end_time_val,
            priority=priority,
            duration_minutes=duration_min
        )

        affected_dates.add(task_date)
        if start_time_val:
            pinned_task_ids_by_date.setdefault(task_date, set()).add(task.id)
        tasks_out.append(task)

    # ======================================================
    # 🔥 REBALANCE AFTER ALL CREATIONS
    # ======================================================

    for d in affected_dates:
        rebalance_day(
            db,
            d,
            pinned_task_ids=pinned_task_ids_by_date.get(d, set()),
        )

    return tasks_out

@app.get("/tasks", response_model=List[TaskResponse])
def get_tasks_endpoint(task_date: date, db: Session = Depends(get_db)):
    return get_tasks(task_date, db)

@app.get("/mood/{date_given}", response_model=MoodResponse)
def get_mood_endpoint(date_given: date, db: Session = Depends(get_db)):
    return get_mood(date_given, db)

@app.get("/reflection/{date}", response_model=ReflectionResponse)
def get_reflection_endpoint(date: date, db: Session = Depends(get_db)):
    return get_reflection(date, db)

@app.get("/schedule/today", response_model=DailyScheduleResponse)
def get_today_schedule_endpoint(db: Session = Depends(get_db)):
    return get_today_schedule(db)

@app.get("/schedule/{schedule_date}", response_model=List[TaskResponse])
def get_schedule_endpoint(schedule_date: date, db: Session = Depends(get_db)):
    return get_schedule(schedule_date, db)

@app.patch("/tasks/{task_id}/complete", response_model=TaskResponse)
def complete_task_endpoint(task_id: int, actual_end: Optional[datetime] = None, db: Session = Depends(get_db)):
    return complete_task(task_id, actual_end, db)

@app.post("/tasks/{task_id}/reflect")
def reflect_on_task_endpoint(
    task_id: int,
    reflection: TaskReflectionInput,
    db: Session = Depends(get_db)
):
    return reflect_on_task(task_id, reflection, db)


@app.delete("/tasks/{task_id}")
def delete_task_endpoint(task_id: int, db: Session = Depends(get_db)):
    return delete_task(task_id, db)

@app.post("/reschedule/overdue")
def reschedule_overdue_tasks_endpoint(db: Session = Depends(get_db)):
    return reschedule_overdue_tasks(db)

@app.get("/summary")
def get_summary_endpoint(summary_date: date = Query(...), db: Session = Depends(get_db)):
    return get_summary(summary_date, db)
