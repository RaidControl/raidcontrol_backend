from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings


def parse_iso_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime string, accepting 'Z' suffix.
    Always returns a UTC datetime (naive) for consistent MySQL storage."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_hora_salida(s: str) -> datetime | None:
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


def _utc_to_local(dt: datetime) -> datetime:
    """Convert a naive UTC datetime to local time using configured offset."""
    return dt + timedelta(hours=settings.local_tz_offset_hours)


def compute_elapsed_seconds(event_ts: datetime, hora_salida_str: str | None) -> int | None:
    """Compute elapsed seconds between event timestamp and cyclist start time.

    event_ts is stored in UTC (naive). hora_salida is in local time.
    We convert event_ts to local time before comparing.
    """
    if not hora_salida_str:
        return None
    start = _parse_hora_salida(hora_salida_str)
    if start is None:
        return None
    # Convert event_ts from UTC to local time (hora_salida is local)
    event_naive = event_ts.replace(tzinfo=None) if event_ts.tzinfo else event_ts
    event_local = _utc_to_local(event_naive)
    # If hora_salida is time-only (HH:MM), combine with event date
    if start.year == 1900:  # strptime default year for time-only formats
        start = start.replace(year=event_local.year, month=event_local.month, day=event_local.day)
    diff = event_local - start
    secs = int(diff.total_seconds())
    return secs if secs >= 0 else None
