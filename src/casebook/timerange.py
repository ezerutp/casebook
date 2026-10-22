"""Rangos de tiempo: ISO 8601 (UTC si no trae zona) o relativos a ahora ("now", "-15m", "-2h", "-3d")."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .guards import GuardError

_RELATIVE = re.compile(r"^-(\d+)\s*([smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse(value: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    text = value.strip().lower()
    if text == "now":
        return now
    m = _RELATIVE.match(text)
    if m:
        return now - timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardError(f"fecha inválida {value!r}: usa ISO 8601 (2026-10-14T10:00:00Z) o relativo (-2h, -3d, now)") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def window(start: str, end: str, max_days: float, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(UTC)
    s, e = parse(start, now), parse(end, now)
    if s >= e:
        raise GuardError("start tiene que ser anterior a end")
    if e - s > timedelta(days=max_days):
        raise GuardError(f"la ventana supera {max_days:g} días; acótala")
    return s, e


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
