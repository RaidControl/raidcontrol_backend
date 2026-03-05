from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_admin
from app.models import Checkpoint, Device, Event
from app.schemas import DeviceCreate, DeviceUpdate

router = APIRouter(prefix="/api/v1/admin/devices", tags=["admin"])


@router.get("")
def admin_list_devices(
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    devices = db.query(Device).order_by(Device.created_at.desc()).all()
    event_stats = (
        db.query(
            Event.device_id,
            func.count(Event.id).label("event_count"),
            func.max(Event.ts).label("last_seen"),
        )
        .filter(Event.deleted_at.is_(None))
        .group_by(Event.device_id)
        .all()
    )
    stats_map = {r.device_id: {"event_count": r.event_count, "last_seen": r.last_seen} for r in event_stats}

    result = []
    for d in devices:
        s = stats_map.get(d.device_id, {"event_count": 0, "last_seen": None})
        result.append({
            "id": d.id,
            "device_id": d.device_id,
            "name": d.name,
            "checkpoint_id": d.checkpoint_id,
            "is_active": d.is_active,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "event_count": s["event_count"],
            "last_seen": s["last_seen"].isoformat() if s["last_seen"] else None,
        })
    return result


@router.post("", status_code=201)
def admin_create_device(
    body: DeviceCreate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Device).filter(Device.device_id == body.device_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="device_id already exists")
    if body.checkpoint_id:
        cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == body.checkpoint_id).first()
        if not cp:
            raise HTTPException(status_code=400, detail=f"Checkpoint '{body.checkpoint_id}' not found")
    device = Device(
        device_id=body.device_id,
        name=body.name,
        checkpoint_id=body.checkpoint_id,
        is_active=body.is_active,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return {
        "id": device.id,
        "device_id": device.device_id,
        "name": device.name,
        "checkpoint_id": device.checkpoint_id,
        "is_active": device.is_active,
        "created_at": device.created_at.isoformat() if device.created_at else None,
        "event_count": 0,
        "last_seen": None,
    }


@router.patch("/{device_id}")
def admin_update_device(
    device_id: str,
    body: DeviceUpdate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    updates = body.model_dump(exclude_unset=True)
    if "checkpoint_id" in updates and updates["checkpoint_id"] is not None:
        cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == updates["checkpoint_id"]).first()
        if not cp:
            raise HTTPException(status_code=400, detail=f"Checkpoint '{updates['checkpoint_id']}' not found")
    for field, val in updates.items():
        setattr(device, field, val)
    db.add(device)
    db.commit()
    db.refresh(device)
    stats = (
        db.query(
            func.count(Event.id).label("event_count"),
            func.max(Event.ts).label("last_seen"),
        )
        .filter(Event.deleted_at.is_(None), Event.device_id == device.device_id)
        .first()
    )
    return {
        "id": device.id,
        "device_id": device.device_id,
        "name": device.name,
        "checkpoint_id": device.checkpoint_id,
        "is_active": device.is_active,
        "created_at": device.created_at.isoformat() if device.created_at else None,
        "event_count": stats.event_count if stats else 0,
        "last_seen": stats.last_seen.isoformat() if stats and stats.last_seen else None,
    }


@router.delete("/{device_id}")
def admin_delete_device(
    device_id: str,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    db.delete(device)
    db.commit()
    return {"ok": True}
