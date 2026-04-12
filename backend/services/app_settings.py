from datetime import datetime

from sqlalchemy.orm import Session

from app.models import AppSetting


INTEGRATION_SETTING_KEYS = {
    "app_base_url": "APP_BASE_URL",
    "google_client_id": "GOOGLE_CLIENT_ID",
    "google_client_secret": "GOOGLE_CLIENT_SECRET",
    "microsoft_client_id": "MICROSOFT_CLIENT_ID",
    "microsoft_client_secret": "MICROSOFT_CLIENT_SECRET",
    "microsoft_tenant_id": "MICROSOFT_TENANT_ID",
}


def get_integration_settings(db: Session) -> dict[str, str]:
    rows = (
        db.query(AppSetting)
        .filter(AppSetting.setting_key.in_(INTEGRATION_SETTING_KEYS.values()))
        .all()
    )
    by_key = {row.setting_key: row.setting_value or "" for row in rows}
    return {
        public_key: by_key.get(storage_key, "")
        for public_key, storage_key in INTEGRATION_SETTING_KEYS.items()
    }


def update_integration_settings(db: Session, values: dict[str, str]) -> dict[str, str]:
    existing_rows = {
        row.setting_key: row
        for row in db.query(AppSetting).filter(AppSetting.setting_key.in_(INTEGRATION_SETTING_KEYS.values())).all()
    }
    now = datetime.utcnow()

    for public_key, storage_key in INTEGRATION_SETTING_KEYS.items():
        next_value = (values.get(public_key) or "").strip()
        row = existing_rows.get(storage_key)
        if row is None:
            db.add(
                AppSetting(
                    setting_key=storage_key,
                    setting_value=next_value,
                    updated_at=now,
                    created_at=now,
                )
            )
        else:
            row.setting_value = next_value
            row.updated_at = now

    db.commit()
    return get_integration_settings(db)
