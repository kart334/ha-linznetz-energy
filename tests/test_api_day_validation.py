"""Regression tests for requested-day validation."""

from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "linznetz_energy"


def _load_api_module():
    """Load const/api without executing the Home Assistant package __init__."""
    package_name = "custom_components.linznetz_energy"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules[package_name] = package

    const_spec = importlib.util.spec_from_file_location(
        f"{package_name}.const", PACKAGE_PATH / "const.py"
    )
    assert const_spec is not None and const_spec.loader is not None
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const_module
    const_spec.loader.exec_module(const_module)

    api_spec = importlib.util.spec_from_file_location(
        f"{package_name}.api", PACKAGE_PATH / "api.py"
    )
    assert api_spec is not None and api_spec.loader is not None
    api_module = importlib.util.module_from_spec(api_spec)
    sys.modules[api_spec.name] = api_module
    api_spec.loader.exec_module(api_module)
    return api_module


_api = _load_api_module()
LinzNetzClient = _api.LinzNetzClient
LinzNetzParseError = _api.LinzNetzParseError
QuarterReading = _api.QuarterReading


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

    with pytest.raises(
        LinzNetzParseError,
        match="returned=2026-07-15, 2026-07-16",
    ):
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
