from app.models import Mood, Task
from agents.scheduler_agent import scheduler_agent
from services.planner import get_historical_avg_duration, rebalance_day
from services.scheduling import infer_duration_minutes, parse_time
from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from app.db import get_db
from app.schemas import ChatInput, ChatMeta, ChatResponse, DailyScheduleResponse, MoodCreate, MoodResponse, ReflectionCreate, ReflectionResponse, TaskCreate, TaskResponse, TaskReflectionInput
from services.tools import create_mood, create_reflection, create_task, get_mood, get_tasks, get_reflection, get_today_schedule, get_schedule, complete_task, reflect_on_task, delete_task, reschedule_overdue_tasks, get_summary
from services.chat_helpers import (
    build_schedule_context,
    description_from_text,
    extract_time_from_text,
    handle_followup_replan,
    split_tasks_from_message,
    title_from_text,
)

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

@app.post("/chat", response_model=ChatResponse)
def chat_create_task_endpoint(
    input: ChatInput,
    db: Session = Depends(get_db)
):
    def _build_response(
        mode: str,
        message: str,
        created_tasks=None,
        updated_tasks=None,
        unchanged_tasks=None,
        used_fallback_parser: bool = False,
        used_replan_handler: bool = False,
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
        for task in created_tasks + updated_tasks + unchanged_tasks:
            if task.id in {t.id for t in unscheduled}:
                continue
            if (not task.start_time) or (not task.end_time):
                unscheduled.append(task)

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
                affected_dates=affected_dates,
                warnings=warnings,
            ),
        )

    def _collect_updated_tasks_for_dates(before_state_by_date, dates):
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

    today = date.today()
    context = build_schedule_context(db, today=today)
    model_input = (
        "Existing schedule context (use this for follow-up commands):\n"
        f"{context}\n\n"
        f"User message:\n{input.message}"
    )
    decision = scheduler_agent.decide(model_input)
    fallback_multi = split_tasks_from_message(input.message)
    used_fallback_parser = False

    if decision.action != "create_task":
        replanned = handle_followup_replan(input.message, db)
        if replanned:
            return _build_response(
                mode="replan",
                message="Updated existing tasks based on your follow-up request.",
                updated_tasks=replanned,
                used_replan_handler=True,
                affected_dates={t.date for t in replanned},
            )
        if decision.action == "get_schedule":
            tasks = get_schedule(today, db)
            return _build_response(
                mode="schedule",
                message="Here is your schedule.",
                unchanged_tasks=tasks,
                affected_dates={today},
            )
        # If model action drifts but input clearly contains task items, continue with local extraction.
        if not fallback_multi:
            return _build_response(
                mode="replan",
                message="No matching task updates were applied.",
                used_replan_handler=True,
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
        input_clean = input.message.strip().lower()
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

        raw_text = args.get("title") or args.get("description") or input.message
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

        today = date.today()

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
                f"{args.get('title') or ''} {args.get('description') or ''} {input.message}"
            )

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
                ensure_before_state(session_date)

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
                created_tasks.append(task)
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
                ensure_before_state(session_date)

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
                created_tasks.append(task)

            continue

        # ======================================================
        # 📅 SINGLE-DAY TASK
        # ======================================================

        if not task_date:
            task_date = today

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
            duration_minutes=duration_min
        )

        affected_dates.add(task_date)
        if start_time_val:
            pinned_task_ids_by_date.setdefault(task_date, set()).add(task.id)
        created_tasks.append(task)

    # ======================================================
    # 🔥 REBALANCE AFTER ALL CREATIONS
    # ======================================================

    for d in affected_dates:
        rebalance_day(
            db,
            d,
            pinned_task_ids=pinned_task_ids_by_date.get(d, set()),
        )

    updated_tasks = _collect_updated_tasks_for_dates(before_state_by_date, affected_dates)
    created_ids = {t.id for t in created_tasks}
    updated_tasks = [t for t in updated_tasks if t.id not in created_ids]

    return _build_response(
        mode="create",
        message="Created and scheduled tasks.",
        created_tasks=created_tasks,
        updated_tasks=updated_tasks,
        used_fallback_parser=used_fallback_parser,
        affected_dates=affected_dates,
    )

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
