from services.planner import get_historical_avg_duration, get_overdue_tasks, infer_task_type, reschedule_task, update_duration_model
from services.scheduling import extract_date_from_text, find_available_slot, find_time_slot, infer_duration_minutes
from fastapi import FastAPI, Query
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ChatInput, DailyScheduleResponse, TaskBase, MoodCreate, MoodResponse, ReflectionCreate, ReflectionResponse, TaskCreate, TaskResponse, TaskReflectionInput
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

    # 1. Extract execution date from NL
    task_date = extract_date_from_text(text)

    # 2. Fetch mood & energy
    mood = db.query(Mood).filter(Mood.date == task_date).first()
    energy = mood.energy if mood else 3

    # 3. Duration inference
    task_type = infer_task_type(text)
    historical_avg = get_historical_avg_duration(db, task_type, energy)
    duration_min = infer_duration_minutes(
        text=text,
        task_type=task_type,
        historical_avg=historical_avg,
    )

    # 4. Priority & deadline
    priority = infer_priority(text)
    deadline = extract_date_from_text(text)

    # 5. Scheduling (find best available slot)
    start_time, end_time = find_available_slot(
        task_date=task_date,
        energy=energy,
        db=db,
        duration_min=duration_min,
        deadline=deadline,
        priority=priority,
    )

    # fallback: if slot only returned start_time
    if start_time == end_time:
        end_time = (
            datetime.combine(task_date, start_time)
            + timedelta(minutes=duration_min)
        ).time()

    if not start_time:
        raise HTTPException(
            status_code=409,
            detail="No available time slot for this day"
        )

    # 6. Create task in DB
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

@app.get("/schedule/today", response_model=DailyScheduleResponse)
def get_today_schedule(db: Session = Depends(get_db)):
    today = date.today()

    # 1. Auto-reschedule overdue tasks
    overdue_tasks = get_overdue_tasks(db)
    for task in overdue_tasks:
        reschedule_task(task, db)

    # 2. Fetch today's mood / energy
    mood = db.query(Mood).filter(Mood.date == today).first()
    energy = mood.energy if mood else 3

    # 3. Fetch today's tasks
    tasks = (
        db.query(Task)
        .filter(Task.date == today)
        .order_by(Task.priority.desc(), Task.start_time)
        .all()
    )

    # 4. Agent-style summary message
    if energy <= 2:
        message = "Low-energy day detected. Focus on light or high-priority tasks."
    elif energy >= 4:
        message = "High-energy day! Great time for deep or important work."
    else:
        message = "Balanced day ahead. Stay consistent."

    return {
        "date": today,
        "energy": energy,
        "message": message,
        "tasks": tasks,
    }

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

@app.patch("/tasks/{task_id}/complete", response_model=TaskResponse)
def complete_task(task_id: int, actual_end: Optional[datetime] = None, db: Session = Depends(get_db)):
    db_task = db.query(Task).filter(Task.id == task_id).first()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    db_task.completed = True

    # Compute actual duration
    if db_task.start_time and actual_end:
        start_dt = datetime.combine(db_task.date, db_task.start_time)
        duration_min = int((actual_end - start_dt).total_seconds() / 60)
        db_task.actual_duration = duration_min

    db.commit()
    db.refresh(db_task)
    return db_task

@app.post("/tasks/{task_id}/reflect")
def reflect_on_task(
    task_id: int,
    reflection: TaskReflectionInput,
    db: Session = Depends(get_db)
):
    task = db.query(Task).filter(Task.id == task_id).first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.completed = reflection.completed
    task.actual_duration = reflection.actual_duration
    task.reflection_energy = reflection.energy
    task.reflection_mood = reflection.mood

    db.commit()

    update_duration_model(db, task)

    return {"status": "reflection recorded"}


@app.delete("/tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    db_task = db.query(Task).filter(Task.id == task_id).first()

    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    db.delete(db_task)
    db.commit()
    return {"detail": "Task deleted successfully"}

@app.post("/reschedule/overdue")
def reschedule_overdue_tasks(db: Session = Depends(get_db)):
    overdue_tasks = get_overdue_tasks(db)

    rescheduled = 0
    failed = []

    for task in overdue_tasks:
        success = reschedule_task(task, db)
        if success:
            rescheduled += 1
        else:
            failed.append(task.id)

    return {
        "rescheduled": rescheduled,
        "failed_tasks": failed,
    }

@app.get("/summary")
def get_summary(summary_date: date = Query(...), db: Session = Depends(get_db)):
    # Fetch tasks for the day
    tasks = db.query(Task).filter(Task.date == summary_date).all()
    total_tasks = len(tasks)
    completed_tasks = len([t for t in tasks if t.completed])
    completion_rate = total_tasks and completed_tasks / total_tasks or 0

    # Fetch mood for the day
    mood_entry = db.query(Mood).filter(Mood.date == summary_date).first()
    mood = mood_entry.mood if mood_entry else None
    energy = mood_entry.energy if mood_entry else None

    # Optional: fetch reflection
    reflection_entry = db.query(Reflection).filter(Reflection.date == summary_date).first()
    reflection = reflection_entry.text if reflection_entry else None

    return {
        "date": summary_date,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "completion_rate": completion_rate,
        "mood": mood,
        "energy": energy,
        "reflection": reflection,
    }
