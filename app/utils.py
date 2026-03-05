from __future__ import annotations

from datetime import datetime


def parse_iso_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime string, accepting 'Z' suffix."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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


def compute_elapsed_seconds(event_ts: datetime, hora_salida_str: str | None) -> int | None:
    """Compute elapsed seconds between event timestamp and cyclist start time."""
    if not hora_salida_str:
        return None
    start = _parse_hora_salida(hora_salida_str)
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
