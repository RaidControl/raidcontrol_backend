from __future__ import annotations

import time

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import engine


def run_migrations():
    """Run lightweight schema migrations for changes not handled by create_all."""
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

    # Migration: race_settings table
    if "race_settings" not in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE race_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    `key` VARCHAR(64) NOT NULL UNIQUE,
                    value TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
            """))


    # Migration: DATETIME → DATETIME(3) for millisecond precision
    if "events" in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE events MODIFY ts DATETIME(3) NOT NULL"))
            conn.execute(text("ALTER TABLE events MODIFY created_at DATETIME(3) NOT NULL"))
            conn.execute(text("ALTER TABLE events MODIFY deleted_at DATETIME(3) NULL"))
    if "cyclists" in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE cyclists MODIFY hora_llegada DATETIME(3) NULL"))


def wait_for_db():
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
