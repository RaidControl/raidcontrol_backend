from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Checkpoint, Cyclist, Event
from app.schemas import EventResponse, PublicEvent
from app.storage import get_image_url
from app.utils import compute_elapsed_seconds
from app.ws_manager import ws_manager


# -----------------------
# Business logic
# -----------------------

def checkpoint_to_response(cp: Checkpoint) -> dict:
    return {
        "id": cp.id,
        "checkpoint_id": cp.checkpoint_id,
        "name": cp.name,
        "ordering": cp.ordering,
        "distances": json.loads(cp.distances) if cp.distances else {},
        "is_meta": cp.is_meta,
    }


def determine_event_status(bib: int | None, conf: float | None, db: Session | None = None) -> tuple[str, str | None]:
    """Returns (status, note)."""
    if bib is None:
        return "needs_review", None
    if conf is not None and conf < settings.needs_review_min_conf:
        return "needs_review", None
    if db is not None:
        cyclist_exists = db.query(Cyclist.id).filter(Cyclist.numero == bib).first() is not None
        if not cyclist_exists:
            return "needs_review", "bib not found in cyclists registry"
    return "ok", None


def find_duplicate_event(
    db: Session, bib: int | None, checkpoint_id: str, exclude_id: int | None = None
) -> Event | None:
    """Return an existing valid event for the same (bib, checkpoint), or None."""
    if bib is None:
        return None
    q = db.query(Event).filter(
        Event.deleted_at.is_(None),
        Event.checkpoint_id == checkpoint_id,
        Event.status != "rejected",
        (Event.bib_number_real == bib)
        | (Event.bib_number_real.is_(None) & (Event.bib_number_pred == bib)),
    )
    if exclude_id is not None:
        q = q.filter(Event.id != exclude_id)
    return q.first()


def auto_set_llego(db: Session, bib: int | None, checkpoint_id: str, event_ts: datetime | None = None) -> bool:
    """If checkpoint is meta and cyclist exists, set status='llego' and hora_llegada. Returns True if changed."""
    if bib is None:
        return False
    cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == checkpoint_id).first()
    if not cp or not cp.is_meta:
        return False
    cyclist = db.query(Cyclist).filter(Cyclist.numero == bib).first()
    if cyclist and cyclist.status != "abandono":
        cyclist.status = "llego"
        cyclist.hora_llegada = event_ts or datetime.now(timezone.utc)
        db.add(cyclist)
        return True
    return False


# -----------------------
# Response formatters
# -----------------------

def event_to_response(ev: Event) -> EventResponse:
    return EventResponse(
        id=ev.id,
        ts=ev.ts,
        device_id=ev.device_id,
        checkpoint_id=ev.checkpoint_id,
        bib_number_pred=ev.bib_number_pred,
        bib_number_real=ev.bib_number_real,
        bib_number_effective=ev.bib_number_effective,
        conf=ev.conf,
        plate_color=ev.plate_color,
        status=ev.status,
        note=ev.note,
        image_url=get_image_url(ev.image_path, ev.id),
        created_at=ev.created_at,
    )


def event_to_public(ev: Event, cyclist: Cyclist | None) -> PublicEvent:
    cyclist_name = None
    cyclist_category = None
    cyclist_distance_label = None
    elapsed = None
    if cyclist:
        cyclist_name = f"{cyclist.nombre} {cyclist.apellido}".strip()
        cyclist_category = cyclist.categoria
        cyclist_distance_label = cyclist.circuito
        elapsed = compute_elapsed_seconds(ev.ts, cyclist.hora_salida)

    return PublicEvent(
        id=ev.id,
        ts=ev.ts,
        checkpoint_id=ev.checkpoint_id,
        device_id=ev.device_id,
        bib_number=ev.bib_number_effective,
        confidence=ev.conf,
        cyclist_name=cyclist_name,
        cyclist_category=cyclist_category,
        cyclist_distance_label=cyclist_distance_label,
        elapsed_seconds=elapsed,
        status=ev.status,
        image_url=get_image_url(ev.image_path, ev.id),
    )


# -----------------------
# WebSocket helpers
# -----------------------

def public_room_for_checkpoint(checkpoint_id: str | None) -> str:
    if checkpoint_id:
        return f"public:{checkpoint_id}"
    return "public:all"


async def broadcast_public_event(db: Session, ev: Event, msg_type: str):
    """Solo transmitir eventos públicos: status='ok' con ciclista identificado."""
    if ev.status != "ok" or ev.bib_number_effective is None:
        return

    cyclist = db.query(Cyclist).filter(Cyclist.numero == ev.bib_number_effective).first()

    msg = {"type": msg_type, "data": event_to_public(ev, cyclist).model_dump()}
    await ws_manager.broadcast(public_room_for_checkpoint(ev.checkpoint_id), msg)
    await ws_manager.broadcast(public_room_for_checkpoint(None), msg)


async def broadcast_stats(db: Session):
    """Compute overall stats (no filters) and broadcast."""
    cyclists = db.query(Cyclist).all()
    total = len(cyclists)
    en_carrera = sum(1 for c in cyclists if c.status == "en_carrera")
    llego = sum(1 for c in cyclists if c.status == "llego")
    abandono = sum(1 for c in cyclists if c.status == "abandono")

    def pct(x: int) -> float:
        return (float(x) / float(total) * 100.0) if total > 0 else 0.0

    payload = {
        "type": "stats_updated",
        "data": {
            "total": total,
            "en_carrera": en_carrera,
            "llego": llego,
            "abandono": abandono,
            "pct_en_carrera": pct(en_carrera),
            "pct_llego": pct(llego),
            "pct_abandono": pct(abandono),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    await ws_manager.broadcast(public_room_for_checkpoint(None), payload)
