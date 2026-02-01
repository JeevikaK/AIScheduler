from services.scheduling import extract_date_from_text, find_available_slot, find_time_slot, infer_duration_minutes
from fastapi import FastAPI
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ChatInput, TaskBase, MoodCreate, MoodResponse, ReflectionCreate, ReflectionResponse, NLTaskCreate, TaskCreate, TaskResponse
from app.models import Task, Mood, Reflection

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
def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    db_task = Task(**task.dict())
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task

@app.post("/tasks/nl", response_model=TaskResponse)
def create_task_from_nl(
    input: NLTaskCreate,
    db: Session = Depends(get_db)
):
    text = input.text

    task_date = extract_date_from_text(text)

    mood = db.query(Mood).filter(Mood.date == task_date).first()
    energy = mood.energy if mood else 3

    start_time, end_time = find_time_slot(task_date, db, energy)

    task = Task(
        title=text.capitalize(),
        date=task_date,
        start_time=start_time,
        end_time=end_time,
        completed=False
    )

    db.add(task)
    db.commit()
    db.refresh(task)

    return task

def infer_priority(text: str):
    text = text.lower()

    if any(word in text for word in ["urgent", "asap", "important", "critical"]):
        return 5
    if any(word in text for word in ["high priority", "must", "deadline"]):
        return 4
    if any(word in text for word in ["low priority", "optional", "if possible"]):
        return 2

    return 3

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

@app.post("/chat", response_model=TaskResponse)
def chat_create_task(
    input: ChatInput,
    db: Session = Depends(get_db)
):
    text = input.message

    # 1. Extract date from natural language
    task_date = extract_date_from_text(text)

    # 2. Fetch mood & energy (default = neutral)
    mood = db.query(Mood).filter(Mood.date == task_date).first()
    energy = mood.energy if mood else 3
    duration_min = infer_duration_minutes(text)
    # 3. Assign time slot based on energy
    priority = infer_priority(text)
    deadline = extract_date_from_text(text)

    start_time, end_time = find_available_slot(
        task_date,
        energy,
        db,
        duration_min,
        deadline
    )

    if not start_time:
        raise HTTPException(
            status_code=409,
            detail="No available time slot for this day"
        )

    # 4. Create task
    task = Task(
        title=text.capitalize(),
        date=task_date,
        start_time=start_time,
        end_time=end_time,
        completed=False,
        priority=priority,
        deadline=deadline
    )

    db.add(task)
    db.commit()
    db.refresh(task)

    return task

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
    
    today_mood = db.query(Mood).filter(Mood.date == schedule_date).first()
    energy = today_mood.energy if today_mood else 3  # default medium energy

    def task_priority(t):
    # Start datetime
        start = datetime.combine(t.date, t.start_time or time(9, 0))
        # End datetime (use end_time if exists, else 60 min duration)
        if t.end_time:
            end = datetime.combine(t.date, t.end_time)
        else:
            end = start + timedelta(minutes=60)
        duration = (end - start).total_seconds() / 60  # duration in minutes

        energy = t.energy if hasattr(t, 'energy') else 3  # fallback if needed

        # Energy-based adjustment
        if energy <= 2:
            return duration           # low energy → short tasks first
        elif energy >= 4:
            return -duration          # high energy → long tasks first
        else:
            return start.hour*60 + start.minute  # medium energy → keep original order

    # Sort tasks: incomplete first, then by priority
    tasks.sort(key=lambda t: (t.completed, task_priority(t)))
    
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
