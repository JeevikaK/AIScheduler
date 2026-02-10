from http.client import HTTPException
from app.models import Mood, Task
from agents.scheduler_agent import scheduler_agent
from agents.scheduler_executor import execute_decision
from services.planner import get_historical_avg_duration, infer_task_type
from services.scheduling import extract_date_from_text, find_available_slot, infer_duration_minutes
from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import date, datetime, timedelta
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

@app.post("/chat", response_model=TaskResponse)
def chat_create_task_endpoint(
    input: ChatInput,
    db: Session = Depends(get_db)
):
    # Ask the agent
    decision = scheduler_agent.decide(input.message)
    
    if decision.action == "create_task":
        args = decision.arguments or {}

        task_date_str = args.get("date")
        task_date = datetime.strptime(task_date_str, "%Y-%m-%d").date()

        start_time_str = args.get("start_time")  # e.g., "18:00"
        start_time = datetime.strptime(start_time_str, "%H:%M").time() if start_time_str else None
        end_time_str = args.get("end_time")  # e.g., "19:00"
        end_time = datetime.strptime(end_time_str, "%H:%M").time() if end_time_str else None

        task = create_task(
            db=db,
            title=args.get("title", input.message),
            date=task_date,
            start_time=start_time,
            end_time=end_time,
            priority=args.get("priority", 1)
        )
        return task
    
    elif decision.action == "respond":
        # LLM fallback response
        raise HTTPException(status_code=400, detail=decision.message)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action {decision.action}")

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