from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import engine
from app.migrations import run_migrations, wait_for_db
from app.models import Base

# Route modules
from app.routes.token import router as token_router
from app.routes.health import router as health_router
from app.routes.auth import router as auth_router
from app.routes.ingest import router as ingest_router
from app.routes.public import router as public_router
from app.routes.admin_events import router as admin_events_router
from app.routes.admin_cyclists import router as admin_cyclists_router
from app.routes.admin_checkpoints import router as admin_checkpoints_router
from app.routes.admin_devices import router as admin_devices_router
from app.routes.admin_meta import router as admin_meta_router
from app.routes.websocket import router as websocket_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.upload_dir, exist_ok=True)
    wait_for_db()
    Base.metadata.create_all(bind=engine)
    run_migrations()
    yield


app = FastAPI(title="Raid Control API", version="0.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(token_router)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(ingest_router)
app.include_router(public_router)
app.include_router(admin_events_router)
app.include_router(admin_cyclists_router)
app.include_router(admin_checkpoints_router)
app.include_router(admin_devices_router)
app.include_router(admin_meta_router)
app.include_router(websocket_router)
