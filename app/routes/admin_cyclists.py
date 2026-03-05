from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_admin
from app.helpers import broadcast_stats
from app.models import Cyclist
from app.schemas import CyclistCreate, CyclistResponse, CyclistUpdate, ImportResponse

router = APIRouter(prefix="/api/v1/admin/cyclists", tags=["admin"])


@router.post("/import", response_model=ImportResponse)
async def admin_import_cyclists(
    mode: str = "upsert",
    file: UploadFile = File(...),
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    reader = csv.DictReader(io.StringIO(text), delimiter=dialect.delimiter)

    expected = ["Nombre", "Apellido", "Numero", "Circuito", "Genero", "Hora de Salida", "Categoria", "Localidad", "Status"]
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

    await broadcast_stats(db)

    return ImportResponse(
        message="Import finished",
        success_count=success,
        failed_count=failed,
        total_processed=total,
        parse_errors=parse_errors,
        import_errors=import_errors,
    )


@router.get("", response_model=list[CyclistResponse])
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


@router.get("/export")
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
    writer = csv.writer(output, delimiter=';')
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


@router.post("", response_model=CyclistResponse, status_code=201)
async def admin_create_cyclist(
    body: CyclistCreate,
    admin_user: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Cyclist).filter(Cyclist.numero == body.numero).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Cyclist with numero {body.numero} already exists")

    valid_statuses = ("en_carrera", "llego", "abandono")
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

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

    await broadcast_stats(db)
    return c


@router.patch("/{cyclist_id}", response_model=CyclistResponse)
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

    if updates.get("status") == "llego" and "hora_llegada" not in updates:
        updates["hora_llegada"] = datetime.now(timezone.utc)

    for field, val in updates.items():
        setattr(c, field, val)

    db.add(c)
    db.commit()
    db.refresh(c)

    await broadcast_stats(db)
    return c
