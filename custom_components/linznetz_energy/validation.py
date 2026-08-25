"""Pure validation helpers for LINZ NETZ readings."""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable


class RequestedDayMismatch(ValueError):
    """Returned reading timestamps do not belong to the requested day."""

    def __init__(self, requested_day: date, returned_days: tuple[date, ...]) -> None:
        self.requested_day = requested_day
        self.returned_days = returned_days
        super().__init__(
            f"requested={requested_day.isoformat()} returned="
            f"{','.join(day.isoformat() for day in returned_days) or 'none'}"
        )


def validate_requested_day(
    requested_day: date, timestamps: Iterable[datetime]
) -> None:
    """Fail closed unless every timestamp belongs to ``requested_day``."""
    returned_days = tuple(sorted({timestamp.date() for timestamp in timestamps}))
    if returned_days != (requested_day,):
        raise RequestedDayMismatch(requested_day, returned_days)
