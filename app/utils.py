from __future__ import annotations
import os
from datetime import datetime
from fastapi import UploadFile

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def parse_iso_dt(s: str) -> datetime:
    # Accept "Z" suffix
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_hora_salida(s: str) -> datetime | None:
    """Parse hora_salida from CSV format 'DD/MM/AAAA HH:MM' into naive datetime."""
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def compute_elapsed_seconds(event_ts: datetime, hora_salida_str: str | None) -> int | None:
    """Compute elapsed seconds between event timestamp and cyclist start time."""
    if not hora_salida_str:
        return None
    start = parse_hora_salida(hora_salida_str)
    if start is None:
        return None
    # If hora_salida is time-only (HH:MM), combine with event date
    if start.year == 1900:  # strptime default year for time-only formats
        start = start.replace(year=event_ts.year, month=event_ts.month, day=event_ts.day)
    # Strip timezone from event_ts for comparison with naive start
    event_naive = event_ts.replace(tzinfo=None) if event_ts.tzinfo else event_ts
    diff = event_naive - start
    secs = int(diff.total_seconds())
    return secs if secs >= 0 else None


async def save_upload(upload_dir: str, file: UploadFile) -> str:
    ensure_dir(upload_dir)
    filename = file.filename or "upload.jpg"
    _, ext = os.path.splitext(filename)
    ext = (ext or ".jpg").lower()
    name = f"{os.urandom(16).hex()}{ext}"
    out_path = os.path.join(upload_dir, name)

    content = await file.read()
    with open(out_path, "wb") as f:
        f.write(content)
    return out_path
