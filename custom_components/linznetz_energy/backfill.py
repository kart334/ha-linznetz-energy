"""Helpers for one-shot manual backfill state handling."""

from __future__ import annotations

from collections.abc import Mapping

from .const import (
    BACKFILL_STATUS_FAILED,
    BACKFILL_STATUS_PARTIAL,
    BACKFILL_STATUS_SUCCESS,
    CONF_LAST_BACKFILL_FAILED_DAYS,
    CONF_LAST_BACKFILL_IMPORTED_DAYS,
    CONF_LAST_BACKFILL_REQUESTED_DAYS,
    CONF_LAST_BACKFILL_STATUS,
    CONF_RUN_BACKFILL,
)


def classify_backfill_result(
    requested_days: int,
    imported_days: int,
    failed_days: int,
) -> str:
    """Classify one completed manual backfill attempt."""
    if requested_days > 0 and imported_days == requested_days and failed_days == 0:
        return BACKFILL_STATUS_SUCCESS
    if imported_days == 0 and failed_days >= requested_days:
        return BACKFILL_STATUS_FAILED
    return BACKFILL_STATUS_PARTIAL


def finalized_backfill_options(
    options: Mapping[str, object],
    *,
    requested_days: int,
    imported_days: int,
    failed_days: int,
) -> dict[str, object]:
    """Consume the one-shot trigger and persist the result of that attempt."""
    result = dict(options)
    result[CONF_RUN_BACKFILL] = False
    result[CONF_LAST_BACKFILL_STATUS] = classify_backfill_result(
        requested_days,
        imported_days,
        failed_days,
    )
    result[CONF_LAST_BACKFILL_REQUESTED_DAYS] = requested_days
    result[CONF_LAST_BACKFILL_IMPORTED_DAYS] = imported_days
    result[CONF_LAST_BACKFILL_FAILED_DAYS] = failed_days
    return result


def manual_backfill_requested(options: Mapping[str, object]) -> bool:
    """Return whether a new one-shot backfill was explicitly requested."""
    return bool(options.get(CONF_RUN_BACKFILL, False))
