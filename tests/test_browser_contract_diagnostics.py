"""Regression tests for privacy-safe portal browser-contract diagnostics."""

from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
import types

from bs4 import BeautifulSoup
import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "linznetz_energy"
PACKAGE_NAME = "custom_components.linznetz_energy"


def _load_module(name: str):
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules[PACKAGE_NAME] = package

    if name == "api":
        const_spec = importlib.util.spec_from_file_location(
            f"{PACKAGE_NAME}.const", PACKAGE_PATH / "const.py"
        )
        assert const_spec is not None and const_spec.loader is not None
        const_module = importlib.util.module_from_spec(const_spec)
        sys.modules[const_spec.name] = const_module
        const_spec.loader.exec_module(const_module)

    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.{name}", PACKAGE_PATH / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diag = _load_module("diagnostics")
api = _load_module("api")


def test_full_form_display_button_is_detected() -> None:
    soup = BeautifulSoup(
        '<form id="f"><button id="f:show" type="submit">Anzeigen</button></form>',
        "html.parser",
    )
    button = soup.find("button")
    assert button is not None

    contract = diag.classify_button_contract(button, "f")

    assert contract.request_type == "full_post"


def test_ajax_display_button_contract_is_detected() -> None:
    soup = BeautifulSoup(
        """
        <form id="f">
          <button id="f:show" type="button" onclick="PrimeFaces.ab({s:'f:show',p:'@form',u:'f:list',e:'action'});return false;">Anzeigen</button>
        </form>
        """,
        "html.parser",
    )
    button = soup.find("button")
    assert button is not None

    contract = diag.classify_button_contract(button, "f")

    assert contract.request_type == "partial_ajax"
    assert contract.source == "f:show"
    assert contract.execute == "@form"
    assert contract.render == "f:list"
    assert contract.event == "action"


def test_datepicker_visible_and_hidden_inputs_are_reported_without_raw_hidden_value() -> None:
    form = BeautifulSoup(
        """
        <form id="f">
          <input id="f:calendarFromRegion_input" name="f:calendarFromRegion_input" value="24.08.2026" />
          <input id="f:calendarFromRegion_hinput" name="f:calendarFromRegion_hinput" value="1787522400000" />
        </form>
        """,
        "html.parser",
    ).find("form")
    assert form is not None

    controls = diag.date_control_candidates(form, "calendarFromRegion")

    assert [item["id"] for item in controls] == [
        "f:calendarFromRegion_input",
        "f:calendarFromRegion_hinput",
    ]
    assert controls[0]["value"] == "24.08.2026"
    assert controls[1]["value"] == "<non-date>"


def test_kwh_and_datepicker_contracts_are_detected_independent_of_markup_order() -> None:
    for scripts in (
        """
        <script>PrimeFaces.ab({s:'f:unit',e:'change',p:'f:unit',u:'f:list'});</script>
        <script>PrimeFaces.ab({s:'f:calendarFromRegion',e:'dateSelect',p:'f:calendarFromRegion',u:'f:list'});</script>
        """,
        """
        <script>PrimeFaces.ab({s:'f:calendarFromRegion',e:'dateSelect',p:'f:calendarFromRegion',u:'f:list'});</script>
        <script>PrimeFaces.ab({s:'f:unit',e:'change',p:'f:unit',u:'f:list'});</script>
        """,
    ):
        soup = BeautifulSoup(scripts, "html.parser")
        kwh = diag.find_component_ajax_contract(soup, {"f:unit"})
        date_contract = diag.find_component_ajax_contract(soup, {"f:calendarFromRegion"})
        assert kwh is not None and kwh.event == "change"
        assert date_contract is not None and date_contract.event == "dateSelect"


def test_response_with_datatable_but_no_value_rows_is_distinguished_from_no_table() -> None:
    empty_table = """
    <partial-response><changes>
      <update id="f:list"><![CDATA[
        <div id="f:list"><table id="f:consumptionsTable"><tr><th>Zeit</th><th>kWh</th></tr></table></div>
      ]]></update>
      <update id="jakarta.faces.ViewState"><![CDATA[fixture-state]]></update>
    </changes></partial-response>
    """
    no_table = "<partial-response><changes><update id='f:list'><![CDATA[<div id='f:list'></div>]]></update></changes></partial-response>"

    empty = diag.response_structure(empty_table)
    missing = diag.response_structure(no_table)

    assert empty["table_found"] is True
    assert empty["candidate_value_rows"] == 0
    assert api.LinzNetzClient._parse_readings(empty_table) == []
    assert missing["table_found"] is False


def test_response_structure_reports_updates_without_exposing_viewstate_value() -> None:
    body = """
    <partial-response><changes>
      <update id="f:list"><![CDATA[<div id="f:list"></div>]]></update>
      <update id="jakarta.faces.ViewState"><![CDATA[secret-looking-fixture-state]]></update>
    </changes></partial-response>
    """

    structure = diag.response_structure(body)

    assert structure["update_count"] == 2
    assert structure["update_ids"] == ["f:list", "<ViewState>"]
    assert "secret-looking-fixture-state" not in repr(structure)


def test_fail_closed_returned_day_is_unchanged() -> None:
    requested = date(2026, 8, 21)
    wrong = api.QuarterReading(datetime(2026, 8, 24, 0, 0), 0.1)

    with pytest.raises(api.LinzNetzParseError, match="requested=2026-08-21"):
        api.LinzNetzClient._validate_requested_day(requested, [wrong])
