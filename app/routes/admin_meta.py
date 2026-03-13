from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_current_admin
from app.models import Cyclist, Event, RaceSetting
from app.schemas import RaceSettingsUpdate

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/dashboard")
def admin_dashboard(
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    total_events = db.query(Event).filter(Event.deleted_at.is_(None)).count()
    needs_review_count = db.query(Event).filter(
        Event.deleted_at.is_(None), Event.status == "needs_review"
    ).count()
    total_cyclists = db.query(Cyclist).count()
    today_start = datetime.combine(date.today(), datetime.min.time())
    events_today = db.query(Event).filter(
        Event.deleted_at.is_(None), Event.created_at >= today_start
    ).count()
    return {
        "total_events": total_events,
        "needs_review_count": needs_review_count,
        "total_cyclists": total_cyclists,
        "events_today": events_today,
    }


@router.get("/categories")
def admin_list_categories(
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    results = (
        db.query(
            Cyclist.categoria,
            func.count(Cyclist.id).label("cyclist_count"),
        )
        .group_by(Cyclist.categoria)
        .order_by(Cyclist.categoria.asc())
        .all()
    )
    return [
        {"categoria": r.categoria, "cyclist_count": r.cyclist_count}
        for r in results
    ]


@router.get("/settings")
def admin_get_settings(admin_user: str = Depends(get_current_admin)):
    return {
        "finish_checkpoint_id": settings.finish_checkpoint_id,
        "needs_review_min_conf": settings.needs_review_min_conf,
        "device_api_key_hint": settings.device_api_key[:4] + "****"
        if len(settings.device_api_key) > 4
        else "****",
    }


@router.get("/race-settings")
def admin_get_race_settings(
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    row = db.query(RaceSetting).filter(RaceSetting.key == "race_start_time").first()
    return {"race_start_time": row.value if row else None}


@router.put("/race-settings")
def admin_update_race_settings(
    body: RaceSettingsUpdate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    row = db.query(RaceSetting).filter(RaceSetting.key == "race_start_time").first()
    if row:
        row.value = body.race_start_time
        row.updated_at = datetime.now()
    else:
        row = RaceSetting(key="race_start_time", value=body.race_start_time)
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"race_start_time": row.value}
