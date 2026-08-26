"""Regression tests for 0.1.18 assignToDate PrimeFaces diagnostics."""

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


def _contract(js: str, *, onchange: bool = True):
    handler = '<input id="myForm1:calendarToRegion" onchange="assignToDate();" />' if onchange else ""
    return diag.assign_to_date_contract(
        f"""{handler}<script>function assignToDate() {{{js}}}</script>"""
    )


def test_primefaces_short_keys_are_mapped_and_listed() -> None:
    c = _contract(
        "PrimeFaces.ab({s:'myForm1:To',p:'myForm1:To',u:'myForm1:list',e:'change',pa:[{name:'foo',value:'SECRET'}]});"
    )
    assert c["ajax"] is True
    assert c["ajax_type"] == "PrimeFaces.ab"
    assert c["ajax_direct"] is True
    assert c["primefaces_keys"] == ["s", "p", "u", "e", "pa"]
    assert c["source"] == "myForm1:To"
    assert c["execute"] == "myForm1:To"
    assert c["render"] == "myForm1:list"
    assert c["event"] == "change"
    assert c["params_present"] is True
    assert c["param_names"] == ["foo"]
    assert "SECRET" not in repr(c)


def test_spelled_out_keys_are_mapped() -> None:
    c = _contract(
        "PrimeFaces.ab({source:'Src',process:'Proc',update:'Upd',event:'Evt',params:[{name:'bar',value:'NOPE'}]});"
    )
    assert c["primefaces_keys"] == ["source", "process", "update", "event", "params"]
    assert c["source"] == "Src"
    assert c["execute"] == "Proc"
    assert c["render"] == "Upd"
    assert c["event"] == "Evt"
    assert c["param_names"] == ["bar"]
    assert "NOPE" not in repr(c)


def test_missing_execute_and_event_are_explicit() -> None:
    c = _contract("PrimeFaces.ab({s:'Src',u:'Upd'});")
    assert c["execute_present"] is False
    assert c["execute"] is None
    assert c["execute_dynamic"] is False
    assert c["event_present"] is False
    assert c["event"] is None
    assert c["event_dynamic"] is False


def test_static_source_preserves_exact_case() -> None:
    c = _contract("PrimeFaces.ab({s:'myform:j_idt1320'});")
    assert c["source_present"] is True
    assert c["source_kind"] == "static_string"
    assert c["source"] == "myform:j_idt1320"
    assert c["source_dynamic"] is False


def test_dynamic_source_is_not_logged_as_expression() -> None:
    c = _contract("PrimeFaces.ab({s:widget.sourceId,u:'myForm1'});")
    assert c["source_present"] is True
    assert c["source_kind"] == "property_access"
    assert c["source"] is None
    assert c["source_dynamic"] is True
    assert "widget.sourceId" in c["source_refs"]
    assert "widget.sourceId" not in str(c["source"])


def test_render_function_call_is_classified_without_expression() -> None:
    c = _contract("PrimeFaces.ab({s:'Src',u:getRenderTarget(componentId)});")
    assert c["render_present"] is True
    assert c["render_kind"] == "function_call"
    assert c["render"] is None
    assert c["render_dynamic"] is True
    assert "getRenderTarget" in c["render_functions"]
    assert "componentId" in c["render_refs"]


def test_render_array_is_classified_without_values() -> None:
    c = _contract("PrimeFaces.ab({s:'Src',u:[targetOne,targetTwo]});")
    assert c["render_present"] is True
    assert c["render_kind"] == "array"
    assert c["render"] is None
    assert c["render_dynamic"] is True
    assert "targetOne" in c["render_refs"]
    assert "targetTwo" in c["render_refs"]


def test_concatenation_and_ternary_are_classified() -> None:
    c = _contract("PrimeFaces.ab({s:prefix + suffix,u:flag ? targetA : targetB});")
    assert c["source_kind"] == "concatenation"
    assert c["render_kind"] == "ternary"
    assert c["source"] is None and c["render"] is None


def test_wrapper_function_is_still_followed() -> None:
    markup = """
    <input id="myForm1:calendarToRegion" onchange="assignToDate();" />
    <script>
      function assignToDate() { helper(); }
      function helper() { PrimeFaces.ab({s:'to',u:'myForm1'}); }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is True
    assert c["ajax_via_function"] == "helper"
    assert c["source"] == "to"


def test_direct_jsf_ajax_support_is_preserved() -> None:
    c = _contract(
        "jsf.ajax.request('myForm1:calendarToRegion','change',{execute:'@this',render:'myForm1'});"
    )
    assert c["ajax_type"] == "jsf.ajax.request"
    assert c["source"] == "myForm1:calendarToRegion"
    assert c["execute"] == "@this"
    assert c["render"] == "myForm1"
    assert c["event"] == "change"


def test_false_positive_text_is_not_ajax() -> None:
    c = _contract("const note = 'PrimeFaces.ab({s:fake}) ajax jsf.ajax.request';")
    assert c["ajax"] is False
    assert c["ajax_type"] is None


def test_nested_blocks_and_parentheses_in_strings_are_safe() -> None:
    markup = """
    <script>
      function assignToDate() {
        if (true) { const x = "}),({ not structure"; helper(); }
      }
      function helper() {
        PrimeFaces.ab({s:'Src',u:getTarget("}),({")});
      }
    </script>
    """
    c = diag.assign_to_date_contract(markup)
    assert c["ajax"] is True
    assert c["source"] == "Src"
    assert c["render_kind"] == "function_call"


def test_local_copy_hidden_and_called_by_remain_structural_only() -> None:
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
    assert c["copies"] == [{"from": "myForm1:calendarFromRegion", "to": "myForm1:calendarToRegion"}]
    assert c["hidden_fields"] == ["myForm1:rangeHelper"]
    assert c["called_by"] == ["myForm1:calendarToRegion"]
    assert "SUPER-SECRET-VALUE" not in repr(c)


def test_sensitive_field_names_are_redacted() -> None:
    c = _contract(
        "document.getElementById('customerToken').value = document.getElementById('myForm1:calendarFromRegion').value;"
    )
    assert "customerToken" not in repr(c)
    assert "<redacted-field>" in c["writes"]
