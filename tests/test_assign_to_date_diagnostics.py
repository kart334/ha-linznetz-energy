"""Regression tests for 0.1.17 assignToDate AJAX diagnostics."""

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


def test_direct_primefaces_contract() -> None:
    markup = """
    <input id="myForm1:calendarToRegion" onchange="assignToDate();" />
    <script>
      function assignToDate() {
        PrimeFaces.ab({s:'myForm1:calendarToRegion',p:'myForm1:calendarToRegion',u:'myForm1',e:'change',params:[{name:'rangeHelper',value:'SECRET'}]});
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is True
    assert c["ajax_type"] == "PrimeFaces.ab"
    assert c["ajax_direct"] is True
    assert c["source"] == "myForm1:calendarToRegion"
    assert c["execute"] == "myForm1:calendarToRegion"
    assert c["render"] == "myForm1"
    assert c["event"] == "change"
    assert c["param_names"] == ["rangeHelper"]
    assert "SECRET" not in repr(c)


def test_direct_jsf_ajax_request_contract() -> None:
    markup = """
    <script>
      function assignToDate() {
        jsf.ajax.request('myForm1:calendarToRegion', 'change', {execute:'@this',render:'myForm1'});
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is True
    assert c["ajax_type"] == "jsf.ajax.request"
    assert c["source"] == "myForm1:calendarToRegion"
    assert c["execute"] == "@this"
    assert c["render"] == "myForm1"
    assert c["event"] == "change"


def test_wrapper_function_is_followed() -> None:
    markup = """
    <script>
      function assignToDate() { updateToDate(); }
      function updateToDate() {
        PrimeFaces.ab({s:'to',p:'to',u:'myForm1',e:'change'});
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is True
    assert c["ajax_type"] == "PrimeFaces.ab"
    assert c["ajax_direct"] is True
    assert c["ajax_via_function"] == "updateToDate"
    assert "updateToDate" in c["calls"]


def test_nested_multiline_and_braces_in_strings() -> None:
    markup = """
    <script>
      function assignToDate() {
        if (true) {
          const text = "fake }) and ajax text";
          helper();
        }
      }
      function helper() {
        const x = "}";
        PrimeFaces.ab({
          s: 'to',
          p: 'to',
          u: 'myForm1',
          e: 'change'
        });
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is True
    assert c["source"] == "to"
    assert c["render"] == "myForm1"


def test_dynamic_contract_values_are_not_guessed() -> None:
    markup = """
    <script>
      function assignToDate() {
        PrimeFaces.ab({s:sourceVar,p:getExecute(),u:targetVar,e:eventName});
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["source"] is None and c["source_dynamic"] is True
    assert c["execute"] is None and c["execute_dynamic"] is True
    assert c["render"] is None and c["render_dynamic"] is True
    assert c["event"] is None and c["event_dynamic"] is True
    assert "sourceVar" in c["source_refs"]
    assert "targetVar" in c["render_refs"]


def test_ajax_word_without_real_call_is_false_positive_free() -> None:
    markup = """
    <script>
      function assignToDate() {
        const ajax = 'PrimeFaces.ab is documentation only';
        const note = 'jsf.ajax.request';
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is False
    assert c["ajax_type"] is None


def test_local_copy_and_hidden_field_are_structural_only() -> None:
    markup = """
    <form>
      <input id="myForm1:calendarFromRegion" />
      <input id="myForm1:calendarToRegion" onchange="assignToDate();" />
      <input id="myForm1:rangeHelper" type="hidden" value="SUPER-SECRET-VALUE" />
      <script>
        function assignToDate() {
          document.getElementById('myForm1:calendarToRegion').value = document.getElementById('myForm1:calendarFromRegion').value;
          document.getElementById('myForm1:rangeHelper').value = 'SUPER-SECRET-VALUE';
        }
      </script>
    </form>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is False
    assert c["copies"] == [{"from":"myForm1:calendarFromRegion","to":"myForm1:calendarToRegion"}]
    assert c["hidden_fields"] == ["myForm1:rangeHelper"]
    assert c["called_by"] == ["myForm1:calendarToRegion"]
    assert "SUPER-SECRET-VALUE" not in repr(c)


def test_sensitive_field_names_are_redacted() -> None:
    markup = """
    <script>
      function assignToDate() {
        document.getElementById('customerToken').value = document.getElementById('myForm1:calendarFromRegion').value;
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert "customerToken" not in repr(c)
    assert "<redacted-field>" in c["writes"]
