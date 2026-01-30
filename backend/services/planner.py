from fastapi import Depends, Query
from app.models import Task, Mood, Reflection
from app.db import get_db
import app
from datetime import date
from sqlalchemy.orm import Session

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

@app.post("/replan")
def replan_day(summary_date: date = Query(...), db: Session = Depends(get_db)):
    """
    Placeholder for AI replanning.
    Later, this will:
    - Read tasks, mood, reflections
    - Adjust schedule based on mood/energy
    - Return new suggested plan
    """
    # Fetch current tasks for reference
    tasks = db.query(Task).filter(Task.date == summary_date).all()
    tasks_data = [
        {"id": t.id, "title": t.title, "completed": t.completed} for t in tasks
    ]

    return {
        "date": summary_date,
        "message": "AI replanning not implemented yet",
        "current_tasks": tasks_data,
        "suggested_plan": None,
    }
