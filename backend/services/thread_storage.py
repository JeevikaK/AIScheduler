from __future__ import annotations

from datetime import date
import re

from sqlalchemy.orm import Session

from app.models import ConversationMessage, ConversationThread


THREAD_KEY_RE = re.compile(r"^(?P<chat_thread_id>[^:]+):(?P<thread_date>\d{4}-\d{2}-\d{2})$")


def derive_thread_identity(
    thread_key: str,
    fallback_thread_date: date | None = None,
    fallback_chat_thread_id: str | None = None,
) -> tuple[str | None, date]:
    match = THREAD_KEY_RE.match((thread_key or "").strip())
    if match:
        return match.group("chat_thread_id"), date.fromisoformat(match.group("thread_date"))
    return fallback_chat_thread_id, fallback_thread_date or date.today()


def repair_thread_storage_consistency(db: Session) -> bool:
    changed = False

    for rec in db.query(ConversationThread).all():
        derived_chat_thread_id, derived_thread_date = derive_thread_identity(
            rec.thread_key,
            fallback_thread_date=rec.thread_date,
            fallback_chat_thread_id=rec.chat_thread_id,
        )
        if rec.chat_thread_id != derived_chat_thread_id:
            rec.chat_thread_id = derived_chat_thread_id
            changed = True
        if rec.thread_date != derived_thread_date:
            rec.thread_date = derived_thread_date
            changed = True

    for rec in db.query(ConversationMessage).all():
        derived_chat_thread_id, derived_thread_date = derive_thread_identity(
            rec.thread_key,
            fallback_thread_date=rec.thread_date,
            fallback_chat_thread_id=rec.chat_thread_id,
        )
        if rec.chat_thread_id != derived_chat_thread_id:
            rec.chat_thread_id = derived_chat_thread_id
            changed = True
        if rec.thread_date != derived_thread_date:
            rec.thread_date = derived_thread_date
            changed = True

    if changed:
        db.commit()

    return changed
