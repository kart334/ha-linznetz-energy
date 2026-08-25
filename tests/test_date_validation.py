"""Regression tests for requested-day validation."""

from __future__ import annotations

from datetime import date, datetime
import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "linznetz_energy"
    / "validation.py"
)
spec = importlib.util.spec_from_file_location("linznetz_energy_validation", MODULE_PATH)
assert spec is not None and spec.loader is not None
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)

RequestedDayMismatch = validation.RequestedDayMismatch
validate_requested_day = validation.validate_requested_day


def _ts(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def test_requested_day_is_accepted() -> None:
    requested = date(2026, 7, 15)
    validate_requested_day(requested, [_ts(requested), _ts(requested, 23, 45)])


def test_wrong_returned_day_fails_closed() -> None:
    requested = date(2026, 7, 15)
    returned = date(2026, 8, 24)

    with pytest.raises(RequestedDayMismatch) as err:
        validate_requested_day(requested, [_ts(returned)])

    assert err.value.requested_day == requested
    assert err.value.returned_days == (returned,)


def test_mixed_returned_days_fail_closed() -> None:
    requested = date(2026, 7, 15)
    other = date(2026, 7, 16)

    with pytest.raises(RequestedDayMismatch) as err:
        validate_requested_day(requested, [_ts(requested), _ts(other)])

    assert err.value.returned_days == (requested, other)


def test_paginated_combined_values_are_validated_as_one_result() -> None:
    requested = date(2026, 7, 15)
    page_1 = [_ts(requested, 0, 0), _ts(requested, 0, 15)]
    page_2 = [_ts(requested, 6, 0), _ts(date(2026, 8, 24), 6, 15)]

    with pytest.raises(RequestedDayMismatch):
        validate_requested_day(requested, [*page_1, *page_2])


def test_two_historical_requests_cannot_accept_same_returned_day() -> None:
    returned = date(2026, 8, 24)
    first_request = date(2026, 7, 15)
    second_request = date(2025, 8, 15)

    for requested in (first_request, second_request):
        with pytest.raises(RequestedDayMismatch):
            validate_requested_day(requested, [_ts(returned)])
