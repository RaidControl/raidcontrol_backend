from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth import require_device_key
from app.db import get_db
from app.helpers import (
    auto_set_llego,
    broadcast_public_event,
    broadcast_stats,
    determine_event_status,
    event_to_response,
    find_duplicate_event,
)
from app.models import Device, Event
from app.schemas import EventResponse, IngestCreateEventRequest, IngestCreateEventResponse
from app.storage import upload_image
from app.utils import parse_iso_dt

router = APIRouter(prefix="/api/v1", tags=["ingest"])


@router.post("/ingest/events/upload", response_model=EventResponse)
async def ingest_event_upload(
    ts: str = Form(...),
    checkpoint_id: str = Form(...),
    device_id: str = Form(...),
    bib_number_pred: str | None = Form(default=None),
    conf: str | None = Form(default=None),
    plate_color: str | None = Form(default=None),
    bbox_json: str | None = Form(default=None),
    meta_json: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    x_device_key: str | None = Header(default=None),
):
    require_device_key(x_device_key)

    ts_dt = parse_iso_dt(ts)
    bib_pred_int = int(bib_number_pred) if bib_number_pred not in (None, "", "null") else None
    conf_f = float(conf) if conf not in (None, "", "null") else None

    img_path = await upload_image(file)

    # Device mapping: resolve checkpoint from device record
    device_record = db.query(Device).filter(Device.device_id == device_id, Device.is_active == True).first()
    if device_record and device_record.checkpoint_id:
        resolved_checkpoint_id = device_record.checkpoint_id
    elif checkpoint_id:
        resolved_checkpoint_id = checkpoint_id
    else:
        resolved_checkpoint_id = "unknown"

    status, note = determine_event_status(bib_pred_int, conf_f, db)

    if resolved_checkpoint_id == "unknown":
        status = "needs_review"

    if status == "ok" and find_duplicate_event(db, bib_pred_int, resolved_checkpoint_id):
        status = "needs_review"
        note = "possible duplicate"

    ev = Event(
        ts=ts_dt,
        checkpoint_id=resolved_checkpoint_id,
        device_id=device_id,
        bib_number_pred=bib_pred_int,
        conf=conf_f,
        plate_color=plate_color,
        bbox_json=bbox_json,
        meta_json=meta_json,
        status=status,
        note=note,
        image_path=img_path,
    )
    db.add(ev)

    llego_changed = False
    if status == "ok":
        llego_changed = auto_set_llego(db, bib_pred_int, resolved_checkpoint_id, event_ts=ev.ts)

    db.commit()
    db.refresh(ev)

    await broadcast_public_event(db, ev, msg_type="event_created")
    if llego_changed:
        await broadcast_stats(db)

    return event_to_response(ev)


@router.post("/stations/{station_id}/events", response_model=IngestCreateEventResponse)
async def ingest_create_event(
    station_id: str,
    body: IngestCreateEventRequest,
    db: Session = Depends(get_db),
    x_device_key: str | None = Header(default=None),
):
    require_device_key(x_device_key)

    ts_dt = parse_iso_dt(body.detected_at)

    bib_pred: int | None = None
    try:
        bib_pred = int(body.number_str)
    except (ValueError, TypeError):
        pass

    meta = body.meta or {}
    conf_raw = meta.get("confidence") or meta.get("conf")
    conf_f = float(conf_raw) if conf_raw is not None else None
    plate_color = meta.get("plate_color")
    bbox = meta.get("bbox")

    effective_device_id = body.device_id or ""
    device_record = db.query(Device).filter(Device.device_id == effective_device_id, Device.is_active == True).first()
    if device_record and device_record.checkpoint_id:
        resolved_checkpoint_id = device_record.checkpoint_id
    elif station_id:
        resolved_checkpoint_id = station_id
    else:
        resolved_checkpoint_id = "unknown"

    status, note = determine_event_status(bib_pred, conf_f, db)

    if resolved_checkpoint_id == "unknown":
        status = "needs_review"

    if status == "ok" and find_duplicate_event(db, bib_pred, resolved_checkpoint_id):
        status = "needs_review"
        note = "possible duplicate"

    ev = Event(
        ts=ts_dt,
        checkpoint_id=resolved_checkpoint_id,
        device_id=effective_device_id,
        bib_number_pred=bib_pred,
        conf=conf_f,
        plate_color=plate_color,
        bbox_json=json.dumps(bbox) if bbox else None,
        meta_json=json.dumps(meta) if meta else None,
        status=status,
        note=note,
    )
    db.add(ev)

    llego_changed = False
    if status == "ok":
        llego_changed = auto_set_llego(db, bib_pred, resolved_checkpoint_id, event_ts=ev.ts)

    db.commit()
    db.refresh(ev)

    await broadcast_public_event(db, ev, msg_type="event_created")
    if llego_changed:
        await broadcast_stats(db)

    return IngestCreateEventResponse(event_id=ev.id, id=ev.id, status=ev.status)


@router.post("/events/{event_id}/image")
async def ingest_upload_image(
    event_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    x_device_key: str | None = Header(default=None),
):
    require_device_key(x_device_key)

    ev = db.query(Event).filter(Event.id == event_id, Event.deleted_at.is_(None)).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    img_path = await upload_image(file)
    ev.image_path = img_path
    db.add(ev)
    db.commit()
    db.refresh(ev)

    await broadcast_public_event(db, ev, msg_type="event_updated")

    return {"ok": True, "event_id": ev.id}
