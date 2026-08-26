"""Regression tests for the 0.1.13 date-contract diagnostic build."""

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

    if name == "date_contract_diagnostics":
        diag_spec = importlib.util.spec_from_file_location(
            f"{PACKAGE_NAME}.diagnostics", PACKAGE_PATH / "diagnostics.py"
        )
        assert diag_spec is not None and diag_spec.loader is not None
        diag_module = importlib.util.module_from_spec(diag_spec)
        sys.modules[diag_spec.name] = diag_module
        diag_spec.loader.exec_module(diag_module)

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
api = _load("api")


def test_confirmed_live_display_contract_executes_button_not_form() -> None:
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


def test_live_date_fields_are_direct_text_inputs_with_ddmmyyyy_format() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion"
             type="text" value="24.08.2026" />
      <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion"
             type="text" value="24.08.2026" />
    </form>
    """

    from_summary = date_diag.calendar_contract_summary(markup, "calendarFromRegion")
    to_summary = date_diag.calendar_contract_summary(markup, "calendarToRegion")

    assert from_summary["controls"] == [
        {
            "id": "myForm1:calendarFromRegion",
            "name": "myForm1:calendarFromRegion",
            "type": "text",
            "format_ddMMyyyy": True,
            "handlers": {},
        }
    ]
    assert to_summary["controls"][0]["type"] == "text"
    assert to_summary["controls"][0]["format_ddMMyyyy"] is True


def test_date_state_without_event_contract_is_reported_as_not_server_settable() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion"
             type="text" value="24.08.2026" />
    </form>
    """

    summary = date_diag.calendar_contract_summary(markup, "calendarFromRegion")

    assert summary["server_contract_detected"] is False
    assert summary["ajax_contracts"] == []
    assert summary["widget_configs"] == []


def test_datepicker_contract_inside_primefaces_widget_behavior_is_detected() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion"
             type="text" value="24.08.2026" />
    </form>
    <script>
      PrimeFaces.cw('Calendar','widget_from',{id:'myForm1:calendarFromRegion',behaviors:{
        dateSelect:function(ext,event){PrimeFaces.ab({s:'myForm1:calendarFromRegion',e:'dateSelect',p:'myForm1:calendarFromRegion',u:'myForm1:list'});}
      }});
    </script>
    """

    summary = date_diag.calendar_contract_summary(markup, "calendarFromRegion")

    assert summary["script_refs"] == 1
    assert summary["server_contract_detected"] is True
    assert summary["ajax_contracts"] == [
        {
            "source": "myForm1:calendarFromRegion",
            "execute": "myForm1:calendarFromRegion",
            "render": "myForm1:list",
            "event": "dateSelect",
        }
    ]
    assert summary["widget_configs"][0]["widget_type"] == "Calendar"
    assert summary["widget_configs"][0]["tokens"]["dateSelect"] is True


def test_datepicker_child_source_is_not_missed_by_exact_id_matching() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion"
             type="text" value="24.08.2026" />
    </form>
    <script>
      var x='myForm1:calendarFromRegion';
      PrimeFaces.ab({s:'myForm1:calendarFromRegion:trigger',e:'change',p:'myForm1:calendarFromRegion',u:'myForm1:list'});
    </script>
    """

    summary = date_diag.calendar_contract_summary(markup, "calendarFromRegion")

    assert summary["ajax_contracts"][0]["source"] == "myForm1:calendarFromRegion:trigger"
    assert summary["ajax_contracts"][0]["event"] == "change"


def test_inline_calendar_handlers_are_reported_without_handler_body() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion"
             type="text" value="24.08.2026"
             onchange="PrimeFaces.ab({s:'myForm1:calendarToRegion',e:'change'});someSensitiveLocalValue='x';" />
    </form>
    """

    summary = date_diag.calendar_contract_summary(markup, "calendarToRegion")
    handlers = summary["controls"][0]["handlers"]

    assert handlers["onchange"] == {
        "present": True,
        "primefaces_ajax": True,
        "jsf_ajax": False,
        "submit": False,
    }
    assert "someSensitiveLocalValue" not in repr(summary)


def test_kwh_without_script_contract_remains_none_detected() -> None:
    markup = """
    <form id="myForm1">
      <input name="myForm1:j_idt1460:j_idt1464:selectedClass" value="KWH" />
    </form>
    """

    summary = date_diag.choice_contract_summary(markup, "KWH")

    assert summary["server_contract_detected"] is False
    assert summary["ajax_contracts"] == []


def test_kwh_generated_child_ajax_contract_can_be_discovered() -> None:
    markup = """
    <form id="myForm1">
      <input name="myForm1:j_idt1460:j_idt1464:selectedClass" value="KWH" />
    </form>
    <script>
      var component='myForm1:j_idt1460:j_idt1464';
      PrimeFaces.ab({s:'myForm1:j_idt1460:j_idt1464:grid_eval',e:'valueChange',p:'myForm1:j_idt1460:j_idt1464:grid_eval',u:'myForm1'});
    </script>
    """

    summary = date_diag.choice_contract_summary(markup, "KWH")

    assert summary["server_contract_detected"] is True
    assert summary["ajax_contracts"][0]["source"].endswith(":grid_eval")


def test_returned_day_fail_closed_remains_unchanged() -> None:
    requested = date(2026, 8, 21)
    wrong = api.QuarterReading(datetime(2026, 8, 24, 0, 0), 0.1)

    with pytest.raises(api.LinzNetzParseError, match="requested=2026-08-21"):
        api.LinzNetzClient._validate_requested_day(requested, [wrong])
