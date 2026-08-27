"""Regression tests for focused 0.1.19 assignToDate f/u/pa diagnostics."""

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
_load("assign_to_date_diagnostics")
diag = _load("assign_to_date_detail_diagnostics")


def _contract(options: str, *, form: str = "", params: str = ""):
    markup = f"""
    {form}
    <input id="myForm1:calendarToRegion" onchange="assignToDate();" />
    <script>function assignToDate({params}) {{ PrimeFaces.ab({options}); }}</script>
    """
    return diag.assign_to_date_detail_contract(markup)


def test_f_static_string_and_role_form_when_markup_proves_it() -> None:
    c = _contract("{s:'myform:j_idt1320',f:'myForm1',u:'myForm1:list',pa:[]}", form='<form id="myForm1"></form>')
    assert c["f_present"] is True
    assert c["f_kind"] == "static_string"
    assert c["f"] == "myForm1"
    assert c["f_dynamic"] is False
    assert c["f_role"] == "form"


def test_f_role_unknown_without_structural_form_evidence() -> None:
    c = _contract("{s:'Src',f:'maybeForm',u:'myForm1:list',pa:[]}")
    assert c["f"] == "maybeForm"
    assert c["f_role"] == "unknown"


def test_f_dynamic_expression_only_reports_refs_and_functions() -> None:
    c = _contract("{s:'Src',f:getFormId(widget.form),u:'myForm1:list',pa:[]}")
    assert c["f_present"] is True
    assert c["f_kind"] == "function_call"
    assert c["f"] is None
    assert c["f_dynamic"] is True
    assert "getFormId" in c["f_functions"]
    assert "widget.form" in c["f_refs"]
    assert c["f_role"] == "unknown"


def test_render_safe_static_component_id_is_exposed_exactly() -> None:
    c = _contract("{s:'myform:j_idt1320',f:'myForm1',u:'myForm1:list',pa:[]}")
    assert c["render_present"] is True
    assert c["render_kind"] == "static_string"
    assert c["render_safe"] is True
    assert c["render"] == "myForm1:list"
    assert c["render_refs"] == ["myForm1:list"]


def test_render_multiple_safe_targets_preserve_exact_string() -> None:
    c = _contract("{s:'Src',f:'myForm1',u:'myForm1:list myForm1:messages',pa:[]}")
    assert c["render_safe"] is True
    assert c["render"] == "myForm1:list myForm1:messages"
    assert c["render_refs"] == ["myForm1:list", "myForm1:messages"]


def test_render_unsafe_static_string_is_not_logged_raw() -> None:
    c = _contract("{s:'Src',f:'myForm1',u:'@(.private-widget)',pa:[]}")
    assert c["render_kind"] == "static_string"
    assert c["render_safe"] is False
    assert c["render"] is None
    assert "@(.private-widget)" not in repr(c)


def test_params_array_only_emits_names_never_values() -> None:
    c = _contract("{s:'Src',f:'myForm1',u:'myForm1:list',pa:[{name:'foo',value:'SECRET-A'},{name:'bar',value:'SECRET-B'}]}")
    assert c["params_present"] is True
    assert c["params_kind"] == "array"
    assert c["param_names"] == ["foo", "bar"]
    assert c["param_names_dynamic"] is False
    assert "SECRET-A" not in repr(c)
    assert "SECRET-B" not in repr(c)


def test_params_object_is_classified_and_name_extracted() -> None:
    c = _contract("{s:'Src',f:'myForm1',u:'myForm1:list',pa:{name:'onlyName',value:'SECRET'}}")
    assert c["params_kind"] == "object"
    assert c["param_names"] == ["onlyName"]
    assert "SECRET" not in repr(c)


def test_params_dynamic_expression_reports_refs_not_values() -> None:
    c = _contract("{s:'Src',f:'myForm1',u:'myForm1:list',pa:buildParams(paramNames)}")
    assert c["params_kind"] == "function_call"
    assert c["param_names"] == []
    assert c["param_names_dynamic"] is True
    assert "buildParams" in c["param_name_refs"]
    assert "paramNames" in c["param_name_refs"]


def test_dynamic_param_name_only_reports_name_reference() -> None:
    c = _contract("{s:'Src',f:'myForm1',u:'myForm1:list',pa:[{name:paramName,value:secretValue}]}")
    assert c["param_names"] == []
    assert c["param_names_dynamic"] is True
    assert "paramName" in c["param_name_refs"]
    assert "secretValue" not in c["param_name_refs"]


def test_component_refs_only_include_safe_component_literals() -> None:
    c = _contract("{s:'myform:j_idt1320',f:'myForm1',u:'myForm1:calendarFromRegion myForm1:calendarToRegion',pa:[{name:'foo',value:'myForm1:calendarToRegion'}]}")
    assert c["component_refs"] == [
        "myform:j_idt1320",
        "myForm1:calendarFromRegion",
        "myForm1:calendarToRegion",
    ]


def test_case_sensitive_ids_remain_unchanged() -> None:
    c = _contract("{s:'myform:j_idt1320',f:'myForm1',u:'myForm1:List',pa:[]}")
    assert c["source"] == "myform:j_idt1320"
    assert c["f"] == "myForm1"
    assert c["render"] == "myForm1:List"


def test_unsafe_render_reports_only_structure_and_safe_fragments() -> None:
    c = _contract(
        "{s:'Src',f:'myForm1',u:'@(#myForm1:list > .private-widget)',pa:[]}"
    )
    assert c["render_safe"] is False
    assert c["render"] is None
    assert c["render_selector_kind"] == "primefaces_search"
    assert c["render_safe_fragments"] == ["myForm1:list"]
    assert c["render_css_id_refs"] == ["myForm1:list"]
    assert "hash" in c["render_char_classes"]
    assert "parentheses" in c["render_char_classes"]
    assert "@(#myForm1:list > .private-widget)" not in repr(c)


def test_reserved_render_keyword_is_exposed_from_finite_safe_set() -> None:
    c = _contract("{s:'Src',f:'myForm1',u:'@none',pa:[]}")
    assert c["render_selector_kind"] == "reserved_keyword"
    assert c["render_reserved"] == "@none"


def test_arguments_source_reports_assign_to_date_parameter_names() -> None:
    c = _contract(
        "{s:'Src',f:'myForm1',u:'myForm1:list',pa:arguments}",
        params="fromDate, toDate",
    )
    assert c["params_source"] == "arguments"
    assert c["assign_param_names"] == ["fromDate", "toDate"]
    assert c["assign_params_dynamic"] is False
    assert c["param_names"] == []
    assert c["param_names_dynamic"] is True


def test_defaulted_function_parameter_marks_signature_dynamic() -> None:
    c = _contract(
        "{s:'Src',f:'myForm1',u:'myForm1:list',pa:arguments}",
        params="fromDate = fallbackDate",
    )
    assert c["assign_param_names"] == ["fromDate"]
    assert c["assign_params_dynamic"] is True


def test_false_positive_text_stays_false_positive_free() -> None:
    markup = """
    <script>
      function assignToDate() {
        const note = 'PrimeFaces.ab({s:\"fake\",f:\"x\",u:\"y\",pa:[]})';
        // PrimeFaces.ab({s:'alsoFake'})
      }
    </script>
    """
    c = diag.assign_to_date_detail_contract(markup)
    assert c["ajax"] is False
    assert c["ajax_type"] is None
    assert c["f_present"] is False
