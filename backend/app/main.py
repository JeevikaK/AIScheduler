import re
from dateparser.search import search_dates
from fastapi import FastAPI
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from db import get_db
from models import TaskBase, Task, Mood, Reflection, NLTaskCreate

app = FastAPI(title="AI Life Planner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class TaskCreate(TaskBase):
    pass

class TaskResponse(TaskBase):
    id: int
    completed: bool

    class Config:
        orm_mode = True

class MoodCreate(BaseModel):
    date: date
    mood: int
    energy: int
    note: Optional[str] = None

class MoodResponse(MoodCreate):
    id: int

    class Config:
        orm_mode = True

class ReflectionCreate(BaseModel):
    date: date
    text: str

class ReflectionResponse(ReflectionCreate):
    id: int

    class Config:
        orm_mode = True


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


@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/tasks", response_model=TaskResponse)
def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    db_task = Task(**task.dict())
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task

@app.post("/tasks/nl", response_model=TaskResponse)
def create_task_nl(nl_task: NLTaskCreate, db: Session = Depends(get_db)):
    text = nl_task.text
    results = search_dates(
        text,
        settings={
            'PREFER_DATES_FROM': 'future',
            'RELATIVE_BASE': datetime.now(), # Use current time for better relative accuracy
            'TIMEZONE': 'America/Los_Angeles',
            'RETURN_AS_TIMEZONE_AWARE': False
        }
    )

    if results:
        # results[0][1] is the datetime object of the first date found
        parsed_dt = results[0][1]
        task_date = parsed_dt.date()
    else:
        # Fallback to today if no date is mentioned
        task_date = date.today()

    start_time, end_time = find_time_slot(task_date, db)
    title = text[:50]

    db_task = Task(
        title=title,
        date=task_date,
        start_time=start_time,
        end_time=end_time
    )
    print(text, "->", task_date)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task

@app.post("/mood", response_model=MoodResponse)
def create_mood(mood: MoodCreate, db: Session = Depends(get_db)):
    existing = db.query(Mood).filter(Mood.date == mood.date).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Mood for this date already exists. Use PUT to update."
        )

    db_mood = Mood(**mood.dict())
    db.add(db_mood)
    db.commit()
    db.refresh(db_mood)
    return db_mood

@app.post("/reflection", response_model=ReflectionResponse)
def create_reflection(
    reflection: ReflectionCreate,
    db: Session = Depends(get_db)
):
    db_reflection = Reflection(**reflection.dict())
    db.add(db_reflection)
    db.commit()
    db.refresh(db_reflection)
    return db_reflection

@app.get("/tasks", response_model=List[TaskResponse])
def get_tasks(task_date: date, db: Session = Depends(get_db)):
    return db.query(Task).filter(Task.date == task_date).all()

@app.get("/mood/{date_given}", response_model=MoodResponse)
def get_mood(date_given: date, db: Session = Depends(get_db)):

    db_mood = db.query(Mood).filter(Mood.date == date_given).first()
    if not db_mood:
        raise HTTPException(status_code=404, detail="Mood not found")
    return db_mood

@app.get("/reflection/{date}", response_model=ReflectionResponse)
def get_reflection(date: date, db: Session = Depends(get_db)):
    reflection = db.query(Reflection).filter(Reflection.date == date).first()
    if not reflection:
        raise HTTPException(status_code=404, detail="Reflection not found")
    return reflection

@app.get("/schedule/{schedule_date}", response_model=List[TaskResponse])
def get_schedule(schedule_date: date, db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.date == schedule_date).all()
    # simple prioritization: incomplete first
    tasks.sort(key=lambda t: (t.completed, t.start_time or datetime.min.time()))
    return tasks

@app.get("/reminders/{reminder_date}", response_model=List[TaskResponse])
def get_reminders(reminder_date: date, db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.date == reminder_date, Task.completed == False).all()
    tasks.sort(key=lambda t: t.start_time or datetime.min.time())
    return tasks

@app.put("/tasks/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: int,
    task: TaskCreate,
    db: Session = Depends(get_db),
):
    db_task = db.query(Task).filter(Task.id == task_id).first()

    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    for key, value in task.dict().items():
        setattr(db_task, key, value)

    db.commit()
    db.refresh(db_task)
    return db_task

@app.patch("/tasks/{task_id}/complete", response_model=TaskResponse)
def complete_task(task_id: int, db: Session = Depends(get_db)):
    db_task = db.query(Task).filter(Task.id == task_id).first()

    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    db_task.completed = True
    db.commit()
    db.refresh(db_task)
    return db_task

@app.delete("/tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    db_task = db.query(Task).filter(Task.id == task_id).first()

    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    db.delete(db_task)
    db.commit()
    return {"detail": "Task deleted successfully"}
