from __future__ import annotations
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator


def _to_utc(v: datetime | None) -> datetime | None:
    """Ensure naive datetimes from MySQL are marked as UTC."""
    if v is not None and isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


# -----------------------
# Auth
# -----------------------
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

# -----------------------
# Public
# -----------------------
class PublicEvent(BaseModel):
    id: int
    ts: datetime
    checkpoint_id: str
    device_id: str

    bib_number: int | None
    confidence: float | None
    cyclist_name: str | None = None
    cyclist_category: str | None = None
    cyclist_distance_label: str | None = None
    elapsed_seconds: int | None = None

    status: str
    image_url: str | None = None

    _ensure_utc_ts = field_validator("ts", mode="before")(_to_utc)

class PublicFeedResponse(BaseModel):
    events: list[PublicEvent]

class PublicStats(BaseModel):
    total: int
    en_carrera: int
    llego: int
    abandono: int
    pct_en_carrera: float
    pct_llego: float
    pct_abandono: float
    updated_at: datetime

    _ensure_utc_updated_at = field_validator("updated_at", mode="before")(_to_utc)

class CyclistSearchResult(BaseModel):
    id: int
    numero: int
    nombre: str
    apellido: str
    circuito: str | None
    genero: str | None
    categoria: str | None
    status: str

class CyclistSearchResponse(BaseModel):
    results: list[CyclistSearchResult]

class CyclistDetection(BaseModel):
    event_id: int
    checkpoint_id: str
    checkpoint_name: str | None
    ts: datetime
    elapsed_seconds: int | None
    image_url: str | None

    _ensure_utc_ts = field_validator("ts", mode="before")(_to_utc)

class PublicCyclistDetail(BaseModel):
    id: int
    numero: int
    nombre: str
    apellido: str
    circuito: str | None
    genero: str | None
    categoria: str | None
    localidad: str | None
    hora_salida: str | None
    status: str
    hora_llegada: datetime | None
    detections: list[CyclistDetection]

    _ensure_utc_hora_llegada = field_validator("hora_llegada", mode="before")(_to_utc)

class FiltersResponse(BaseModel):
    checkpoints: list[dict]
    categories: list[str]
    distances: list[str]
    genders: list[str] = Field(default_factory=list)

# -----------------------
# Admin / DB
# -----------------------
class EventResponse(BaseModel):
    id: int
    ts: datetime
    device_id: str
    checkpoint_id: str

    bib_number_pred: int | None
    bib_number_real: int | None
    bib_number_effective: int | None
    conf: float | None

    plate_color: str | None
    status: str
    note: str | None

    image_url: str | None
    created_at: datetime

    _ensure_utc_ts = field_validator("ts", "created_at", mode="before")(_to_utc)

    class Config:
        from_attributes = True

class AdminEventCreate(BaseModel):
    bib_number: int
    checkpoint_id: str
    ts: datetime | None = None
    status: str = "ok"
    note: str | None = None

class EventUpdateRequest(BaseModel):
    bib_number_real: int | None = None
    status: str | None = None
    note: str | None = None
    ts: datetime | None = None

class ImportResponse(BaseModel):
    message: str
    success_count: int
    failed_count: int
    total_processed: int
    parse_errors: list[dict] = Field(default_factory=list)
    import_errors: list[dict] = Field(default_factory=list)

class CyclistResponse(BaseModel):
    id: int
    numero: int
    nombre: str
    apellido: str
    circuito: str
    genero: str
    hora_salida: str
    categoria: str
    localidad: str
    status: str
    hora_llegada: datetime | None = None

    _ensure_utc_hora_llegada = field_validator("hora_llegada", mode="before")(_to_utc)

    class Config:
        from_attributes = True

class CyclistCreate(BaseModel):
    numero: int
    nombre: str
    apellido: str
    circuito: str = ""
    genero: str = ""
    hora_salida: str = ""
    categoria: str = ""
    localidad: str = ""
    status: str = "en_carrera"
    hora_llegada: datetime | None = None

class CyclistUpdate(BaseModel):
    nombre: str | None = None
    apellido: str | None = None
    localidad: str | None = None
    status: str | None = None
    circuito: str | None = None
    genero: str | None = None
    categoria: str | None = None
    hora_salida: str | None = None
    hora_llegada: datetime | None = None

# -----------------------
# Ingest (device / uploader)
# -----------------------
class LeaderboardEntry(BaseModel):
    position: int
    bib_number: int
    cyclist_name: str | None = None
    cyclist_category: str | None = None
    cyclist_distance_label: str | None = None
    ts: datetime
    elapsed_seconds: int | None = None
    checkpoint_id: str

    _ensure_utc_ts = field_validator("ts", mode="before")(_to_utc)

class LeaderboardResponse(BaseModel):
    checkpoint_id: str
    entries: list[LeaderboardEntry]

# -----------------------
# Ingest (device / uploader)
# -----------------------
class IngestCreateEventRequest(BaseModel):
    station_id: str
    device_id: str | None = None
    detected_at: str
    number_str: str
    meta: dict = Field(default_factory=dict)

class IngestCreateEventResponse(BaseModel):
    event_id: int
    id: int
    status: str

# -----------------------
# Checkpoints admin
# -----------------------
class CheckpointResponse(BaseModel):
    id: int
    checkpoint_id: str
    name: str
    ordering: int
    distances: dict[str, float] = Field(default_factory=dict)
    is_meta: bool = False

class CheckpointCreate(BaseModel):
    checkpoint_id: str
    name: str
    ordering: int = 0
    distances: dict[str, float] = Field(default_factory=dict)
    is_meta: bool = False

class CheckpointUpdate(BaseModel):
    name: str | None = None
    ordering: int | None = None
    distances: dict[str, float] | None = None
    is_meta: bool | None = None

# -----------------------
# Devices admin
# -----------------------
class DeviceResponse(BaseModel):
    id: int
    device_id: str
    name: str
    checkpoint_id: str | None
    is_active: bool
    created_at: datetime
    event_count: int = 0
    last_seen: datetime | None = None

    _ensure_utc_dt = field_validator("created_at", "last_seen", mode="before")(_to_utc)

class DeviceCreate(BaseModel):
    device_id: str
    name: str = ""
    checkpoint_id: str | None = None
    is_active: bool = True

class DeviceUpdate(BaseModel):
    name: str | None = None
    checkpoint_id: str | None = None
    is_active: bool | None = None

# -----------------------
# Race Settings
# -----------------------
class RaceSettingsPublic(BaseModel):
    race_start_time: str | None = None
    countdown_active: bool = False

class RaceSettingsUpdate(BaseModel):
    race_start_time: str | None = None
