"""Regression tests for PrimeFaces historical date selection."""

from datetime import date
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


FORM_HTML = """
<form id="myForm1" name="myForm1">
  <input type="hidden" name="jakarta.faces.ViewState" value="fixture-state-1" />
  <input id="myForm1:calendarFromRegion_input"
         name="myForm1:calendarFromRegion" value="24.08.2026" />
  <input id="myForm1:calendarToRegion_input"
         name="myForm1:calendarToRegion" value="24.08.2026" />
  <input name="myForm1:period:selectedClass" value="ConsumQuarter" />
  <input name="myForm1:unit:selectedClass" value="KWH" />
  <button id="myForm1:btnIdA1">Anzeigen</button>
  <div id="myForm1:list"></div>
</form>
<script>
PrimeFaces.cw("DatePicker","fromWidget",{
  id:"myForm1:calendarFromRegion",
  behaviors:{dateSelect:function(ext,event){
    PrimeFaces.ab({s:"myForm1:calendarFromRegion",e:"dateSelect",
      f:"myForm1",p:"myForm1:calendarFromRegion",u:"myForm1:list"},ext);
  }}
});
PrimeFaces.cw("DatePicker","toWidget",{
  id:"myForm1:calendarToRegion",
  behaviors:{dateSelect:function(ext,event){
    PrimeFaces.ab({s:"myForm1:calendarToRegion",e:"dateSelect",
      f:"myForm1",p:"myForm1:calendarToRegion",u:"myForm1:list"},ext);
  }}
});
</script>
"""


def _form_and_controls():
    soup = BeautifulSoup(FORM_HTML, "html.parser")
    form = soup.find("form")
    assert form is not None
    date_from = LinzNetzClient._find_named_control(form, _api._FROM_RE)
    date_to = LinzNetzClient._find_named_control(form, _api._TO_RE)
    assert date_from is not None and date_to is not None
    return soup, form, date_from, date_to


def test_datepicker_behavior_is_discovered_from_primefaces_script() -> None:
    soup, _form, date_from, _date_to = _form_and_controls()
    behavior = LinzNetzClient._find_date_behavior(soup, date_from, "myForm1")

    assert behavior is not None
    assert behavior.source == "myForm1:calendarFromRegion"
    assert behavior.execute == "myForm1:calendarFromRegion"
    assert behavior.render == "myForm1:list"
    assert behavior.event == "dateSelect"


def test_datepicker_selection_builds_browser_ajax_request() -> None:
    soup, form, date_from, date_to = _form_and_controls()
    behavior = LinzNetzClient._find_date_behavior(soup, date_from, "myForm1")
    assert behavior is not None

    payload = LinzNetzClient._build_date_selection_payload(
        form,
        "myForm1",
        date_from,
        date_to,
        ChoiceField("myForm1:period:selectedClass", "ConsumQuarter"),
        ChoiceField("myForm1:unit:selectedClass", "KWH"),
        "21.08.2026",
        behavior,
        "jakarta.faces.ViewState",
        "fixture-state-1",
    )

    assert payload["jakarta.faces.partial.ajax"] == "true"
    assert payload["jakarta.faces.source"] == "myForm1:calendarFromRegion"
    assert payload["jakarta.faces.partial.execute"] == "myForm1:calendarFromRegion"
    assert payload["jakarta.faces.partial.render"] == "myForm1:list"
    assert payload["jakarta.faces.behavior.event"] == "dateSelect"
    assert payload["jakarta.faces.partial.event"] == "dateSelect"
    assert payload["myForm1:calendarFromRegion"] == "21.08.2026"
    assert payload["myForm1:calendarToRegion"] == "21.08.2026"
    assert payload["jakarta.faces.ViewState"] == "fixture-state-1"


def test_new_viewstate_is_extracted_from_partial_response() -> None:
    body = """
    <partial-response><changes>
      <update id="jakarta.faces.ViewState"><![CDATA[fixture-state-2]]></update>
    </changes></partial-response>
    """
    assert LinzNetzClient._extract_partial_view_state(body) == "fixture-state-2"


def test_updated_form_must_not_confirm_a_different_day() -> None:
    form = BeautifulSoup(
        '<form><input name="myForm1:calendarFromRegion" value="24.08.2026" /></form>',
        "html.parser",
    ).find("form")
    assert form is not None

    with pytest.raises(LinzNetzParseError, match="requested=2026-08-21"):
        LinzNetzClient._verify_rendered_day_if_present(
            form, _api._FROM_RE, date(2026, 8, 21)
        )


def test_pagination_keeps_latest_viewstate_and_date_payload() -> None:
    base = {
        "myForm1": "myForm1",
        "myForm1:calendarFromRegion": "21.08.2026",
        "myForm1:calendarToRegion": "21.08.2026",
        "jakarta.faces.ViewState": "fixture-state-1",
        "jakarta.faces.source": "myForm1:btnIdA1",
        "jakarta.faces.partial.execute": "@form",
        "jakarta.faces.partial.render": "myForm1:list",
        "jakarta.faces.behavior.event": "action",
        "jakarta.faces.partial.event": "click",
    }
    payload = LinzNetzClient._build_pagination_payload(
        base,
        PaginationInfo("myForm1:consumptionsTable", 24, 96),
        48,
        "jakarta.faces.ViewState",
        "fixture-state-2",
    )

    assert payload["jakarta.faces.ViewState"] == "fixture-state-2"
    assert payload["myForm1:calendarFromRegion"] == "21.08.2026"
    assert payload["myForm1:calendarToRegion"] == "21.08.2026"
    assert payload["myForm1:consumptionsTable_first"] == "48"


def test_96_quarter_values_for_requested_day_are_accepted() -> None:
    requested = date(2026, 8, 21)
    rows = []
    for index in range(96):
        hour, minute_index = divmod(index, 4)
        minute = minute_index * 15
        rows.append(
            f"<tr><td>21.08.2026 {hour:02d}:{minute:02d}</td><td>0,100</td><td></td></tr>"
        )
    readings = LinzNetzClient._parse_readings("<table>" + "".join(rows) + "</table>")

    assert len(readings) == 96
    assert LinzNetzClient._validate_requested_day(requested, readings) == readings
