"""Regression tests for 0.1.16 assignToDate structural diagnostics."""

import importlib.util
from pathlib import Path
import sys
import types

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
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.{name}", PACKAGE_PATH / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("diagnostics")
_load("date_contract_diagnostics")
_load("inline_date_handler_diagnostics")
diag = _load("assign_to_date_diagnostics")


def test_assign_to_date_local_copy_is_structurally_detected() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion" type="text" />
      <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion" type="text"
             onchange="assignToDate();" />
      <script>
        function assignToDate() {
          document.getElementById('myForm1:calendarToRegion').value =
            document.getElementById('myForm1:calendarFromRegion').value;
          normalizeDate();
        }
      </script>
    </form>
    """
    contract = diag.assign_to_date_contract(markup)
    assert contract["found"] is True
    assert "myForm1:calendarFromRegion" in contract["reads"]
    assert "myForm1:calendarToRegion" in contract["writes"]
    assert contract["copies"] == [
        {"from": "myForm1:calendarFromRegion", "to": "myForm1:calendarToRegion"}
    ]
    assert contract["ajax"] is False
    assert contract["submit"] is False
    assert contract["called_by"] == ["myForm1:calendarToRegion"]
    assert "normalizeDate" in contract["calls"]


def test_assign_to_date_server_actions_are_detected_without_arguments() -> None:
    markup = """
    <script>
      function assignToDate() {
        PrimeFaces.ab({s:'x',p:'x'});
        document.getElementById('myForm1').submit();
      }
    </script>
    """
    contract = diag.assign_to_date_contract(markup)
    assert contract["ajax"] is True
    assert contract["submit"] is True
    assert "PrimeFaces.ab" not in contract["calls"]


def test_hidden_fields_are_named_but_values_never_exposed() -> None:
    markup = """
    <form id="myForm1">
      <input id="myForm1:rangeHelper" name="myForm1:rangeHelper" type="hidden" value="SUPER-SECRET-VALUE" />
      <script>
        function assignToDate() {
          document.getElementById('myForm1:rangeHelper').value = 'SUPER-SECRET-VALUE';
        }
      </script>
    </form>
    """
    contract = diag.assign_to_date_contract(markup)
    assert contract["hidden_fields"] == ["myForm1:rangeHelper"]
    assert "SUPER-SECRET-VALUE" not in repr(contract)


def test_sensitive_field_names_are_redacted() -> None:
    markup = """
    <script>
      function assignToDate() {
        document.getElementById('customerToken').value = document.getElementById('myForm1:calendarFromRegion').value;
      }
    </script>
    """
    contract = diag.assign_to_date_contract(markup)
    assert "customerToken" not in repr(contract)
    assert "<redacted-field>" in contract["writes"]


def test_function_body_with_nested_blocks_is_bounded_safely() -> None:
    markup = """
    <script>
      function assignToDate() {
        if (true) { document.getElementById('myForm1:calendarToRegion').value = 'x'; }
        var literal = "}";
      }
      function unrelated() { document.getElementById('other').value = 'y'; }
    </script>
    """
    contract = diag.assign_to_date_contract(markup)
    assert "myForm1:calendarToRegion" in contract["writes"]
    assert "other" not in repr(contract)
