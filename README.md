# Raid Control Backend

Backend para tracking en tiempo real de carreras de ciclismo. Recibe detecciones desde dispositivos edge (Raspberry Pi + YOLO/OCR), almacena eventos y los expone via REST API + WebSockets.

## Stack

- FastAPI 0.115 + Python 3.11
- SQLAlchemy 2.0 + MySQL 8.0
- JWT (admin) + API Key (dispositivos)
- WebSockets para tiempo real
- Docker Compose

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Swagger UI: http://localhost:8000/docs

Para desarrollo con hot reload:
```bash
docker compose watch
```

## Variables de entorno (.env)

| Variable | Default | Descripcion |
|----------|---------|-------------|
| `DB_HOST` | — | Host de MySQL |
| `DB_PORT` | `3306` | Puerto de MySQL |
| `DB_NAME` | — | Nombre de la base |
| `DB_USER` | — | Usuario MySQL |
| `DB_PASSWORD` | — | Password MySQL |
| `DEVICE_API_KEY` | — | Key para autenticar dispositivos |
| `ADMIN_USERNAME` | — | Usuario admin |
| `ADMIN_PASSWORD` | — | Password admin |
| `JWT_SECRET` | — | Secret para firmar JWT |
| `JWT_EXPIRES_MIN` | `720` | Expiracion del token en minutos |
| `UPLOAD_DIR` | `uploads` | Carpeta para imagenes |
| `NEEDS_REVIEW_MIN_CONF` | `0.60` | Confianza minima para marcar "ok" |
| `FINISH_CHECKPOINT_ID` | `finish` | Checkpoint ID de meta (deprecated, usar is_meta) |

## Autenticacion

**Dispositivos (ingest):** header `X-Device-Key` con el valor de `DEVICE_API_KEY`.

**Admin:** JWT Bearer token. Obtener con login:

```bash
# Login JSON
curl -X POST http://localhost:8000/api/v1/admin/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# Login OAuth2 (form-data, para Swagger UI)
curl -X POST http://localhost:8000/token \
  -d "username=admin&password=admin123"
```

Respuesta: `{"access_token": "...", "token_type": "bearer"}`

Usar en requests admin: `Authorization: Bearer <token>`

---

## Endpoints

### Health

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/health` | Health check (verifica conexion a DB) |

### Publicos (sin auth)

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/v1/public/filters` | Checkpoints, categorias, distancias y generos disponibles |
| GET | `/api/v1/public/feed` | Feed de eventos recientes |
| GET | `/api/v1/public/stats` | Estadisticas de ciclistas por status |
| GET | `/api/v1/public/leaderboard` | Ranking por checkpoint |
| GET | `/api/v1/public/events/{event_id}/image` | Imagen de un evento |

#### GET /api/v1/public/feed

Query params:
- `checkpoint_id` — filtrar por checkpoint
- `category` — filtrar por categoria
- `distance_label` — filtrar por circuito
- `genero` — filtrar por genero
- `q` — buscar por dorsal o nombre
- `limit` — max resultados (default 50, max 500)
- `since` — ISO datetime, solo eventos posteriores

Solo devuelve eventos con `status=ok` y ciclista identificado.

#### GET /api/v1/public/stats

Query params: `circuito`, `genero`, `categoria`

```json
{
  "total": 150,
  "en_carrera": 100,
  "llego": 40,
  "abandono": 10,
  "pct_en_carrera": 66.67,
  "pct_llego": 26.67,
  "pct_abandono": 6.67,
  "updated_at": "2026-02-28T12:00:00+00:00"
}
```

#### GET /api/v1/public/leaderboard

Query params:
- `checkpoint_id` **(requerido)** — checkpoint del ranking
- `circuito`, `categoria`, `genero` — filtros opcionales
- `limit` — max resultados (default 100)

### Ingest (dispositivos)

Todos requieren header `X-Device-Key`.

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| POST | `/api/v1/ingest/events/upload` | Crear evento + imagen (todo en uno) |
| POST | `/api/v1/stations/{station_id}/events` | Crear evento (paso 1, JSON) |
| POST | `/api/v1/events/{event_id}/image` | Subir imagen (paso 2) |

#### POST /api/v1/ingest/events/upload (multipart)

Todo en un solo request. Form fields:
- `ts` **(requerido)** — timestamp ISO
- `checkpoint_id` **(requerido)** — ej: "pc1", "finish"
- `device_id` **(requerido)** — ej: "rpi-01"
- `file` **(requerido)** — imagen
- `bib_number_pred` — dorsal predicho por OCR
- `conf` — confianza (0-1)
- `plate_color` — color de la placa
- `bbox_json` — bounding box JSON
- `meta_json` — metadata extra JSON

```bash
curl -X POST "http://localhost:8000/api/v1/ingest/events/upload" \
  -H "X-Device-Key: changeme-device-key" \
  -F "ts=2026-02-28T12:00:00-03:00" \
  -F "checkpoint_id=pc1" \
  -F "device_id=rpi-01" \
  -F "bib_number_pred=42" \
  -F "conf=0.85" \
  -F "file=@crop.jpg"
```

#### POST /api/v1/stations/{station_id}/events (JSON, dos pasos)

Usado por el script uploader de la Raspberry Pi. Paso 1: crear evento sin imagen.

```json
{
  "station_id": "pc1",
  "device_id": "rpi-01",
  "detected_at": "2026-02-28T12:00:00-03:00",
  "number_str": "42",
  "meta": {
    "confidence": 0.85,
    "plate_color": "yellow"
  }
}
```

