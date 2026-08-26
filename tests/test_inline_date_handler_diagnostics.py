"""Regression tests for 0.1.14 inline date-handler diagnostics."""

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


def _load(name: str):
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
        sys.modules[PACKAGE_NAME] = package
    if name == "api" and f"{PACKAGE_NAME}.const" not in sys.modules:
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


diag = _load("diagnostics")
date_diag = _load("date_contract_diagnostics")
inline_diag = _load("inline_date_handler_diagnostics")
api = _load("api")


def test_from_onchange_primefaces_contract_is_extracted_without_values() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion"
             value="24.08.2026"
             onchange="document.getElementById('myForm1:calendarToRegion').value=this.value;PrimeFaces.ab({s:'myForm1:calendarFromRegion',p:'myForm1:calendarFromRegion myForm1:calendarToRegion',u:'myForm1:list',e:'change',params:[{name:'myForm1:calendarFromRegion',value:'SECRET-DATE'},{name:'myForm1:calendarToRegion',value:'SECRET-DATE'}]});" />
      <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion" value="24.08.2026" />
    </form>
    """
    controls = inline_diag.calendar_inline_contracts(markup)
    contract = controls[0]

    assert contract["primefaces_ajax"] is True
    assert contract["source"] == "myForm1:calendarFromRegion"
    assert contract["execute"] == "myForm1:calendarFromRegion myForm1:calendarToRegion"
    assert contract["render"] == "myForm1:list"
    assert contract["event"] == "change"
    assert contract["execute_flags"]["contains_from"] is True
    assert contract["execute_flags"]["contains_to"] is True
    assert contract["execute_flags"]["uses_form"] is False
    assert "calendarFromRegion" in contract["related_controls"]
    assert "calendarToRegion" in contract["related_controls"]
    assert "myForm1:calendarFromRegion" in contract["param_names"]
    assert "myForm1:calendarToRegion" in contract["param_names"]
    assert "myForm1:calendarToRegion" in contract["pre_ajax_assignments"]
    assert "SECRET-DATE" not in repr(contract)


def test_to_onchange_without_primefaces_is_distinguished_and_function_names_only() -> None:
    soup = BeautifulSoup(
        '<input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion" value="24.08.2026" onchange="syncDateRange(this);validateRange();" />',
        "html.parser",
    )
    control = soup.find("input")
    assert control is not None

    contract = inline_diag.inline_handler_contract(control)

    assert contract["present"] is True
    assert contract["primefaces_ajax"] is False
    assert contract["source"] is None
    assert contract["function_calls"] == ["syncDateRange", "validateRange"]
    assert contract["param_names"] == []
    assert contract["pre_ajax_assignments"] == []


def test_direct_date_inputs_and_ddmmyyyy_are_preserved() -> None:
    markup = """
    <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion" type="text" value="24.08.2026" />
    <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion" type="text" value="24.08.2026" />
    """
    from_summary = date_diag.calendar_contract_summary(markup, "calendarFromRegion")
    to_summary = date_diag.calendar_contract_summary(markup, "calendarToRegion")

    assert from_summary["controls"][0]["type"] == "text"
    assert from_summary["controls"][0]["format_ddMMyyyy"] is True
    assert to_summary["controls"][0]["type"] == "text"
    assert to_summary["controls"][0]["format_ddMMyyyy"] is True


def test_confirmed_display_contract_is_button_only_not_form() -> None:
    soup = BeautifulSoup(
        """
        <form id="myForm1">
          <input id="myForm1:btnIdA1" type="button" value="Anzeigen"
                 onclick="PrimeFaces.ab({s:'myForm1:btnIdA1',p:'myForm1:btnIdA1',u:'myForm1:list',e:'action'});return false;" />
        </form>
        """,
        "html.parser",
    )
    button = soup.find("input")
    assert button is not None
    contract = diag.classify_button_contract(button, "myForm1")

    assert contract.request_type == "partial_ajax"
    assert contract.source == "myForm1:btnIdA1"
    assert contract.execute == "myForm1:btnIdA1"
    assert contract.execute != "@form"
    assert contract.render == "myForm1:list"
    assert contract.event == "action"


def test_returned_day_fail_closed_remains_unchanged() -> None:
    requested = date(2026, 8, 21)
    wrong = api.QuarterReading(datetime(2026, 8, 24, 0, 0), 0.1)

    with pytest.raises(api.LinzNetzParseError, match="requested=2026-08-21"):
        api.LinzNetzClient._validate_requested_day(requested, [wrong])


def test_pagination_payload_contract_remains_unchanged() -> None:
    pagination = api.PaginationInfo("myForm1:consumptionsTable", 24, 96)
    payload = api.LinzNetzClient._build_pagination_payload(
        {"myForm1": "myForm1", "jakarta.faces.partial.ajax": "true"},
        pagination,
        24,
        "jakarta.faces.ViewState",
        "fixture-state",
    )

    assert payload["jakarta.faces.source"] == "myForm1:consumptionsTable"
    assert payload["jakarta.faces.partial.execute"] == "myForm1:consumptionsTable"
    assert payload["jakarta.faces.partial.render"] == "myForm1:consumptionsTable"
    assert payload["myForm1:consumptionsTable_pagination"] == "true"


def test_sensitive_handler_values_are_never_returned() -> None:
    soup = BeautifulSoup(
        """
        <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion"
          onchange="PrimeFaces.ab({s:'myForm1:calendarFromRegion',p:'@this',u:'myForm1:list',e:'change',params:[{name:'safeParam',value:'customer-123-secret'}]});" />
        """,
        "html.parser",
    )
    control = soup.find("input")
    assert control is not None
    contract = inline_diag.inline_handler_contract(control)

    assert contract["execute"] == "@this"
    assert contract["execute_flags"]["uses_this"] is True
    assert contract["param_names"] == ["safeParam"]
    assert "customer-123-secret" not in repr(contract)
