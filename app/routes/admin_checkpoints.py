from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_admin
from app.helpers import checkpoint_to_response
from app.models import Checkpoint, Event
from app.schemas import CheckpointCreate, CheckpointUpdate

router = APIRouter(prefix="/api/v1/admin/checkpoints", tags=["admin"])


@router.get("")
def admin_list_checkpoints(
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    cps = db.query(Checkpoint).order_by(Checkpoint.ordering.asc()).all()
    return [checkpoint_to_response(cp) for cp in cps]


@router.post("", status_code=201)
def admin_create_checkpoint(
    body: CheckpointCreate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == body.checkpoint_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="checkpoint_id already exists")
    if body.is_meta:
        db.query(Checkpoint).filter(Checkpoint.is_meta == True).update({"is_meta": False})
    cp = Checkpoint(
        checkpoint_id=body.checkpoint_id,
        name=body.name,
        ordering=body.ordering,
        distances=json.dumps(body.distances),
        is_meta=body.is_meta,
    )
    db.add(cp)
    db.commit()
    db.refresh(cp)
    return checkpoint_to_response(cp)


@router.patch("/{checkpoint_id}")
def admin_update_checkpoint(
    checkpoint_id: str,
    body: CheckpointUpdate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == checkpoint_id).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    updates = body.model_dump(exclude_unset=True)
    if "distances" in updates:
        cp.distances = json.dumps(updates.pop("distances"))
    if updates.get("is_meta") is True:
        db.query(Checkpoint).filter(Checkpoint.is_meta == True, Checkpoint.id != cp.id).update({"is_meta": False})
    for field, val in updates.items():
        setattr(cp, field, val)
    db.add(cp)
    db.commit()
    db.refresh(cp)
    return checkpoint_to_response(cp)


@router.delete("/{checkpoint_id}")
def admin_delete_checkpoint(
    checkpoint_id: str,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == checkpoint_id).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    event_count = db.query(Event).filter(Event.checkpoint_id == checkpoint_id).count()
    if event_count > 0:
        raise HTTPException(status_code=409, detail=f"Cannot delete: {event_count} events reference this checkpoint")
    db.delete(cp)
    db.commit()
    return {"ok": True}
