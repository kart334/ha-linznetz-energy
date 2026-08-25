"""Regression tests for requested-day validation."""

from datetime import date, datetime

import pytest

from custom_components.linznetz_energy.api import (
    LinzNetzClient,
    LinzNetzParseError,
    QuarterReading,
)


def _reading(day: date, hour: int = 0) -> QuarterReading:
    return QuarterReading(
        start_local=datetime(day.year, day.month, day.day, hour, 0),
        kwh=0.125,
    )


def test_requested_day_is_accepted() -> None:
    requested = date(2026, 7, 15)
    readings = [_reading(requested, 0), _reading(requested, 1)]

    assert LinzNetzClient._validate_requested_day(requested, readings) == readings


def test_different_returned_day_is_rejected() -> None:
    requested = date(2026, 7, 15)
    returned = date(2026, 8, 24)

    with pytest.raises(LinzNetzParseError, match="requested=2026-07-15"):
        LinzNetzClient._validate_requested_day(requested, [_reading(returned)])


def test_mixed_returned_days_are_rejected() -> None:
    requested = date(2026, 7, 15)
    readings = [_reading(requested), _reading(date(2026, 7, 16), 1)]

    with pytest.raises(LinzNetzParseError, match="returned=2026-07-15, 2026-07-16"):
        LinzNetzClient._validate_requested_day(requested, readings)


def test_paginated_combined_result_with_wrong_day_is_rejected() -> None:
    requested = date(2026, 7, 15)
    first_page = [_reading(requested, hour) for hour in range(6)]
    later_page = [_reading(date(2026, 8, 24), hour) for hour in range(6, 12)]

    with pytest.raises(LinzNetzParseError):
        LinzNetzClient._validate_requested_day(
            requested, first_page + later_page
        )


def test_two_historical_requests_cannot_accept_same_returned_day() -> None:
    first_requested = date(2026, 7, 15)
    second_requested = date(2026, 7, 16)
    returned = [_reading(first_requested)]

    assert LinzNetzClient._validate_requested_day(first_requested, returned) == returned
    with pytest.raises(LinzNetzParseError, match="requested=2026-07-16"):
        LinzNetzClient._validate_requested_day(second_requested, returned)
