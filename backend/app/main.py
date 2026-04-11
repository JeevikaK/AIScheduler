from agents.scheduler_agent import scheduler_agent
from fastapi import FastAPI, Depends, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import date, datetime
from pathlib import Path
from sqlalchemy.orm import Session
from app.db import get_db
from app.schemas import ChatInput, ChatResponse, ChatThreadCreateResponse, ChatThreadDetailResponse, ChatThreadSummaryResponse, DailyScheduleResponse, DashboardOverviewResponse, MoodCreate, MoodResponse, ReflectionCreate, ReflectionResponse, TaskCreate, TaskResponse, TaskReflectionInput
from services.tools import create_chat_thread, create_mood, create_reflection, create_task, delete_chat_thread, get_chat_thread_detail, get_chat_threads, get_dashboard_overview, get_mood, get_tasks, get_reflection, get_today_schedule, get_schedule, complete_task, reflect_on_task, delete_task, clear_today_tasks, reschedule_overdue_tasks, get_summary
from services.chat_helpers import (
    process_chat_request,
)

app = FastAPI(title="AI Life Planner")
STATIC_DIR = Path(__file__).resolve().parent / "static"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/")
def serve_frontend():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/todayschats")
@app.get("/recentactivities")
@app.get("/insights")
def serve_frontend_routes():
    return FileResponse(STATIC_DIR / "index.html")

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
    return process_chat_request(
        message=input.message,
        db=db,
        decide_fn=scheduler_agent.decide,
        chat_thread_id=input.chat_thread_id,
        thread_date=input.thread_date,
        pending_response=input.pending_response,
        confirm=input.confirm,
    )

@app.get("/chat/threads", response_model=List[ChatThreadSummaryResponse])
def get_chat_threads_endpoint(thread_date: date = Query(...), db: Session = Depends(get_db)):
    return get_chat_threads(thread_date, db)

@app.post("/chat/threads", response_model=ChatThreadCreateResponse)
def create_chat_thread_endpoint(thread_date: date = Query(...), db: Session = Depends(get_db)):
    return create_chat_thread(thread_date, db)

@app.get("/chat/threads/{chat_thread_id}", response_model=ChatThreadDetailResponse)
def get_chat_thread_detail_endpoint(chat_thread_id: str, thread_date: date = Query(...), db: Session = Depends(get_db)):
    return get_chat_thread_detail(thread_date, chat_thread_id, db)

@app.delete("/chat/threads/{chat_thread_id}")
def delete_chat_thread_endpoint(chat_thread_id: str, thread_date: date = Query(...), db: Session = Depends(get_db)):
    return delete_chat_thread(thread_date, chat_thread_id, db)

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


@app.delete("/tasks/clear/today")
def clear_today_tasks_endpoint(db: Session = Depends(get_db)):
    return clear_today_tasks(db)

@app.post("/reschedule/overdue")
def reschedule_overdue_tasks_endpoint(db: Session = Depends(get_db)):
    return reschedule_overdue_tasks(db)

@app.get("/summary")
def get_summary_endpoint(summary_date: date = Query(...), db: Session = Depends(get_db)):
    return get_summary(summary_date, db)


@app.get("/dashboard/overview", response_model=DashboardOverviewResponse)
def get_dashboard_overview_endpoint(db: Session = Depends(get_db)):
    return get_dashboard_overview(db)
