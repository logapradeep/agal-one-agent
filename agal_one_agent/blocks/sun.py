"""Sunrise / sunset (NOAA solar equations, ±3 min) for schedules with ``sunEvent``.

Pure function of date, latitude, longitude and timezone; no dependencies.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def _julian_day(d: date) -> float:
    a = (14 - d.month) // 12
    y = d.year + 4800 - a
    m = d.month + 12 * a - 3
    # Julian day number (noon-based), as the NOAA formulation expects.
    return float(d.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045)


def sun_event_utc(d: date, lat: float, lon: float, event: str) -> Optional[datetime]:
    """UTC datetime of sunrise/sunset on calendar day ``d`` at lat/lon, or None (polar)."""
    n = _julian_day(d) - 2451545.0 + 0.0008
    j_star = n - lon / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360.0)
    c = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180.0 + 102.9372) % 360.0)
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if cos_w < -1 or cos_w > 1:
        return None
    w = math.degrees(math.acos(cos_w))
    jd = j_transit + (w / 360.0 if event == "sunset" else -w / 360.0)
    epoch = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
    return epoch + timedelta(days=jd - 2451545.0)


def sun_event_local(d: date, lat: float, lon: float, event: str, tz: str) -> Optional[datetime]:
    """Local-time sunrise/sunset for the local calendar day ``d``."""
    zone = ZoneInfo(tz)
    # Search the UTC day whose event lands on local day d (handles the date line).
    for delta in (0, -1, 1):
        utc = sun_event_utc(d + timedelta(days=delta), lat, lon, event)
        if utc is None:
            return None
        local = utc.astimezone(zone)
        if local.date() == d:
            return local
    return None
