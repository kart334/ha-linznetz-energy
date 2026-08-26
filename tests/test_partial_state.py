"""Regression tests for JSF Partial Response state handling."""

from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
import types

from bs4 import BeautifulSoup
import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "linznetz_energy"


def _load_api_module():
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
ChoiceField = _api.ChoiceField
PaginationInfo = _api.PaginationInfo
QuarterReading = _api.QuarterReading


BASE_FORM = """
<form id="myForm1" name="myForm1">
  <input type="hidden" name="myForm1:token" value="old-hidden" />
  <input id="myForm1:calendarFromRegion_input"
         name="myForm1:calendarFromRegion" value="24.08.2026" />
  <input id="myForm1:calendarToRegion_input"
         name="myForm1:calendarToRegion" value="24.08.2026" />
  <input name="myForm1:period:selectedClass" value="ConsumQuarter" />
  <input name="myForm1:unit:selectedClass" value="KWH" />
  <button id="myForm1:btnIdA1">Anzeigen</button>
  <div id="myForm1:list"><span id="old-list">old</span></div>
</form>
"""


def _form():
    form = BeautifulSoup(BASE_FORM, "html.parser").find("form")
    assert form is not None
    return form


def test_partial_response_parses_multiple_updates() -> None:
    body = """
    <partial-response><changes>
      <update id="myForm1:list"><![CDATA[
        <div id="myForm1:list"><span>new</span></div>
      ]]></update>
      <update id="myForm1:hiddenRegion"><![CDATA[
        <div id="myForm1:hiddenRegion"><input name="myForm1:newHidden" value="x" /></div>
      ]]></update>
      <update id="jakarta.faces.ViewState"><![CDATA[fixture-state-2]]></update>
    </changes></partial-response>
    """

    updates = LinzNetzClient._parse_partial_updates(body)

    assert set(updates) == {
        "myForm1:list",
        "myForm1:hiddenRegion",
        "jakarta.faces.ViewState",
    }
    assert LinzNetzClient._extract_partial_view_state(body) == "fixture-state-2"


def test_viewstate_and_relevant_form_region_advance_together() -> None:
    body = """
    <partial-response><changes>
      <update id="myForm1:list"><![CDATA[
        <div id="myForm1:list">
          <input type="hidden" name="myForm1:serverMarker" value="new-state" />
        </div>
      ]]></update>
      <update id="jakarta.faces.ViewState"><![CDATA[fixture-state-2]]></update>
    </changes></partial-response>
    """

    merged = LinzNetzClient._merge_partial_response_form(_form(), "myForm1", body)
    payload = LinzNetzClient._collect_form_payload(merged)

    assert payload["myForm1:serverMarker"] == "new-state"
    assert LinzNetzClient._extract_partial_view_state(body) == "fixture-state-2"


def test_display_post_uses_merged_form_state_and_processes_form() -> None:
    body = """
    <partial-response><changes>
      <update id="myForm1:list"><![CDATA[
        <div id="myForm1:list">
          <input type="hidden" name="myForm1:serverMarker" value="after-date-ajax" />
        </div>
      ]]></update>
    </changes></partial-response>
    """
    merged = LinzNetzClient._merge_partial_response_form(_form(), "myForm1", body)
    date_from = LinzNetzClient._find_named_control(merged, _api._FROM_RE)
    date_to = LinzNetzClient._find_named_control(merged, _api._TO_RE)
    assert date_from is not None and date_to is not None

    payload = LinzNetzClient._build_display_payload(
        merged,
        "myForm1",
        "myForm1:btnIdA1",
        date_from,
        date_to,
        ChoiceField("myForm1:period:selectedClass", "ConsumQuarter"),
        ChoiceField("myForm1:unit:selectedClass", "KWH"),
        "21.08.2026",
        "jakarta.faces.ViewState",
        "fixture-state-2",
    )

    assert payload["jakarta.faces.partial.execute"] == "@form"
    assert payload["myForm1:serverMarker"] == "after-date-ajax"
    assert payload["myForm1:calendarFromRegion"] == "21.08.2026"
    assert payload["myForm1:calendarToRegion"] == "21.08.2026"
    assert payload["jakarta.faces.ViewState"] == "fixture-state-2"


def test_last_available_day_96_quarters_remains_valid() -> None:
    requested = date(2026, 8, 24)
    readings = [
        QuarterReading(
            datetime(2026, 8, 24, index // 4, (index % 4) * 15),
            0.1,
        )
        for index in range(96)
    ]

    assert len(LinzNetzClient._validate_requested_day(requested, readings)) == 96


def test_historical_day_with_correct_selection_is_valid() -> None:
    requested = date(2026, 8, 21)
    readings = [QuarterReading(datetime(2026, 8, 21, 12, 0), 0.2)]

    assert LinzNetzClient._validate_requested_day(requested, readings) == readings


def test_empty_datatable_is_not_success() -> None:
    assert LinzNetzClient._parse_readings(
        '<div id="myForm1:consumptionsTable"><table><tbody></tbody></table></div>'
    ) == []


def test_wrong_returned_day_still_fails_closed() -> None:
    with pytest.raises(LinzNetzParseError, match="requested=2026-08-21"):
        LinzNetzClient._validate_requested_day(
            date(2026, 8, 21),
            [QuarterReading(datetime(2026, 8, 24, 0, 0), 0.1)],
        )


def test_pagination_keeps_updated_state_and_dates() -> None:
    base = {
        "myForm1": "myForm1",
        "myForm1:calendarFromRegion": "21.08.2026",
        "myForm1:calendarToRegion": "21.08.2026",
        "myForm1:serverMarker": "after-date-ajax",
        "jakarta.faces.ViewState": "fixture-state-2",
        "jakarta.faces.source": "myForm1:btnIdA1",
        "jakarta.faces.partial.execute": "@form",
        "jakarta.faces.partial.render": "myForm1:list",
        "jakarta.faces.behavior.event": "action",
        "jakarta.faces.partial.event": "click",
    }

    payload = LinzNetzClient._build_pagination_payload(
        base,
        PaginationInfo("myForm1:consumptionsTable", 24, 96),
        24,
        "jakarta.faces.ViewState",
        "fixture-state-3",
    )

    assert payload["jakarta.faces.ViewState"] == "fixture-state-3"
    assert payload["myForm1:serverMarker"] == "after-date-ajax"
    assert payload["myForm1:calendarFromRegion"] == "21.08.2026"
    assert payload["myForm1:calendarToRegion"] == "21.08.2026"
    assert payload["myForm1:consumptionsTable_first"] == "24"
