from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, String, Float, DateTime, Text

class Base(DeclarativeBase):
    pass

class Cyclist(Base):
    __tablename__ = "cyclists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    numero: Mapped[int] = mapped_column(Integer, unique=True, index=True)

    nombre: Mapped[str] = mapped_column(String(255))
    apellido: Mapped[str] = mapped_column(String(255))

    circuito: Mapped[str] = mapped_column(String(64))
    genero: Mapped[str] = mapped_column(String(32))
    hora_salida: Mapped[str] = mapped_column(String(32))  # "DD/MM/AAAA HH:MM" or "HH:MM"
    categoria: Mapped[str] = mapped_column(String(64))
    localidad: Mapped[str] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(String(32), default="en_carrera")  # en_carrera|llego|abandono
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class Checkpoint(Base):
    __tablename__ = "checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # e.g. "pc1", "finish"
    name: Mapped[str] = mapped_column(String(255))
    ordering: Mapped[int] = mapped_column(Integer, default=0)
    distance_km: Mapped[float] = mapped_column(Float, default=0.0)

class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)

    device_id: Mapped[str] = mapped_column(String(64), index=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), index=True)

    bib_number_pred: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    bib_number_real: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    conf: Mapped[float | None] = mapped_column(Float, nullable=True)
    plate_color: Mapped[str | None] = mapped_column(String(32), nullable=True)

    bbox_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="ok")  # ok|needs_review|rejected
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def bib_number_effective(self) -> int | None:
        return self.bib_number_real if self.bib_number_real is not None else self.bib_number_pred
