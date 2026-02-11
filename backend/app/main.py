from app.models import Mood, Task
from agents.scheduler_agent import scheduler_agent
from agents.scheduler_executor import execute_decision
from services.planner import get_historical_avg_duration, infer_task_type
from services.scheduling import extract_date_from_text, find_available_slot, infer_duration_minutes
from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import date, datetime, time, timedelta
from sqlalchemy.orm import Session
from app.db import get_db
from app.schemas import ChatInput, DailyScheduleResponse, MoodCreate, MoodResponse, ReflectionCreate, ReflectionResponse, TaskCreate, TaskResponse, TaskReflectionInput
from services.tools import chat_create_task, create_mood, create_reflection, create_task, get_mood, get_tasks, get_reflection, get_today_schedule, get_schedule, complete_task, infer_priority, reflect_on_task, delete_task, reschedule_overdue_tasks, get_summary

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
    decision = scheduler_agent.decide(input.message)

    if decision.action != "create_task":
        raise HTTPException(status_code=400, detail=decision.message or "Unsupported action")

    tasks_out = []
    args_list = decision.arguments

    if isinstance(args_list, dict):
        args_list = [args_list]

    for args in args_list or []:
        # 1. Date
        task_date_str = args.get("date")
        task_date = datetime.strptime(task_date_str, "%Y-%m-%d").date()

        # 2. Start & end times
        start_time_str = args.get("start_time")
        end_time_str = args.get("end_time")

        if start_time_str and end_time_str:
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
            end_time = datetime.strptime(end_time_str, "%H:%M").time()
        else:
            text = args.get("title", input.message)
            mood = db.query(Mood).filter(Mood.date == task_date).first()
            energy = mood.energy if mood else 3

            task_type = infer_task_type(text)
            historical_avg = get_historical_avg_duration(db, task_type, energy)
            duration_min = infer_duration_minutes(
                text=text,
                task_type=task_type,
                historical_avg=historical_avg,
            )

            if start_time and not end_time:
                # compute end time from duration
                duration_min = 60  # or infer based on task type
                end_time = (
                    datetime.combine(task_date, start_time) +
                    timedelta(minutes=duration_min)
                ).time()

            if not start_time:
                # Let scheduler find optimal slot
                duration_min = 60  # or infer properly

                start_time, end_time = find_available_slot(
                    task_date=task_date,
                    energy=3,  # or fetch mood energy
                    db=db,
                    duration_min=duration_min,
                    deadline=None,
                    priority=args.get("priority", 1),
                )

            # fallback if only start_time returned
            if start_time == end_time:
                end_time = (datetime.combine(task_date, start_time) + timedelta(minutes=duration_min)).time()

            if not start_time:
                # Could not find slot, skip task or set full-day
                start_time = time(0, 0)
                end_time = time(23, 59)

        # 3. Create the task
        task = create_task(
            db=db,
            title=args.get("title", input.message),
            description=args.get("description"),
            date=task_date,
            start_time=start_time,
            end_time=end_time,
            priority=args.get("priority", 1)
        )
        tasks_out.append(task)

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