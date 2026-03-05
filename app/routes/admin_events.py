from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_current_admin
from app.helpers import (
    auto_set_llego,
    broadcast_public_event,
    broadcast_stats,
    determine_event_status,
    event_to_response,
    find_duplicate_event,
    public_room_for_checkpoint,
)
from app.models import Checkpoint, Cyclist, Event
from app.schemas import AdminEventCreate, EventResponse, EventUpdateRequest
from app.ws_manager import ws_manager

router = APIRouter(prefix="/api/v1/admin/events", tags=["admin"])


@router.post("", response_model=EventResponse, status_code=201)
async def admin_create_event(
    body: AdminEventCreate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == body.checkpoint_id).first()
    if not cp:
        raise HTTPException(status_code=400, detail=f"Checkpoint '{body.checkpoint_id}' not found")

    cyclist = db.query(Cyclist).filter(Cyclist.numero == body.bib_number).first()
    if not cyclist:
        raise HTTPException(status_code=400, detail=f"Cyclist with numero {body.bib_number} not found")

    valid_statuses = ("ok", "needs_review")
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    ts = body.ts or datetime.now(timezone.utc)
    status = body.status
    note = body.note

    if status == "ok" and find_duplicate_event(db, body.bib_number, body.checkpoint_id):
        status = "needs_review"
        note = note or "possible duplicate (manual)"

    ev = Event(
        ts=ts,
        checkpoint_id=body.checkpoint_id,
        device_id="admin",
        bib_number_pred=body.bib_number,
        bib_number_real=body.bib_number,
        conf=1.0,
        status=status,
        note=note,
    )
    db.add(ev)

    llego_changed = False
    if status == "ok":
        llego_changed = auto_set_llego(db, body.bib_number, body.checkpoint_id, event_ts=ts)

    db.commit()
    db.refresh(ev)

    await broadcast_public_event(db, ev, msg_type="event_created")
    if llego_changed:
        await broadcast_stats(db)

    return event_to_response(ev)


@router.get("", response_model=list[EventResponse])
def admin_list_events(
    limit: int = 100,
    skip: int = 0,
    status: str | None = None,
    needs_review: bool = False,
    checkpoint_id: str | None = None,
    bib_number: int | None = None,
    has_image: bool | None = None,
    min_conf: float | None = None,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    q_db = db.query(Event).filter(Event.deleted_at.is_(None)).order_by(Event.ts.desc())

    if status:
        q_db = q_db.filter(Event.status == status)
    elif needs_review:
        q_db = q_db.filter(Event.status == "needs_review")
    if checkpoint_id:
        q_db = q_db.filter(Event.checkpoint_id == checkpoint_id)
    if bib_number is not None:
        q_db = q_db.filter((Event.bib_number_real == bib_number) | (Event.bib_number_pred == bib_number))
    if has_image is not None:
        if has_image:
            q_db = q_db.filter(Event.image_path.is_not(None))
        else:
            q_db = q_db.filter(Event.image_path.is_(None))
    if min_conf is not None:
        q_db = q_db.filter(Event.conf.is_not(None)).filter(Event.conf >= float(min_conf))

    events = q_db.offset(max(skip, 0)).limit(min(limit, 500)).all()
    return [event_to_response(e) for e in events]


@router.patch("/{event_id}", response_model=EventResponse)
async def admin_update_event(
    event_id: int,
    body: EventUpdateRequest,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    ev = db.query(Event).filter(Event.id == event_id, Event.deleted_at.is_(None)).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    was_public = ev.status == "ok" and ev.bib_number_effective is not None

    bib_changed = False
    ts_changed = False

    if body.ts is not None:
        ev.ts = body.ts
        ts_changed = True

    if body.bib_number_real is not None:
        ev.bib_number_real = body.bib_number_real
        bib_changed = True

    if body.status is not None:
        ev.status = body.status

    if body.note is not None:
        ev.note = body.note

    if ev.status == "needs_review":
        if ev.bib_number_effective is not None and (ev.conf is None or ev.conf >= settings.needs_review_min_conf):
            ev.status = "ok"

    llego_changed = False
    if bib_changed and ev.bib_number_effective is not None and ev.status != "rejected":
        dup = find_duplicate_event(db, ev.bib_number_effective, ev.checkpoint_id, exclude_id=ev.id)
        if dup:
            if dup.ts <= ev.ts:
                ev.status = "rejected"
                ev.note = f"duplicate (kept event #{dup.id})"
            else:
                dup.status = "rejected"
                dup.note = f"duplicate (kept event #{ev.id})"
                db.add(dup)

        if ev.status != "rejected":
            llego_changed = auto_set_llego(db, ev.bib_number_effective, ev.checkpoint_id, event_ts=ev.ts)

    if ts_changed and not llego_changed and ev.status == "ok" and ev.bib_number_effective is not None:
        cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == ev.checkpoint_id).first()
        if cp and cp.is_meta:
            cyclist = db.query(Cyclist).filter(Cyclist.numero == ev.bib_number_effective).first()
            if cyclist and cyclist.status == "llego":
                cyclist.hora_llegada = ev.ts
                db.add(cyclist)

    db.add(ev)
    db.commit()
    db.refresh(ev)

    is_public = ev.status == "ok" and ev.bib_number_effective is not None

    if is_public:
        await broadcast_public_event(db, ev, msg_type="event_updated")
    elif was_public and not is_public:
        msg = {"type": "event_deleted", "id": ev.id}
        await ws_manager.broadcast(public_room_for_checkpoint(ev.checkpoint_id), msg)
        await ws_manager.broadcast(public_room_for_checkpoint(None), msg)

    if llego_changed:
        await broadcast_stats(db)

    return event_to_response(ev)


@router.delete("/{event_id}")
async def admin_delete_event(
    event_id: int,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    ev = db.query(Event).filter(Event.id == event_id, Event.deleted_at.is_(None)).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")
    ev.deleted_at = datetime.now(timezone.utc)
    db.add(ev)
    db.commit()

    await ws_manager.broadcast(public_room_for_checkpoint(ev.checkpoint_id), {"type": "event_deleted", "id": ev.id})
    await broadcast_stats(db)
    return {"ok": True}