Respuesta: `{"event_id": 1, "id": 1, "status": "ok"}`

Paso 2: subir imagen al evento creado.

```bash
curl -X POST "http://localhost:8000/api/v1/events/1/image" \
  -H "X-Device-Key: changeme-device-key" \
  -F "file=@crop.jpg"
```

### Auth

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| POST | `/token` | Login OAuth2 (form-data) |
| POST | `/api/v1/admin/auth/login` | Login JSON |

### Admin — Eventos

Todos requieren `Authorization: Bearer <token>`.

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/v1/admin/events` | Listar eventos |
| POST | `/api/v1/admin/events` | Crear evento manual |
| PATCH | `/api/v1/admin/events/{event_id}` | Corregir evento |
| DELETE | `/api/v1/admin/events/{event_id}` | Eliminar evento (soft delete) |

#### GET /api/v1/admin/events

Query params: `limit`, `skip`, `status`, `needs_review`, `checkpoint_id`, `bib_number`, `has_image`, `min_conf`

#### POST /api/v1/admin/events

```json
{
  "bib_number": 42,
  "checkpoint_id": "pc1",
  "ts": "2026-02-28T12:00:00Z",
  "status": "ok",
  "note": "agregado manual"
}
```

#### PATCH /api/v1/admin/events/{event_id}

```json
{
  "bib_number_real": 42,
  "status": "ok",
  "note": "corregido",
  "ts": "2026-02-28T12:00:00Z"
}
```

### Admin — Ciclistas

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/v1/admin/cyclists` | Listar ciclistas |
| POST | `/api/v1/admin/cyclists` | Crear ciclista |
| PATCH | `/api/v1/admin/cyclists/{cyclist_id}` | Actualizar ciclista |
| POST | `/api/v1/admin/cyclists/import` | Importar CSV |
| GET | `/api/v1/admin/cyclists/export` | Exportar CSV |

#### GET /api/v1/admin/cyclists

Query params: `skip`, `limit`, `circuito`, `genero`, `categoria`, `status`, `search`

#### POST /api/v1/admin/cyclists/import

Multipart form-data. Query param `mode`: `upsert` (default) o `replace`.

```bash
curl -X POST "http://localhost:8000/api/v1/admin/cyclists/import?mode=upsert" \
  -H "Authorization: Bearer <token>" \
  -F "file=@ciclistas.csv"
```

CSV headers (exactos):
```
Nombre,Apellido,Numero,Circuito,Genero,Hora de Salida,Categoria,Localidad,Status
```

### Admin — Checkpoints

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/v1/admin/checkpoints` | Listar checkpoints |
| POST | `/api/v1/admin/checkpoints` | Crear checkpoint |
| PATCH | `/api/v1/admin/checkpoints/{checkpoint_id}` | Actualizar checkpoint |
| DELETE | `/api/v1/admin/checkpoints/{checkpoint_id}` | Eliminar (409 si tiene eventos) |

```json
{
  "checkpoint_id": "pc1",
  "name": "Puesto de Control 1",
  "ordering": 1,
  "distances": {"100km": 60.0, "50km": 30.0},
  "is_meta": false
}
```

### Admin — Dispositivos

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/v1/admin/devices` | Listar dispositivos |
| POST | `/api/v1/admin/devices` | Crear dispositivo |
| PATCH | `/api/v1/admin/devices/{device_id}` | Actualizar dispositivo |
| DELETE | `/api/v1/admin/devices/{device_id}` | Eliminar dispositivo |

```json
{
  "device_id": "rpi-01",
  "name": "Raspberry Pi Puesto 1",
  "checkpoint_id": "pc1",
  "is_active": true
}
```

### Admin — Otros

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| GET | `/api/v1/admin/dashboard` | Stats resumen |
| GET | `/api/v1/admin/categories` | Categorias con count de ciclistas |
| GET | `/api/v1/admin/settings` | Configuracion actual (read-only) |

---

## WebSocket

```
ws://localhost:8000/ws/public
ws://localhost:8000/ws/public?checkpoint_id=pc1
```

Solo transmite eventos con `status=ok` y ciclista identificado.

Mensajes del servidor:

| Tipo | Cuando |
|------|--------|
| `hello` | Al conectar |
| `ping` | Cada 25 segundos |
| `event_created` | Nuevo evento publico |
| `event_updated` | Evento actualizado |
| `event_deleted` | Evento eliminado o dejo de ser publico |
| `stats_updated` | Cambio en estadisticas de ciclistas |

Formato:
```json
{
  "type": "event_created",
  "data": {
    "id": 1,
    "ts": "2026-02-28T12:00:00+00:00",
    "checkpoint_id": "pc1",
    "bib_number": 42,
    "cyclist_name": "Juan Perez",
    "status": "ok"
  }
}
```

---

## Reglas de negocio

- **Dorsal unico**: `bib_number` no se repite entre ciclistas
- **Duplicados**: si un dorsal ya tiene evento OK en un checkpoint, el nuevo va a `needs_review`
- **Auto "llego"**: evento OK en checkpoint meta → ciclista pasa a `status=llego` (no sobrescribe `abandono`)
- **Confianza**: eventos con confianza < 0.60 van a `needs_review`
- **Soft delete**: eventos eliminados se marcan con `deleted_at`, no se borran fisicamente
