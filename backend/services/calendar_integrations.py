import json
import os
import secrets
import re
from html import unescape
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import ConnectedAccount, ConnectedCalendar, ExternalEvent, IntegrationAuthSession, Task
from services.app_settings import get_integration_settings


GOOGLE_PROVIDER = "google"
MICROSOFT_PROVIDER = "microsoft"
SYNC_WINDOW_DAYS_BACK = 365
SYNC_WINDOW_DAYS_FORWARD = 365


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    label: str
    client_id: str | None
    client_secret: str | None
    authorize_url: str
    token_url: str
    scopes: list[str]
    default_redirect_path: str
    tenant_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {})


def _app_base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _provider_config(provider: str, db: Session | None = None) -> ProviderConfig:
    saved = get_integration_settings(db) if db is not None else {}
    app_base_url = (saved.get("app_base_url") or os.getenv("APP_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    microsoft_tenant = saved.get("microsoft_tenant_id") or os.getenv("MICROSOFT_TENANT_ID", "common")
    configs = {
        GOOGLE_PROVIDER: ProviderConfig(
            provider=GOOGLE_PROVIDER,
            label="Google Calendar",
            client_id=saved.get("google_client_id") or os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=saved.get("google_client_secret") or os.getenv("GOOGLE_CLIENT_SECRET"),
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=[
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
            default_redirect_path=f"{app_base_url}/integrations/google/callback",
        ),
        MICROSOFT_PROVIDER: ProviderConfig(
            provider=MICROSOFT_PROVIDER,
            label="Outlook / Microsoft 365",
            client_id=saved.get("microsoft_client_id") or os.getenv("MICROSOFT_CLIENT_ID"),
            client_secret=saved.get("microsoft_client_secret") or os.getenv("MICROSOFT_CLIENT_SECRET"),
            authorize_url=(
                f"https://login.microsoftonline.com/"
                f"{microsoft_tenant}/oauth2/v2.0/authorize"
            ),
            token_url=(
                f"https://login.microsoftonline.com/"
                f"{microsoft_tenant}/oauth2/v2.0/token"
            ),
            scopes=["offline_access", "User.Read", "Calendars.Read"],
            default_redirect_path=f"{app_base_url}/integrations/microsoft/callback",
            tenant_id=microsoft_tenant,
        ),
    }
    if provider not in configs:
        raise HTTPException(status_code=404, detail="Unsupported integration provider")
    return configs[provider]


def list_available_providers(db: Session | None = None) -> list[dict[str, Any]]:
    providers = []
    for provider in (GOOGLE_PROVIDER, MICROSOFT_PROVIDER):
        config = _provider_config(provider, db)
        providers.append(
            {
                "provider": config.provider,
                "label": config.label,
                "configured": config.configured,
                "auth_path": f"/integrations/{config.provider}/start",
                "status": "available" if config.configured else "not_configured",
            }
        )
    return providers


def start_provider_auth(provider: str, return_path: str | None, db: Session) -> dict[str, Any]:
    config = _provider_config(provider, db)
    if not config.configured:
        raise HTTPException(status_code=400, detail=f"{config.label} is not configured on this server")

    state = secrets.token_urlsafe(24)
    redirect_path = return_path or "/recentactivities"
    db.add(
        IntegrationAuthSession(
            provider=provider,
            state=state,
            return_path=redirect_path,
        )
    )
    db.commit()

    if provider == GOOGLE_PROVIDER:
        query = {
            "client_id": config.client_id,
            "redirect_uri": config.default_redirect_path,
            "response_type": "code",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "scope": " ".join(config.scopes),
            "state": state,
        }
    else:
        query = {
            "client_id": config.client_id,
            "redirect_uri": config.default_redirect_path,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(config.scopes),
            "state": state,
        }

    return {
        "provider": provider,
        "authorization_url": f"{config.authorize_url}?{urlencode(query)}",
    }


def handle_provider_callback(provider: str, state: str, code: str, db: Session) -> tuple[str, int]:
    config = _provider_config(provider, db)
    auth_session = (
        db.query(IntegrationAuthSession)
        .filter(IntegrationAuthSession.provider == provider, IntegrationAuthSession.state == state)
        .first()
    )
    if not auth_session:
        raise HTTPException(status_code=400, detail="Invalid integration auth session")

    token_payload = _exchange_code_for_tokens(config, code)
    account_info = _fetch_provider_account_info(config, token_payload["access_token"])
    account = _upsert_connected_account(db, config, account_info, token_payload)
    calendars = _list_provider_calendars(config, account.access_token)
    _upsert_connected_calendars(db, account, calendars)
    _ensure_default_calendar_selection(db, account)

    db.delete(auth_session)
    db.commit()
    return auth_session.return_path, account.id


def list_connected_accounts(db: Session) -> list[dict[str, Any]]:
    accounts = (
        db.query(ConnectedAccount)
        .order_by(ConnectedAccount.provider.asc(), ConnectedAccount.email.asc(), ConnectedAccount.id.asc())
        .all()
    )
    results = []
    for account in accounts:
        _ensure_default_calendar_selection(db, account)
        calendars = (
            db.query(ConnectedCalendar)
            .filter(ConnectedCalendar.account_id == account.id)
            .order_by(ConnectedCalendar.calendar_name.asc())
            .all()
        )
        last_synced = (
            db.query(ExternalEvent.last_synced_at)
            .filter(ExternalEvent.account_id == account.id)
            .order_by(ExternalEvent.last_synced_at.desc())
            .first()
        )
        results.append(
            {
                "id": account.id,
                "provider": account.provider,
                "email": account.email,
                "external_account_id": account.external_account_id,
                "connection_status": account.connection_status,
                "last_synced_at": last_synced[0] if last_synced else None,
                "calendars": [
                    {
                        "id": calendar.id,
                        "provider_calendar_id": calendar.provider_calendar_id,
                        "calendar_name": calendar.calendar_name,
                        "color": calendar.color,
                        "selected_for_sync": calendar.selected_for_sync,
                        "last_synced_at": _calendar_last_synced(db, calendar.id),
                    }
                    for calendar in calendars
                ],
            }
        )
    return results


def select_account_calendars(account_id: int, calendar_ids: list[str], sync_immediately: bool, db: Session) -> dict[str, Any]:
    account = db.query(ConnectedAccount).filter(ConnectedAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Connected account not found")

    selected_set = set(calendar_ids)
    calendars = db.query(ConnectedCalendar).filter(ConnectedCalendar.account_id == account.id).all()
    selected_count = 0
    for calendar in calendars:
        calendar.selected_for_sync = calendar.provider_calendar_id in selected_set
        calendar.updated_at = _utcnow()
        if calendar.selected_for_sync:
            selected_count += 1
    db.commit()

    if sync_immediately and selected_count:
        return sync_connected_account(account_id, db)

    return {
        "account_id": account.id,
        "provider": account.provider,
        "synced_events": 0,
        "selected_calendars": selected_count,
        "status": "ok",
    }


def sync_connected_account(account_id: int, db: Session) -> dict[str, Any]:
    account = db.query(ConnectedAccount).filter(ConnectedAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Connected account not found")

    _ensure_default_calendar_selection(db, account)
    config = _provider_config(account.provider, db)
    access_token = _ensure_valid_access_token(config, account, db)
    calendars = (
        db.query(ConnectedCalendar)
        .filter(ConnectedCalendar.account_id == account.id, ConnectedCalendar.selected_for_sync == True)
        .all()
    )
    total_synced = 0
    for calendar in calendars:
        total_synced += _sync_single_calendar(config, account, calendar, access_token, db)

    account.updated_at = _utcnow()
    db.commit()
    return {
        "account_id": account.id,
        "provider": account.provider,
        "synced_events": total_synced,
        "selected_calendars": len(calendars),
        "status": "ok",
    }


def disconnect_connected_account(account_id: int, db: Session) -> dict[str, Any]:
    account = db.query(ConnectedAccount).filter(ConnectedAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Connected account not found")

    calendar_ids = [
        item.id
        for item in db.query(ConnectedCalendar.id).filter(ConnectedCalendar.account_id == account.id).all()
    ]
    if calendar_ids:
        (
            db.query(ExternalEvent)
            .filter(ExternalEvent.connected_calendar_id.in_(calendar_ids))
            .delete(synchronize_session=False)
        )
        (
            db.query(ConnectedCalendar)
            .filter(ConnectedCalendar.account_id == account.id)
            .delete(synchronize_session=False)
        )
    db.delete(account)
    db.commit()
    return {"detail": "Disconnected calendar account", "account_id": account_id}


def get_week_calendar_data(start_date: date, db: Session, timezone_name: str | None = None) -> dict[str, Any]:
    days = [start_date + timedelta(days=index) for index in range(7)]
    end_date = days[-1]
    local_timezone = _resolve_calendar_timezone(timezone_name)
    utc_start = _local_day_boundary_to_utc_naive(start_date, local_timezone)
    utc_end = _local_day_boundary_to_utc_naive(end_date + timedelta(days=1), local_timezone)

    local_tasks = (
        db.query(Task)
        .filter(Task.date >= start_date, Task.date <= end_date)
        .order_by(Task.date.asc(), Task.start_time.asc(), Task.id.asc())
        .all()
    )
    external_events = (
        db.query(ExternalEvent, ConnectedAccount.email)
        .join(ConnectedCalendar, ConnectedCalendar.id == ExternalEvent.connected_calendar_id)
        .join(ConnectedAccount, ConnectedAccount.id == ExternalEvent.account_id)
        .filter(
            ExternalEvent.cancelled == False,
            ExternalEvent.start_at < utc_end,
            ExternalEvent.end_at >= utc_start,
        )
        .order_by(ExternalEvent.start_at.asc(), ExternalEvent.id.asc())
        .all()
    )

    task_map: dict[date, list[Any]] = {day: [] for day in days}
    external_map: dict[date, list[dict[str, Any]]] = {day: [] for day in days}

    for task in local_tasks:
        task_map.setdefault(task.date, []).append(task)

    for event, account_email in external_events:
        event_date = _utc_naive_to_local(event.start_at, local_timezone).date()
        if event_date not in external_map:
            continue
        external_map[event_date].append(
            {
                "id": event.id,
                "account_id": event.account_id,
                "connected_calendar_id": event.connected_calendar_id,
                "provider": event.provider,
                "provider_event_id": event.provider_event_id,
                "title": event.title,
                "description": event.description,
                "location": event.location,
                "start_at": _utc_naive_to_aware(event.start_at),
                "end_at": _utc_naive_to_aware(event.end_at),
                "timezone": event.timezone,
                "all_day": event.all_day,
                "status": event.status,
                "cancelled": event.cancelled,
                "readonly": True,
                "calendar_label": event.calendar_label,
                "account_email": account_email,
                "color": _event_color(event.provider, event.calendar_label),
                "source_url": event.source_url,
                "organizer": (_json_loads(event.event_meta_json, {}) or {}).get("organizer"),
                "attendee_count": int(((_json_loads(event.event_meta_json, {}) or {}).get("attendee_count")) or 0),
            }
        )

    return {
        "start_date": start_date,
        "days": [
            {
                "date": day,
                "tasks": task_map.get(day, []),
                "external_events": external_map.get(day, []),
            }
            for day in days
        ],
    }


def _event_color(provider: str, calendar_label: str | None) -> str:
    if provider == GOOGLE_PROVIDER:
        return "#4ba3ff"
    if provider == MICROSOFT_PROVIDER:
        return "#7b9cff"
    return "#99a6cc"


def _calendar_last_synced(db: Session, calendar_id: int) -> datetime | None:
    row = (
        db.query(ExternalEvent.last_synced_at)
        .filter(ExternalEvent.connected_calendar_id == calendar_id)
        .order_by(ExternalEvent.last_synced_at.desc())
        .first()
    )
    return row[0] if row else None


def _exchange_code_for_tokens(config: ProviderConfig, code: str) -> dict[str, Any]:
    payload = {
        "code": code,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "redirect_uri": config.default_redirect_path,
        "grant_type": "authorization_code",
    }
    data = _request_json(
        "POST",
        config.token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    return _normalize_token_payload(data)


def _refresh_access_token(config: ProviderConfig, account: ConnectedAccount) -> dict[str, Any]:
    if not account.refresh_token:
        raise HTTPException(status_code=400, detail=f"{config.label} refresh token is missing")

    payload = {
        "refresh_token": account.refresh_token,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "grant_type": "refresh_token",
    }
    if config.provider == MICROSOFT_PROVIDER:
        payload["scope"] = " ".join(config.scopes)

    data = _request_json(
        "POST",
        config.token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    return _normalize_token_payload(data)


def _normalize_token_payload(data: dict[str, Any]) -> dict[str, Any]:
    expires_in = int(data.get("expires_in") or 0)
    expiry = _utcnow() + timedelta(seconds=expires_in) if expires_in else None
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "token_expiry": expiry,
        "raw": data,
    }


def _fetch_provider_account_info(config: ProviderConfig, access_token: str) -> dict[str, str | None]:
    if config.provider == GOOGLE_PROVIDER:
        data = _authorized_json(
            "GET",
            "https://www.googleapis.com/oauth2/v2/userinfo",
            access_token=access_token,
        )
        return {
            "external_account_id": data.get("id") or data.get("email"),
            "email": data.get("email"),
        }

    data = _authorized_json(
        "GET",
        "https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName",
        access_token=access_token,
    )
    return {
        "external_account_id": data.get("id") or data.get("userPrincipalName"),
        "email": data.get("mail") or data.get("userPrincipalName"),
    }


def _list_provider_calendars(config: ProviderConfig, access_token: str) -> list[dict[str, Any]]:
    if config.provider == GOOGLE_PROVIDER:
        data = _authorized_json(
            "GET",
            "https://www.googleapis.com/calendar/v3/users/me/calendarList",
            access_token=access_token,
        )
        return [
            {
                "provider_calendar_id": item["id"],
                "calendar_name": item.get("summary") or "Calendar",
                "color": item.get("backgroundColor"),
            }
            for item in data.get("items", [])
        ]

    data = _authorized_json(
        "GET",
        "https://graph.microsoft.com/v1.0/me/calendars?$select=id,name,color",
        access_token=access_token,
    )
    return [
        {
            "provider_calendar_id": item["id"],
            "calendar_name": item.get("name") or "Calendar",
            "color": item.get("color"),
        }
        for item in data.get("value", [])
    ]


def _upsert_connected_account(
    db: Session,
    config: ProviderConfig,
    account_info: dict[str, str | None],
    token_payload: dict[str, Any],
) -> ConnectedAccount:
    account = (
        db.query(ConnectedAccount)
        .filter(
            ConnectedAccount.provider == config.provider,
            ConnectedAccount.external_account_id == account_info["external_account_id"],
        )
        .first()
    )
    if account is None:
        account = ConnectedAccount(
            provider=config.provider,
            external_account_id=account_info["external_account_id"],
            email=account_info.get("email"),
            access_token=token_payload["access_token"],
            refresh_token=token_payload.get("refresh_token"),
            token_expiry=token_payload.get("token_expiry"),
            connection_status="connected",
            account_meta_json=_json_dumps({"label": config.label}),
        )
        db.add(account)
        db.flush()
    else:
        account.email = account_info.get("email")
        account.access_token = token_payload["access_token"]
        if token_payload.get("refresh_token"):
            account.refresh_token = token_payload["refresh_token"]
        account.token_expiry = token_payload.get("token_expiry")
        account.connection_status = "connected"
        account.updated_at = _utcnow()
    db.commit()
    db.refresh(account)
    return account


def _upsert_connected_calendars(db: Session, account: ConnectedAccount, calendars: list[dict[str, Any]]) -> None:
    existing = {
        calendar.provider_calendar_id: calendar
        for calendar in db.query(ConnectedCalendar).filter(ConnectedCalendar.account_id == account.id).all()
    }
    seen = set()
    for item in calendars:
        seen.add(item["provider_calendar_id"])
        record = existing.get(item["provider_calendar_id"])
        if record is None:
            db.add(
                ConnectedCalendar(
                    account_id=account.id,
                    provider_calendar_id=item["provider_calendar_id"],
                    calendar_name=item["calendar_name"],
                    color=item.get("color"),
                    selected_for_sync=False,
                    sync_state_json="{}",
                )
            )
        else:
            record.calendar_name = item["calendar_name"]
            record.color = item.get("color")
            record.updated_at = _utcnow()
    for provider_calendar_id, record in existing.items():
        if provider_calendar_id not in seen:
            record.updated_at = _utcnow()
    db.commit()


def _ensure_default_calendar_selection(db: Session, account: ConnectedAccount) -> bool:
    calendars = (
        db.query(ConnectedCalendar)
        .filter(ConnectedCalendar.account_id == account.id)
        .order_by(ConnectedCalendar.id.asc())
        .all()
    )
    if not calendars or any(calendar.selected_for_sync for calendar in calendars):
        return False

    candidates: list[ConnectedCalendar] = []
    normalized_email = (account.email or "").strip().lower()

    if account.provider == GOOGLE_PROVIDER:
        candidates = [
            calendar
            for calendar in calendars
            if (calendar.provider_calendar_id or "").strip().lower() == normalized_email
            or (calendar.calendar_name or "").strip().lower() == normalized_email
        ]
    else:
        candidates = [
            calendar
            for calendar in calendars
            if (calendar.calendar_name or "").strip().lower() == "calendar"
        ]

    if not candidates and len(calendars) == 1:
        candidates = [calendars[0]]

    if not candidates:
        return False

    for calendar in calendars:
        calendar.selected_for_sync = calendar in candidates
        calendar.updated_at = _utcnow()

    db.commit()
    return True


def _ensure_valid_access_token(config: ProviderConfig, account: ConnectedAccount, db: Session) -> str:
    if account.token_expiry and account.token_expiry > (_utcnow() + timedelta(seconds=60)):
        return account.access_token

    refreshed = _refresh_access_token(config, account)
    account.access_token = refreshed["access_token"]
    if refreshed.get("refresh_token"):
        account.refresh_token = refreshed["refresh_token"]
    account.token_expiry = refreshed.get("token_expiry")
    account.updated_at = _utcnow()
    db.commit()
    return account.access_token


def _sync_single_calendar(
    config: ProviderConfig,
    account: ConnectedAccount,
    calendar: ConnectedCalendar,
    access_token: str,
    db: Session,
) -> int:
    sync_state = _json_loads(calendar.sync_state_json, {})
    if config.provider == GOOGLE_PROVIDER:
        items, next_state = _google_sync_calendar(calendar.provider_calendar_id, access_token, sync_state)
    else:
        items, next_state = _microsoft_sync_calendar(calendar.provider_calendar_id, access_token, sync_state)

    synced_count = 0
    active_event_ids: set[str] = set()
    for item in items:
        synced_count += 1
        provider_event_id = item["provider_event_id"]
        active_event_ids.add(provider_event_id)
        event = (
            db.query(ExternalEvent)
            .filter(
                ExternalEvent.connected_calendar_id == calendar.id,
                ExternalEvent.provider_event_id == provider_event_id,
            )
            .first()
        )
        if event is None:
            event = ExternalEvent(
                account_id=account.id,
                connected_calendar_id=calendar.id,
                provider=config.provider,
                provider_event_id=provider_event_id,
                title=item["title"],
                description=item.get("description"),
                location=item.get("location"),
                start_at=item["start_at"],
                end_at=item["end_at"],
                timezone=item.get("timezone"),
                all_day=item.get("all_day", False),
                status=item.get("status", "confirmed"),
                cancelled=item.get("cancelled", False),
                etag=item.get("etag"),
                source_url=item.get("source_url"),
                calendar_label=calendar.calendar_name,
                event_meta_json=_json_dumps(item.get("meta")),
                last_synced_at=_utcnow(),
            )
            db.add(event)
        else:
            event.title = item["title"]
            event.description = item.get("description")
            event.location = item.get("location")
            event.start_at = item["start_at"]
            event.end_at = item["end_at"]
            event.timezone = item.get("timezone")
            event.all_day = item.get("all_day", False)
            event.status = item.get("status", "confirmed")
            event.cancelled = item.get("cancelled", False)
            event.etag = item.get("etag")
            event.source_url = item.get("source_url")
            event.calendar_label = calendar.calendar_name
            event.event_meta_json = _json_dumps(item.get("meta"))
            event.last_synced_at = _utcnow()
            event.updated_at = _utcnow()

    if config.provider == GOOGLE_PROVIDER and sync_state.get("sync_token"):
        existing_events = (
            db.query(ExternalEvent)
            .filter(ExternalEvent.connected_calendar_id == calendar.id)
            .all()
        )
        for event in existing_events:
            if event.provider_event_id in active_event_ids:
                continue
            # Google incremental sync returns deleted/cancelled items separately. Preserve items not mentioned.

    calendar.sync_state_json = _json_dumps(next_state)
    calendar.updated_at = _utcnow()
    db.commit()
    return synced_count


def _google_sync_calendar(calendar_id: str, access_token: str, sync_state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params: dict[str, Any] = {
        "singleEvents": "true",
        "showDeleted": "true",
        "maxResults": "2500",
    }
    sync_token = sync_state.get("sync_token")
    if sync_token:
        params["syncToken"] = sync_token
    else:
        params["timeMin"] = _sync_window_start().isoformat().replace("+00:00", "Z")
        params["timeMax"] = _sync_window_end().isoformat().replace("+00:00", "Z")

    items: list[dict[str, Any]] = []
    next_page_token = None
    next_sync_token = None
    while True:
        query = dict(params)
        if next_page_token:
            query["pageToken"] = next_page_token
        try:
            data = _authorized_json(
                "GET",
                f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(calendar_id, safe='')}/events",
                access_token=access_token,
                params=query,
            )
        except HTTPException as exc:
            if exc.status_code == 410:
                return _google_sync_calendar(calendar_id, access_token, {})
            raise
        next_page_token = data.get("nextPageToken")
        next_sync_token = data.get("nextSyncToken") or next_sync_token
        for item in data.get("items", []):
            parsed = _normalize_google_event(item)
            if parsed:
                items.append(parsed)
        if not next_page_token:
            break
    return items, {"sync_token": next_sync_token}


def _resolve_calendar_timezone(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _utc_naive_to_local(value: datetime, local_timezone: ZoneInfo) -> datetime:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.astimezone(local_timezone)


def _utc_naive_to_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _local_day_boundary_to_utc_naive(value: date, local_timezone: ZoneInfo) -> datetime:
    local_boundary = datetime.combine(value, time.min, tzinfo=local_timezone)
    return local_boundary.astimezone(timezone.utc).replace(tzinfo=None)


def _microsoft_sync_calendar(calendar_id: str, access_token: str, sync_state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    delta_link = sync_state.get("delta_link")
    if delta_link:
        url = delta_link
        params = None
    else:
        url = f"https://graph.microsoft.com/v1.0/me/calendars/{calendar_id}/calendarView/delta"
        params = {
            "startDateTime": _sync_window_start().isoformat().replace("+00:00", "Z"),
            "endDateTime": _sync_window_end().isoformat().replace("+00:00", "Z"),
            "$select": "id,subject,body,bodyPreview,location,start,end,isAllDay,webLink,lastModifiedDateTime,showAs,onlineMeetingUrl,organizer,attendees",
        }

    items: list[dict[str, Any]] = []
    next_link = url
    next_delta_link = None
    while next_link:
        data = _authorized_json(
            "GET",
            next_link,
            access_token=access_token,
            params=params,
        )
        params = None
        next_link = data.get("@odata.nextLink")
        next_delta_link = data.get("@odata.deltaLink") or next_delta_link
        for item in data.get("value", []):
            parsed = _normalize_microsoft_event(item)
            if parsed:
                items.append(parsed)
    return items, {"delta_link": next_delta_link}


def _normalize_google_event(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("id"):
        return None
    start_at, timezone_name, all_day = _parse_provider_datetime(item.get("start", {}))
    end_at, _, _ = _parse_provider_datetime(item.get("end", {}), all_day=all_day, is_end=True)
    if start_at is None or end_at is None:
        return None
    return {
        "provider_event_id": item["id"],
        "title": item.get("summary") or "Untitled event",
        "description": item.get("description"),
        "location": item.get("location"),
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone_name,
        "all_day": all_day,
        "status": item.get("status", "confirmed"),
        "cancelled": item.get("status") == "cancelled",
        "etag": item.get("etag"),
        "source_url": item.get("htmlLink"),
        "meta": {
            "provider": GOOGLE_PROVIDER,
            "organizer": ((item.get("organizer") or {}).get("email")) or ((item.get("creator") or {}).get("email")),
            "attendee_count": len(item.get("attendees") or []),
        },
    }


def _normalize_microsoft_event(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("id"):
        return None
    start_at, timezone_name, all_day = _parse_provider_datetime(item.get("start", {}), time_key="dateTime", timezone_key="timeZone")
    end_at, _, _ = _parse_provider_datetime(
        item.get("end", {}),
        time_key="dateTime",
        timezone_key="timeZone",
        all_day=item.get("isAllDay", False),
        is_end=True,
    )
    if start_at is None or end_at is None:
        return None
    show_as = item.get("showAs", "busy")
    status = "cancelled" if item.get("@removed") else show_as
    body = item.get("body") or {}
    description = _strip_html(body.get("content")) or item.get("bodyPreview")
    source_url = item.get("onlineMeetingUrl") or item.get("webLink")
    organizer = ((item.get("organizer") or {}).get("emailAddress") or {}).get("address")
    return {
        "provider_event_id": item["id"],
        "title": item.get("subject") or "Untitled event",
        "description": description,
        "location": (item.get("location") or {}).get("displayName"),
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone_name,
        "all_day": bool(item.get("isAllDay", False)),
        "status": status,
        "cancelled": bool(item.get("@removed")),
        "etag": item.get("@odata.etag"),
        "source_url": source_url,
        "meta": {
            "provider": MICROSOFT_PROVIDER,
            "organizer": organizer,
            "attendee_count": len(item.get("attendees") or []),
        },
    }


def _parse_provider_datetime(
    payload: dict[str, Any],
    time_key: str = "dateTime",
    timezone_key: str = "timeZone",
    all_day: bool | None = None,
    is_end: bool = False,
) -> tuple[datetime | None, str | None, bool]:
    if "date" in payload:
        all_day = True if all_day is None else all_day
        raw_date = payload.get("date")
        if not raw_date:
            return None, None, bool(all_day)
        parsed_date = date.fromisoformat(raw_date)
        if is_end:
            parsed_date = parsed_date - timedelta(days=1)
        dt = datetime.combine(parsed_date, time(0, 0))
        if is_end:
            dt = datetime.combine(parsed_date, time(23, 59))
        return dt, payload.get(timezone_key), True

    raw_value = payload.get(time_key)
    timezone_name = payload.get(timezone_key)
    if not raw_value:
        return None, timezone_name, bool(all_day)

    normalized = str(raw_value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed, timezone_name, bool(all_day)


def _sync_window_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=SYNC_WINDOW_DAYS_BACK)


def _sync_window_end() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=SYNC_WINDOW_DAYS_FORWARD)


def _authorized_json(
    method: str,
    url: str,
    *,
    access_token: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {"Authorization": f"Bearer {access_token}"}
    if headers:
        request_headers.update(headers)
    return _request_json(method, url, headers=request_headers, params=params, data=data)


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.request(method, url, headers=headers, params=params, data=data, timeout=15)
    if response.status_code >= 400:
        detail = _safe_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)
    if not response.text:
        return {}
    return response.json()


def _safe_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text or "Provider request failed"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return error.get("message") or error.get("code") or "Provider request failed"
        if isinstance(error, str):
            return error
        return payload.get("error_description") or payload.get("message") or "Provider request failed"
    return "Provider request failed"


def _strip_html(value: str | None) -> str | None:
    if not value:
        return value
    text = unescape(value.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n"))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
