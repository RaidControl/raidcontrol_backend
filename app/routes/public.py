from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.helpers import event_to_public
from app.models import Checkpoint, Cyclist, Event, RaceSetting
from app.schemas import (
    CyclistDetection,
    CyclistSearchResponse,
    CyclistSearchResult,
    FiltersResponse,
    LeaderboardEntry,
    LeaderboardResponse,
    PublicCyclistDetail,
    PublicEvent,
    PublicFeedResponse,
    PublicStats,
    RaceSettingsPublic,
)
from app.storage import get_image_url, is_spaces_path
from app.utils import compute_elapsed_seconds, parse_iso_dt

router = APIRouter(prefix="/api/v1/public", tags=["public"])


@router.get("/race-settings", response_model=RaceSettingsPublic)
def public_race_settings(db: Session = Depends(get_db)):
    row = db.query(RaceSetting).filter(RaceSetting.key == "race_start_time").first()
    if not row or not row.value:
        return RaceSettingsPublic(race_start_time=None, countdown_active=False)

    try:
        target = datetime.fromisoformat(row.value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        is_active = target > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return RaceSettingsPublic(race_start_time=row.value, countdown_active=False)

    return RaceSettingsPublic(race_start_time=row.value, countdown_active=is_active)


@router.get("/filters", response_model=FiltersResponse)
def public_filters(db: Session = Depends(get_db)):
    cps = db.query(Checkpoint).order_by(Checkpoint.ordering.asc()).all()
    categories = [r[0] for r in db.query(Cyclist.categoria).distinct().all() if r[0]]
    distances = [r[0] for r in db.query(Cyclist.circuito).distinct().all() if r[0]]
    genders = [r[0] for r in db.query(Cyclist.genero).distinct().all() if r[0]]

    return FiltersResponse(
        checkpoints=[
            {
                "checkpoint_id": c.checkpoint_id,
                "name": c.name,
                "order": c.ordering,
                "distances": json.loads(c.distances) if c.distances else {},
                "is_meta": c.is_meta,
            }
            for c in cps
        ],
        categories=sorted(categories),
        distances=sorted(distances),
        genders=sorted(genders),
    )


@router.get("/feed", response_model=PublicFeedResponse)
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
            if bib is not None and ql in str(bib):
                pass
            elif cyclist and (ql in (cyclist.nombre + " " + cyclist.apellido).lower()):
                pass
            else:
                continue

        out.append(event_to_public(ev, cyclist))

    return PublicFeedResponse(events=out)


@router.get("/stats", response_model=PublicStats)
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


@router.get("/leaderboard", response_model=LeaderboardResponse)
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


@router.get("/cyclists/search", response_model=CyclistSearchResponse)
def public_cyclist_search(q: str, db: Session = Depends(get_db)):
    q = q.strip()
    if not q:
        return CyclistSearchResponse(results=[])

    if q.isdigit():
        rows = db.query(Cyclist).filter(Cyclist.numero == int(q)).limit(10).all()
    else:
        s = q.lower()
        rows = (
            db.query(Cyclist)
            .filter((Cyclist.nombre.ilike(f"%{s}%")) | (Cyclist.apellido.ilike(f"%{s}%")))
            .order_by(Cyclist.numero.asc())
            .limit(10)
            .all()
        )

    return CyclistSearchResponse(
        results=[
            CyclistSearchResult(
                id=c.id,
                numero=c.numero,
                nombre=c.nombre,
                apellido=c.apellido,
                circuito=c.circuito,
                genero=c.genero,
                categoria=c.categoria,
                status=c.status,
            )
            for c in rows
        ]
    )


@router.get("/cyclists/{numero}", response_model=PublicCyclistDetail)
def public_cyclist_detail(numero: int, db: Session = Depends(get_db)):
    cyclist = db.query(Cyclist).filter(Cyclist.numero == numero).first()
    if not cyclist:
        raise HTTPException(status_code=404, detail="Cyclist not found")

    events = (
        db.query(Event)
        .filter(
            Event.deleted_at.is_(None),
            Event.status == "ok",
            (Event.bib_number_real == numero)
            | (Event.bib_number_real.is_(None) & (Event.bib_number_pred == numero)),
        )
        .order_by(Event.ts.asc())
        .all()
    )

    # Build checkpoint name map
    checkpoint_ids = {ev.checkpoint_id for ev in events}
    checkpoints = db.query(Checkpoint).filter(Checkpoint.checkpoint_id.in_(checkpoint_ids)).all() if checkpoint_ids else []
    cp_name_map = {cp.checkpoint_id: cp.name for cp in checkpoints}

    detections = []
    for ev in events:
        detections.append(CyclistDetection(
            event_id=ev.id,
            checkpoint_id=ev.checkpoint_id,
            checkpoint_name=cp_name_map.get(ev.checkpoint_id),
            ts=ev.ts,
            elapsed_seconds=compute_elapsed_seconds(ev.ts, cyclist.hora_salida),
            image_url=get_image_url(ev.image_path, ev.id),
        ))

    return PublicCyclistDetail(
        id=cyclist.id,
        numero=cyclist.numero,
        nombre=cyclist.nombre,
        apellido=cyclist.apellido,
        circuito=cyclist.circuito,
        genero=cyclist.genero,
        categoria=cyclist.categoria,
        localidad=cyclist.localidad,
        hora_salida=cyclist.hora_salida,
        status=cyclist.status,
        hora_llegada=cyclist.hora_llegada,
        detections=detections,
    )


@router.get("/events/{event_id}/image")
def public_event_image(event_id: int, db: Session = Depends(get_db)):
    ev = db.query(Event).filter(Event.id == event_id, Event.deleted_at.is_(None)).first()
    if not ev or not ev.image_path:
        raise HTTPException(status_code=404, detail="Image not found")

    # Spaces: redirect to CDN
    if is_spaces_path(ev.image_path):
        print(f"Redirecting to CDN for event {event_id} image")
        print(f"Image path: {ev.image_path}")
        cdn_url = get_image_url(ev.image_path, ev.id)
        if cdn_url:
            return RedirectResponse(url=cdn_url, status_code=302)

    # Local: serve file
    if not os.path.exists(ev.image_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(ev.image_path)
