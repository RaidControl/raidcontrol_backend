from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from datetime import date, datetime, timezone
import time

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from sqlalchemy import func, text

from app.routes.token import router as token_router
from app.deps import get_current_admin
from app.config import settings
from app.db import engine, get_db
from app.models import Base, Cyclist, Checkpoint, Device, Event
from app.schemas import (
    AdminEventCreate,
    CheckpointCreate,
    CheckpointResponse,
    CheckpointUpdate,
    CyclistCreate,
    CyclistResponse,
    CyclistUpdate,
    DeviceCreate,
    DeviceResponse,
    DeviceUpdate,
    EventResponse,
    EventUpdateRequest,
    FiltersResponse,
    ImportResponse,
    IngestCreateEventRequest,
    IngestCreateEventResponse,
    LeaderboardEntry,
    LeaderboardResponse,
    LoginRequest,
    LoginResponse,
    PublicEvent,
    PublicFeedResponse,
    PublicStats,
)
from app.auth import create_access_token, require_device_key
from app.utils import compute_elapsed_seconds, parse_iso_dt, save_upload
from app.ws_manager import ws_manager

def _run_migrations():
    """Run lightweight schema migrations for changes not handled by create_all."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)

    # Migration: UTF-8
    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER DATABASE `{settings.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
        for tbl in ["cyclists", "checkpoints", "devices", "events"]:
            if tbl in insp.get_table_names():
                conn.execute(text(
                    f"ALTER TABLE `{tbl}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                ))

    # Migration 1: checkpoints — rename distance_km → distances (Text/JSON)
    if "checkpoints" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("checkpoints")}
        if "distance_km" in cols and "distances" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE checkpoints ADD COLUMN distances TEXT"))
                conn.execute(text("UPDATE checkpoints SET distances = CONCAT('{\"default\": ', COALESCE(distance_km, 0), '}')"))
                conn.execute(text("ALTER TABLE checkpoints DROP COLUMN distance_km"))
        elif "distance_km" in cols and "distances" in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE checkpoints DROP COLUMN distance_km"))
        elif "distances" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE checkpoints ADD COLUMN distances TEXT"))
                conn.execute(text("UPDATE checkpoints SET distances = '{}' WHERE distances IS NULL"))

    # Migration: is_meta
    if "checkpoints" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("checkpoints")}
        if "is_meta" not in cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE checkpoints ADD COLUMN is_meta BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                conn.execute(text(
                    "UPDATE checkpoints SET is_meta = TRUE WHERE checkpoint_id = :cp_id"
                ), {"cp_id": settings.finish_checkpoint_id})

    # Migration: hora_llegada
    if "cyclists" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("cyclists")}
        if "hora_llegada" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE cyclists ADD COLUMN hora_llegada DATETIME NULL"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    os.makedirs(settings.upload_dir, exist_ok=True)
    _wait_for_db()
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    yield
    # Shutdown (noop por ahora)

app = FastAPI(title="Raid Control API", version="0.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(token_router)

def _wait_for_db():
    max_tries = 60
    delay_sec = 1.0
    last_err = None

    for i in range(max_tries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print(f"[DB] Connected OK after {i+1} tries")
            return
        except OperationalError as e:
            last_err = e
            if (i + 1) % 5 == 0:
                print(f"[DB] waiting... try {i+1}/{max_tries}: {e}")
            time.sleep(delay_sec)

    raise RuntimeError(f"Database not ready after waiting. Last error: {last_err}")

# -----------------------
# Business logic helpers
# -----------------------
def _checkpoint_to_response(cp: Checkpoint) -> dict:
    return {
        "id": cp.id,
        "checkpoint_id": cp.checkpoint_id,
        "name": cp.name,
        "ordering": cp.ordering,
        "distances": json.loads(cp.distances) if cp.distances else {},
        "is_meta": cp.is_meta,
    }

def _determine_event_status(bib: int | None, conf: float | None) -> str:
    if bib is None:
        return "needs_review"
    if conf is not None and conf < settings.needs_review_min_conf:
        return "needs_review"
    return "ok"


def _find_duplicate_event(
    db: Session, bib: int | None, checkpoint_id: str, exclude_id: int | None = None
) -> Event | None:
    """Return an existing valid event for the same (bib, checkpoint), or None."""
    if bib is None:
        return None
    q = db.query(Event).filter(
        Event.deleted_at.is_(None),
        Event.checkpoint_id == checkpoint_id,
        Event.status != "rejected",
        # bib_number_effective as SQL filter:
        (Event.bib_number_real == bib)
        | (Event.bib_number_real.is_(None) & (Event.bib_number_pred == bib)),
    )
    if exclude_id is not None:
        q = q.filter(Event.id != exclude_id)
    return q.first()


def _auto_set_llego(db: Session, bib: int | None, checkpoint_id: str, event_ts: datetime | None = None) -> bool:
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
# Health
# -----------------------
@app.get("/health", tags=["health"])
def health(db: Session = Depends(get_db)):
    # Basic DB check
    db.execute(text("SELECT 1"))
    return {"ok": True}

# -----------------------
# Admin auth
# -----------------------
@app.post("/api/v1/admin/auth/login", tags=["auth"], response_model=LoginResponse)
def admin_login(req: LoginRequest):
    if req.username != settings.admin_username or req.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(subject=req.username)
    return LoginResponse(access_token=token, expires_in=settings.jwt_expires_min * 60)

# -----------------------
# Ingest (device): upload event + image
# -----------------------
@app.post("/api/v1/ingest/events/upload", tags=["ingest"], response_model=EventResponse)
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

    img_path = await save_upload(settings.upload_dir, file)

    # Device mapping: resolve checkpoint from device record
    device_record = db.query(Device).filter(Device.device_id == device_id, Device.is_active == True).first()
    if device_record and device_record.checkpoint_id:
        resolved_checkpoint_id = device_record.checkpoint_id
    elif checkpoint_id:
        resolved_checkpoint_id = checkpoint_id
    else:
        resolved_checkpoint_id = "unknown"

    status = _determine_event_status(bib_pred_int, conf_f)
    note = None

    # If checkpoint is unknown, force needs_review
    if resolved_checkpoint_id == "unknown":
        status = "needs_review"

    # Duplicate check — possible false OCR, send to admin review
    if status == "ok" and _find_duplicate_event(db, bib_pred_int, resolved_checkpoint_id):
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

    # Auto llego if finish checkpoint (only for confirmed events)
    llego_changed = False
    if status == "ok":
        llego_changed = _auto_set_llego(db, bib_pred_int, resolved_checkpoint_id, event_ts=ev.ts)

    db.commit()
    db.refresh(ev)

    await _broadcast_public_event(db, ev, msg_type="event_created")
    if llego_changed:
        await _broadcast_stats(db)

    return _event_to_response(ev)

# -----------------------
# Ingest (device): two-step (JSON event + image separately)
# Used by the Raspberry Pi uploader script
# -----------------------
@app.post("/api/v1/stations/{station_id}/events", tags=["ingest"], response_model=IngestCreateEventResponse)
async def ingest_create_event(
    station_id: str,
    body: IngestCreateEventRequest,
    db: Session = Depends(get_db),
    x_device_key: str | None = Header(default=None),
):
    require_device_key(x_device_key)

    ts_dt = parse_iso_dt(body.detected_at)

    # Parse number_str → bib_number_pred
    bib_pred: int | None = None
    try:
        bib_pred = int(body.number_str)
    except (ValueError, TypeError):
        pass

    # Extract known fields from meta dict
    meta = body.meta or {}
    conf_raw = meta.get("confidence") or meta.get("conf")
    conf_f = float(conf_raw) if conf_raw is not None else None
    plate_color = meta.get("plate_color")
    bbox = meta.get("bbox")

    # Device mapping: resolve checkpoint from device record
    effective_device_id = body.device_id or ""
    device_record = db.query(Device).filter(Device.device_id == effective_device_id, Device.is_active == True).first()
    if device_record and device_record.checkpoint_id:
        resolved_checkpoint_id = device_record.checkpoint_id
    elif station_id:
        resolved_checkpoint_id = station_id
    else:
        resolved_checkpoint_id = "unknown"

    status = _determine_event_status(bib_pred, conf_f)
    note = None

    # If checkpoint is unknown, force needs_review
    if resolved_checkpoint_id == "unknown":
        status = "needs_review"

    # Duplicate check — possible false OCR, send to admin review
    if status == "ok" and _find_duplicate_event(db, bib_pred, resolved_checkpoint_id):
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

    # Auto llego if finish checkpoint (only for confirmed events)
    llego_changed = False
    if status == "ok":
        llego_changed = _auto_set_llego(db, bib_pred, resolved_checkpoint_id, event_ts=ev.ts)

    db.commit()
    db.refresh(ev)

    await _broadcast_public_event(db, ev, msg_type="event_created")
    if llego_changed:
        await _broadcast_stats(db)

    return IngestCreateEventResponse(event_id=ev.id, id=ev.id, status=ev.status)


@app.post("/api/v1/events/{event_id}/image", tags=["ingest"])
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

    img_path = await save_upload(settings.upload_dir, file)
    ev.image_path = img_path
    db.add(ev)
    db.commit()
    db.refresh(ev)

    await _broadcast_public_event(db, ev, msg_type="event_updated")

    return {"ok": True, "event_id": ev.id}

# -----------------------
# Public: filters
# -----------------------
@app.get("/api/v1/public/filters", tags=["public"], response_model=FiltersResponse)
def public_filters(db: Session = Depends(get_db)):
    cps = db.query(Checkpoint).order_by(Checkpoint.ordering.asc()).all()
    categories = [r[0] for r in db.query(Cyclist.categoria).distinct().all() if r[0]]
    distances = [r[0] for r in db.query(Cyclist.circuito).distinct().all() if r[0]]
    genders = [r[0] for r in db.query(Cyclist.genero).distinct().all() if r[0]]

    return FiltersResponse(
        checkpoints=[
            {"checkpoint_id": c.checkpoint_id, "name": c.name, "order": c.ordering, "distances": json.loads(c.distances) if c.distances else {}, "is_meta": c.is_meta}
            for c in cps
        ],
        categories=sorted(categories),
        distances=sorted(distances),
        genders=sorted(genders),
    )

# -----------------------
# Public: feed (latest events)
# -----------------------
@app.get("/api/v1/public/feed", tags=["public"], response_model=PublicFeedResponse)
def public_feed(
    checkpoint_id: str | None = None,
    category: str | None = None,
    distance_label: str | None = None,
    genero: str | None = None,
    q: str | None = None,
    limit: int = 50,
    since: str | None = None,
    db: Session = Depends(get_db),
):
    q_db = (
        db.query(Event)
        .filter(
            Event.deleted_at.is_(None),
            Event.status == "ok",
            (Event.bib_number_real.is_not(None)) | (Event.bib_number_pred.is_not(None)),
        )
        .order_by(Event.ts.desc())
    )
    if checkpoint_id:
        q_db = q_db.filter(Event.checkpoint_id == checkpoint_id)
    if since:
        q_db = q_db.filter(Event.ts >= parse_iso_dt(since))
    q_db = q_db.limit(min(limit, 500))
    events = q_db.all()

    # Join cyclist info in Python (simple)
    out: list[PublicEvent] = []
    for ev in events:
        bib = ev.bib_number_effective
        cyclist = None
        if bib is not None:
            cyclist = db.query(Cyclist).filter(Cyclist.numero == bib).first()

        if category and (not cyclist or cyclist.categoria != category):
            continue
        if distance_label and (not cyclist or cyclist.circuito != distance_label):
            continue
        if genero and (not cyclist or cyclist.genero != genero):
            continue

        if q:
            ql = q.lower()
            # Search by bib or name
            if bib is not None and ql in str(bib):
                pass
            elif cyclist and (ql in (cyclist.nombre + " " + cyclist.apellido).lower()):
                pass
            else:
                continue

        out.append(_event_to_public(ev, cyclist))

    return PublicFeedResponse(events=out)

# -----------------------
# Public: stats (cyclists by status)
# -----------------------
@app.get("/api/v1/public/stats", tags=["public"], response_model=PublicStats)
def public_stats(
    circuito: str | None = None,
    genero: str | None = None,
    categoria: str | None = None,
    db: Session = Depends(get_db),
):
    q_db = db.query(Cyclist)
    if circuito:
        q_db = q_db.filter(Cyclist.circuito == circuito)
    if genero:
        q_db = q_db.filter(Cyclist.genero == genero)
    if categoria:
        q_db = q_db.filter(Cyclist.categoria == categoria)

    cyclists = q_db.all()
    total = len(cyclists)
    en_carrera = sum(1 for c in cyclists if c.status == "en_carrera")
    llego = sum(1 for c in cyclists if c.status == "llego")
    abandono = sum(1 for c in cyclists if c.status == "abandono")

    def pct(x: int) -> float:
        return (float(x) / float(total) * 100.0) if total > 0 else 0.0

    return PublicStats(
        total=total,
        en_carrera=en_carrera,
        llego=llego,
        abandono=abandono,
        pct_en_carrera=pct(en_carrera),
        pct_llego=pct(llego),
        pct_abandono=pct(abandono),
        updated_at=datetime.now(timezone.utc),
    )

# -----------------------
# Public: leaderboard
# -----------------------
@app.get("/api/v1/public/leaderboard", tags=["public"], response_model=LeaderboardResponse)
def public_leaderboard(
    checkpoint_id: str,
    circuito: str | None = None,
    categoria: str | None = None,
    genero: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    events = (
        db.query(Event)
        .filter(
            Event.checkpoint_id == checkpoint_id,
            Event.deleted_at.is_(None),
            Event.status == "ok",
        )
        .order_by(Event.ts.asc())
        .all()
    )

    seen_bibs: set[int] = set()
    entries: list[LeaderboardEntry] = []
    position = 0

    for ev in events:
        bib = ev.bib_number_effective
        if bib is None or bib in seen_bibs:
            continue
        seen_bibs.add(bib)

        cyclist = db.query(Cyclist).filter(Cyclist.numero == bib).first()

        # Apply cyclist filters
        if circuito and (not cyclist or cyclist.circuito != circuito):
            continue
        if categoria and (not cyclist or cyclist.categoria != categoria):
            continue
        if genero and (not cyclist or cyclist.genero != genero):
            continue

        position += 1
        cyclist_name = None
        cyclist_category = None
        cyclist_distance_label = None
        elapsed = None
        if cyclist:
            cyclist_name = f"{cyclist.nombre} {cyclist.apellido}".strip()
            cyclist_category = cyclist.categoria
            cyclist_distance_label = cyclist.circuito
            elapsed = compute_elapsed_seconds(ev.ts, cyclist.hora_salida)

        entries.append(LeaderboardEntry(
            position=position,
            bib_number=bib,
            cyclist_name=cyclist_name,
            cyclist_category=cyclist_category,
            cyclist_distance_label=cyclist_distance_label,
            ts=ev.ts,
            elapsed_seconds=elapsed,
            checkpoint_id=checkpoint_id,
        ))

        if position >= min(limit, 500):
            break

    return LeaderboardResponse(checkpoint_id=checkpoint_id, entries=entries)

# -----------------------
# Assets: event image
# -----------------------
@app.get("/api/v1/public/events/{event_id}/image", tags=["public"])
def public_event_image(event_id: int, db: Session = Depends(get_db)):
    ev = db.query(Event).filter(Event.id == event_id, Event.deleted_at.is_(None)).first()
    if not ev or not ev.image_path or not os.path.exists(ev.image_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(ev.image_path)

# -----------------------
# Admin: create/list/update/delete events
# -----------------------
@app.post("/api/v1/admin/events", tags=["admin"], response_model=EventResponse, status_code=201)
async def admin_create_event(
    body: AdminEventCreate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    # Validate checkpoint exists
    cp = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == body.checkpoint_id).first()
    if not cp:
        raise HTTPException(status_code=400, detail=f"Checkpoint '{body.checkpoint_id}' not found")

    # Validate cyclist with that bib number exists
    cyclist = db.query(Cyclist).filter(Cyclist.numero == body.bib_number).first()
    if not cyclist:
        raise HTTPException(status_code=400, detail=f"Cyclist with numero {body.bib_number} not found")

    # Validate status
    valid_statuses = ("ok", "needs_review")
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    ts = body.ts or datetime.now(timezone.utc)
    status = body.status
    note = body.note

    # Duplicate check: if (bib, checkpoint) already has an ok event -> needs_review + note
    if status == "ok" and _find_duplicate_event(db, body.bib_number, body.checkpoint_id):
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

    # Auto llego if meta checkpoint (only for confirmed events)
    llego_changed = False
    if status == "ok":
        llego_changed = _auto_set_llego(db, body.bib_number, body.checkpoint_id, event_ts=ts)

    db.commit()
    db.refresh(ev)

    await _broadcast_public_event(db, ev, msg_type="event_created")
    if llego_changed:
        await _broadcast_stats(db)

    return _event_to_response(ev)

@app.get("/api/v1/admin/events", tags=["admin"], response_model=list[EventResponse])
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
        # Search in real or pred
        q_db = q_db.filter((Event.bib_number_real == bib_number) | (Event.bib_number_pred == bib_number))
    if has_image is not None:
        if has_image:
            q_db = q_db.filter(Event.image_path.is_not(None))
        else:
            q_db = q_db.filter(Event.image_path.is_(None))
    if min_conf is not None:
        q_db = q_db.filter(Event.conf.is_not(None)).filter(Event.conf >= float(min_conf))

    events = q_db.offset(max(skip, 0)).limit(min(limit, 500)).all()
    return [_event_to_response(e) for e in events]

@app.patch("/api/v1/admin/events/{event_id}", tags=["admin"], response_model=EventResponse)
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

    # If we now have an effective bib and conf ok, mark ok unless admin forced otherwise
    if ev.status == "needs_review":
        if ev.bib_number_effective is not None and (ev.conf is None or ev.conf >= settings.needs_review_min_conf):
            ev.status = "ok"

    # On bib assignment: check duplicates and auto-llego
    llego_changed = False
    if bib_changed and ev.bib_number_effective is not None and ev.status != "rejected":
        dup = _find_duplicate_event(db, ev.bib_number_effective, ev.checkpoint_id, exclude_id=ev.id)
        if dup:
            # Keep the earlier event, reject the later one
            if dup.ts <= ev.ts:
                ev.status = "rejected"
                ev.note = f"duplicate (kept event #{dup.id})"
            else:
                dup.status = "rejected"
                dup.note = f"duplicate (kept event #{ev.id})"
                db.add(dup)

        if ev.status != "rejected":
            llego_changed = _auto_set_llego(db, ev.bib_number_effective, ev.checkpoint_id, event_ts=ev.ts)

    # On ts change: sync cyclist.hora_llegada if this event is at a meta checkpoint
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
        await _broadcast_public_event(db, ev, msg_type="event_updated")
    elif was_public and not is_public:
        # Era visible pero ya no → decirle al frontend que lo saque
        msg = {"type": "event_deleted", "id": ev.id}
        await ws_manager.broadcast(_public_room_for_checkpoint(ev.checkpoint_id), msg)
        await ws_manager.broadcast(_public_room_for_checkpoint(None), msg)

    if llego_changed:
        await _broadcast_stats(db)

    return _event_to_response(ev)

@app.delete("/api/v1/admin/events/{event_id}", tags=["admin"])
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

    await ws_manager.broadcast(_public_room_for_checkpoint(ev.checkpoint_id), {"type": "event_deleted", "id": ev.id})
    await _broadcast_stats(db)
    return {"ok": True}

# -----------------------
# Admin: cyclists import + list + patch
# -----------------------
@app.post("/api/v1/admin/cyclists/import", tags=["admin"], response_model=ImportResponse)
async def admin_import_cyclists(
    mode: str = "upsert",  # upsert|replace
    file: UploadFile = File(...),
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    # Auto-detect delimiter (Excel in Spanish locales exports with ;)
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    reader = csv.DictReader(io.StringIO(text), delimiter=dialect.delimiter)

    expected = ["Nombre","Apellido","Numero","Circuito","Genero","Hora de Salida","Categoria","Localidad","Status"]
    # Allow slight header variations by stripping spaces
    headers = [h.strip() for h in (reader.fieldnames or [])]
    if headers != expected:
        return ImportResponse(
            message="CSV header mismatch",
            success_count=0,
            failed_count=0,
            total_processed=0,
            parse_errors=[{"expected": expected, "got": headers}],
            import_errors=[],
        )

    if mode == "replace":
        db.query(Cyclist).delete()
        db.commit()

    success = 0
    failed = 0
    parse_errors = []
    import_errors = []
    total = 0

    # Mapping for common status variations (Spanish → internal)
    _status_map = {
        "en carrera": "en_carrera",
        "en_carrera": "en_carrera",
        "llego": "llego",
        "llegó": "llego",
        "abandono": "abandono",
        "abandonó": "abandono",
    }

    for i, row in enumerate(reader, start=2):
        total += 1
        # Normalize keys: strip whitespace from header names
        row = {k.strip(): v for k, v in row.items() if k}
        try:
            numero = int(row["Numero"])
            if numero <= 0:
                raise ValueError("Numero must be > 0")
            raw_status = (row.get("Status") or "en_carrera").strip().lower()
            status = _status_map.get(raw_status, "en_carrera")

            data = dict(
                numero=numero,
                nombre=(row["Nombre"] or "").strip(),
                apellido=(row["Apellido"] or "").strip(),
                circuito=(row["Circuito"] or "").strip(),
                genero=(row["Genero"] or "").strip(),
                hora_salida=(row["Hora de Salida"] or "").strip(),
                categoria=(row["Categoria"] or "").strip(),
                localidad=(row["Localidad"] or "").strip(),
                status=status,
            )
            if not data["nombre"] or not data["apellido"]:
                raise ValueError("Nombre/Apellido required")

        except Exception as e:
            failed += 1
            parse_errors.append({"line": i, "error": str(e), "row": row})
            continue

        try:
            existing = db.query(Cyclist).filter(Cyclist.numero == data["numero"]).first()
            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
                db.add(existing)
            else:
                db.add(Cyclist(**data))
            db.commit()
            success += 1
        except Exception as e:
            db.rollback()
            failed += 1
            import_errors.append({"line": i, "error": str(e), "row": row})

    # Notify public stats updated (optional)
    await _broadcast_stats(db)

    return ImportResponse(
        message="Import finished",
        success_count=success,
        failed_count=failed,
        total_processed=total,
        parse_errors=parse_errors,
        import_errors=import_errors,
    )

@app.get("/api/v1/admin/cyclists", tags=["admin"], response_model=list[CyclistResponse])
def admin_list_cyclists(
    skip: int = 0,
    limit: int = 100,
    circuito: str | None = None,
    genero: str | None = None,
    categoria: str | None = None,
    status: str | None = None,
    search: str | None = None,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    q_db = db.query(Cyclist).order_by(Cyclist.numero.asc())
    if circuito:
        q_db = q_db.filter(Cyclist.circuito == circuito)
    if genero:
        q_db = q_db.filter(Cyclist.genero == genero)
    if categoria:
        q_db = q_db.filter(Cyclist.categoria == categoria)
    if status:
        q_db = q_db.filter(Cyclist.status == status)
    if search:
        s = search.strip().lower()
        if s.isdigit():
            q_db = q_db.filter(Cyclist.numero == int(s))
        else:
            q_db = q_db.filter((Cyclist.nombre.ilike(f"%{s}%")) | (Cyclist.apellido.ilike(f"%{s}%")))

    rows = q_db.offset(max(skip, 0)).limit(min(limit, 1000)).all()
    return rows

@app.get("/api/v1/admin/cyclists/export", tags=["admin"])
def admin_export_cyclists(
    circuito: str | None = None,
    genero: str | None = None,
    categoria: str | None = None,
    status: str | None = None,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    q_db = db.query(Cyclist).order_by(Cyclist.numero.asc())
    if circuito:
        q_db = q_db.filter(Cyclist.circuito == circuito)
    if genero:
        q_db = q_db.filter(Cyclist.genero == genero)
    if categoria:
        q_db = q_db.filter(Cyclist.categoria == categoria)
    if status:
        q_db = q_db.filter(Cyclist.status == status)

    rows = q_db.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Numero", "Nombre", "Apellido", "Circuito", "Genero",
        "Categoria", "Localidad", "Hora de Salida", "Status",
        "Hora de Llegada",
    ])
    for c in rows:
        writer.writerow([
            c.numero,
            c.nombre,
            c.apellido,
            c.circuito,
            c.genero,
            c.categoria,
            c.localidad,
            c.hora_salida,
            c.status,
            c.hora_llegada.strftime("%d/%m/%Y %H:%M:%S") if c.hora_llegada else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ciclistas.csv"},
    )

@app.post("/api/v1/admin/cyclists", tags=["admin"], response_model=CyclistResponse, status_code=201)
async def admin_create_cyclist(
    body: CyclistCreate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    # Validate unique numero
    existing = db.query(Cyclist).filter(Cyclist.numero == body.numero).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Cyclist with numero {body.numero} already exists")

    # Validate status
    valid_statuses = ("en_carrera", "llego", "abandono")
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    # Default hora_llegada if status is llego and not provided
    hora_llegada = body.hora_llegada
    if body.status == "llego" and hora_llegada is None:
        hora_llegada = datetime.now(timezone.utc)

    c = Cyclist(
        numero=body.numero,
        nombre=body.nombre,
        apellido=body.apellido,
        circuito=body.circuito,
        genero=body.genero,
        hora_salida=body.hora_salida,
        categoria=body.categoria,
        localidad=body.localidad,
        status=body.status,
        hora_llegada=hora_llegada,
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    await _broadcast_stats(db)
    return c

@app.patch("/api/v1/admin/cyclists/{cyclist_id}", tags=["admin"], response_model=CyclistResponse)
async def admin_update_cyclist(
    cyclist_id: int,
    body: CyclistUpdate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    c = db.query(Cyclist).filter(Cyclist.id == cyclist_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cyclist not found")

    updates = body.model_dump(exclude_unset=True)

    # If status changes to "llego" and no hora_llegada provided, default to now
    if updates.get("status") == "llego" and "hora_llegada" not in updates:
        updates["hora_llegada"] = datetime.now(timezone.utc)

    for field, val in updates.items():
        setattr(c, field, val)

    db.add(c)
    db.commit()
    db.refresh(c)

    await _broadcast_stats(db)
    return c

# -----------------------
# Admin: checkpoints CRUD
# -----------------------
@app.get("/api/v1/admin/checkpoints", tags=["admin"])
def admin_list_checkpoints(
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    cps = db.query(Checkpoint).order_by(Checkpoint.ordering.asc()).all()
    return [_checkpoint_to_response(cp) for cp in cps]

@app.post("/api/v1/admin/checkpoints", tags=["admin"], status_code=201)
def admin_create_checkpoint(
    body: CheckpointCreate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Checkpoint).filter(Checkpoint.checkpoint_id == body.checkpoint_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="checkpoint_id already exists")
    # Exclusive toggle: unset other metas when setting is_meta=True
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
    return _checkpoint_to_response(cp)

@app.patch("/api/v1/admin/checkpoints/{checkpoint_id}", tags=["admin"])
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
    # Exclusive toggle: unset other metas when setting is_meta=True
    if updates.get("is_meta") is True:
        db.query(Checkpoint).filter(Checkpoint.is_meta == True, Checkpoint.id != cp.id).update({"is_meta": False})
    for field, val in updates.items():
        setattr(cp, field, val)
    db.add(cp)
    db.commit()
    db.refresh(cp)
    return _checkpoint_to_response(cp)

@app.delete("/api/v1/admin/checkpoints/{checkpoint_id}", tags=["admin"])
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

# -----------------------
# Admin: dashboard, devices, categories, settings (read-only)
# -----------------------
@app.get("/api/v1/admin/dashboard", tags=["admin"])
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

@app.get("/api/v1/admin/devices", tags=["admin"])
def admin_list_devices(
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    devices = db.query(Device).order_by(Device.created_at.desc()).all()
    # Aggregate event stats per device
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

@app.post("/api/v1/admin/devices", tags=["admin"], status_code=201)
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

@app.patch("/api/v1/admin/devices/{device_id}", tags=["admin"])
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
    # Get event stats
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

@app.delete("/api/v1/admin/devices/{device_id}", tags=["admin"])
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

@app.get("/api/v1/admin/categories", tags=["admin"])
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

@app.get("/api/v1/admin/settings", tags=["admin"])
def admin_get_settings(admin_user: str = Depends(get_current_admin)):
    return {
        "finish_checkpoint_id": settings.finish_checkpoint_id,
        "needs_review_min_conf": settings.needs_review_min_conf,
        "device_api_key_hint": settings.device_api_key[:4] + "****"
        if len(settings.device_api_key) > 4
        else "****",
    }

# -----------------------
# WebSocket: public realtime
# -----------------------
@app.websocket("/ws/public")
async def ws_public(ws: WebSocket):
    checkpoint_id = ws.query_params.get("checkpoint_id")  # optional room filter

    room = _public_room_for_checkpoint(checkpoint_id)
    await ws_manager.connect(ws, room=room)

    await ws.send_json({
        "type": "hello",
        "room": room,
        "server_time": datetime.now(timezone.utc).isoformat(),
    })

    ping_task = asyncio.create_task(_ws_ping_loop(ws))
    try:
        while True:
            # We don't require any client message, but receive_text keeps the connection state
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ping_task.cancel()
        await ws_manager.disconnect(ws)

async def _ws_ping_loop(ws: WebSocket):
    while True:
        await asyncio.sleep(25)
        try:
            await ws.send_json({"type": "ping", "ts": datetime.now(timezone.utc).isoformat()})
        except Exception:
            break

# -----------------------
# Helpers: response formatting + broadcasting
# -----------------------
def _event_image_url(event_id: int) -> str:
    return f"/api/v1/public/events/{event_id}/image"

def _event_to_response(ev: Event) -> EventResponse:
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
        image_url=_event_image_url(ev.id) if ev.image_path else None,
        created_at=ev.created_at,
    )

def _event_to_public(ev: Event, cyclist: Cyclist | None) -> PublicEvent:
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
        image_url=_event_image_url(ev.id) if ev.image_path else None,
    )

def _public_room_for_checkpoint(checkpoint_id: str | None) -> str:
    if checkpoint_id:
        return f"public:{checkpoint_id}"
    return "public:all"

async def _broadcast_public_event(db: Session, ev: Event, msg_type: str):
    # Solo transmitir eventos públicos: status="ok" con ciclista identificado
    if ev.status != "ok" or ev.bib_number_effective is None:
        return

    cyclist = db.query(Cyclist).filter(Cyclist.numero == ev.bib_number_effective).first()

    msg = {"type": msg_type, "data": _event_to_public(ev, cyclist).model_dump()}
    # broadcast to checkpoint room and also to all
    await ws_manager.broadcast(_public_room_for_checkpoint(ev.checkpoint_id), msg)
    await ws_manager.broadcast(_public_room_for_checkpoint(None), msg)

async def _broadcast_stats(db: Session):
    # Compute overall stats (no filters) and broadcast
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
    await ws_manager.broadcast(_public_room_for_checkpoint(None), payload)
