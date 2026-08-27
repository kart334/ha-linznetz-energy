"""Focused privacy-safe diagnostics for assignToDate PrimeFaces f/u/pa options.

This module only inspects already-rendered JavaScript in memory. It never executes
JavaScript and never adds portal requests. Logs contain only structural metadata,
safe form/component identifiers, function/variable names, and parameter names.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import assign_to_date_diagnostics as base
from .date_contract_diagnostics import _markup_from_response, _request_step
from .inline_date_handler_diagnostics import InlineDateHandlerDiagnosticSessionProxy

_LOGGER = logging.getLogger(__name__)
_SAFE_COMPONENT_TOKEN = re.compile(r"^[A-Za-z0-9_:@.\-$]+$")
_STRING_LITERAL = re.compile(r"(['\"])(.*?)\1", re.DOTALL)
_COMPONENT_FRAGMENT = re.compile(r"[A-Za-z0-9_.\-$]+:[A-Za-z0-9_:@.\-$]+")
_CSS_ID_REF = re.compile(r"#([A-Za-z0-9_:@.\-$]+)")
_JS_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_ATTR_SEARCH = re.compile(
    r"^@\(\[\s*([A-Za-z_$][A-Za-z0-9_$-]*)\s*(\^=|\$=|\*=|~=|\|=|=)\s*"
    r"([A-Za-z0-9_:@.\-$]+)\s*\]\)$"
)
_ARGUMENT_INDEX = re.compile(r"\barguments\s*\[\s*(\d{1,3})\s*\]")


def _primefaces_object(body: str) -> str | None:
    masked = base._mask_strings_and_comments(body)
    match = re.search(r"PrimeFaces\.ab\s*\(", masked)
    if not match:
        return None
    start = masked.find("(", match.start())
    inner, _ = base._balanced_segment(body, start, "(", ")")
    if inner is None:
        return None
    args = base._split_top_level(inner)
    return args[0] if args else None


def _safe_component_string(value: str | None) -> tuple[str | None, bool, list[str]]:
    """Return exact static component target only when every target is structurally safe."""
    if value is None or not value:
        return None, False, []
    tokens = value.split()
    if not tokens:
        return None, False, []
    refs: list[str] = []
    for token in tokens:
        if not _SAFE_COMPONENT_TOKEN.fullmatch(token):
            return None, False, []
        safe = base._safe_name(token)
        if safe is None or safe.startswith("<"):
            return None, False, []
        refs.append(token)
    return value, True, refs


def _literal_component_refs(expr: str | None) -> list[str]:
    if not expr:
        return []
    refs: list[str] = []
    for match in _STRING_LITERAL.finditer(expr):
        raw = match.group(2)
        for token in raw.split():
            if ":" not in token and not token.startswith("@"):
                continue
            if not _SAFE_COMPONENT_TOKEN.fullmatch(token):
                continue
            safe = base._safe_name(token)
            if safe and not safe.startswith("<") and token not in refs:
                refs.append(token)
    return refs[:30]


def _render_structure(value: str | None) -> dict[str, object]:
    """Classify a static render literal without exposing an unsafe raw value."""
    result: dict[str, object] = {
        "render_length": len(value) if value is not None else None,
        "render_selector_kind": None,
        "render_reserved": None,
        "render_char_classes": [],
        "render_safe_fragments": [],
        "render_css_id_refs": [],
        "render_attr_name": None,
        "render_attr_operator": None,
        "render_attr_value": None,
    }
    if value is None:
        return result

    if value in {"@none", "@all", "@this", "@form"}:
        result["render_selector_kind"] = "reserved_keyword"
        result["render_reserved"] = value
    elif value.startswith("@(") and value.endswith(")"):
        attr_match = _ATTR_SEARCH.fullmatch(value)
        result["render_selector_kind"] = (
            "primefaces_attribute_search" if attr_match else "primefaces_search"
        )
        if attr_match:
            attr_name, operator, attr_value = attr_match.groups()
            safe_name = base._safe_name(attr_name)
            safe_value = base._safe_name(attr_value)
            if safe_name and safe_value and not safe_name.startswith("<") and not safe_value.startswith("<"):
                result["render_attr_name"] = attr_name
                result["render_attr_operator"] = operator
                result["render_attr_value"] = attr_value
    elif _CSS_ID_REF.fullmatch(value):
        result["render_selector_kind"] = "css_id"
    else:
        result["render_selector_kind"] = "unknown_static"

    classes: list[str] = []
    for label, chars in (
        ("at", "@"),
        ("hash", "#"),
        ("colon", ":"),
        ("dot", "."),
        ("comma", ","),
        ("parentheses", "()"),
        ("brackets", "[]"),
        ("equals", "="),
        ("quotes", "'\""),
        ("hyphen", "-"),
        ("whitespace", " \t\r\n"),
    ):
        if any(char in value for char in chars):
            classes.append(label)
    known = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_:@.\-$#,()[]=\"' \t\r\n")
    if any(char not in known for char in value):
        classes.append("other")
    result["render_char_classes"] = classes

    fragments: list[str] = []
    for match in _COMPONENT_FRAGMENT.finditer(value):
        fragment = match.group(0)
        safe = base._safe_name(fragment)
        if safe and not safe.startswith("<") and fragment not in fragments:
            fragments.append(fragment)
    result["render_safe_fragments"] = fragments[:20]

    css_refs: list[str] = []
    for match in _CSS_ID_REF.finditer(value):
        ref = match.group(1)
        safe = base._safe_name(ref)
        if safe and not safe.startswith("<") and ref not in css_refs:
            css_refs.append(ref)
    result["render_css_id_refs"] = css_refs[:20]
    return result


def _assign_to_date_params(markup: str) -> tuple[list[str], bool]:
    """Return only safe parameter names from the assignToDate declaration."""
    masked = base._mask_strings_and_comments(markup)
    match = re.search(r"\bfunction\s+assignToDate\s*\(", masked)
    if not match:
        return [], False
    start = masked.find("(", match.start())
    inner, _ = base._balanced_segment(markup, start, "(", ")")
    if inner is None:
        return [], True

    names: list[str] = []
    dynamic = False
    for item in base._split_top_level(inner):
        candidate = item.strip()
        if not candidate:
            continue
        if "=" in candidate:
            candidate = candidate.split("=", 1)[0].strip()
            dynamic = True
        if _JS_IDENTIFIER.fullmatch(candidate):
            safe = base._safe_name(candidate)
            if safe and not safe.startswith("<") and safe not in names:
                names.append(safe)
        else:
            dynamic = True
    return names[:20], dynamic


def _param_structure(expr: str | None) -> dict[str, object]:
    if expr is None:
        return {
            "params_kind": None,
            "param_names": [],
            "param_names_dynamic": False,
            "param_name_refs": [],
        }
    kind = base._expression_kind(expr)
    names: list[str] = []
    refs: list[str] = []
    dynamic = False

    containers: list[str] = []
    stripped = expr.strip()
    if kind == "array" and stripped.startswith("[") and stripped.endswith("]"):
        containers = base._split_top_level(stripped[1:-1])
    elif kind == "object":
        containers = [stripped]

    for container in containers:
        for key, name_expr in base._parse_object_members(container):
            if key.lower() not in {"name", "n"}:
                continue
            literal = base._literal(name_expr)
            if literal is not None:
                safe = base._safe_name(literal)
                if safe and not safe.startswith("<") and safe not in names:
                    names.append(safe)
            else:
                dynamic = True
                for ref in base._safe_expr_names(name_expr):
                    if ref not in refs:
                        refs.append(ref)

    if kind == "array":
        for item in containers:
            for match in re.finditer(r"\b(?:name|n)\s*:\s*([^,}]+)", item, re.I | re.S):
                name_expr = match.group(1).strip()
                literal = base._literal(name_expr)
                if literal is not None:
                    safe = base._safe_name(literal)
                    if safe and not safe.startswith("<") and safe not in names:
                        names.append(safe)
                else:
                    dynamic = True
                    for ref in base._safe_expr_names(name_expr):
                        if ref not in refs:
                            refs.append(ref)

    if kind not in {"array", "object"}:
        dynamic = True
        refs = base._safe_expr_names(expr)

    return {
        "params_kind": kind,
        "param_names": names[:20],
        "param_names_dynamic": dynamic,
        "param_name_refs": refs[:20],
    }


def _arguments_structure(expr: str | None) -> dict[str, object]:
    """Classify use of the JavaScript arguments object without logging values."""
    result: dict[str, object] = {
        "params_expr_length": len(expr.strip()) if expr is not None else None,
        "params_arguments_mode": None,
        "params_argument_indexes": [],
        "params_expr_char_classes": [],
        "params_function_refs": [],
    }
    if expr is None:
        return result

    stripped = expr.strip()
    compact = re.sub(r"\\s+", "", stripped)
    indexes = sorted({int(value) for value in _ARGUMENT_INDEX.findall(stripped)})
    result["params_argument_indexes"] = indexes[:20]

    if compact == "arguments":
        mode = "bare_arguments"
    elif re.fullmatch(r"arguments\\[\\d{1,3}\\]", compact):
        mode = "indexed_arguments"
    elif compact == "[...arguments]":
        mode = "spread_arguments_array"
    elif compact in {"Array.from(arguments)", "Array.prototype.slice.call(arguments)"}:
        mode = "arguments_array_conversion"
    elif re.search(r"\\barguments\\b", stripped):
        mode = "composite_arguments_expression"
    else:
        mode = "no_arguments_reference"
    result["params_arguments_mode"] = mode

    classes: list[str] = []
    for label, chars in (
        ("brackets", "[]"),
        ("parentheses", "()"),
        ("dot", "."),
        ("comma", ","),
        ("spread", "..."),
        ("operators", "+-*/?:"),
        ("whitespace", " \\t\\r\\n"),
    ):
        if chars == "...":
            present = chars in stripped
        else:
            present = any(char in stripped for char in chars)
        if present:
            classes.append(label)
    result["params_expr_char_classes"] = classes
    result["params_function_refs"] = base._function_names(stripped)[:20]
    return result


def _assign_call_structure(soup: Any) -> dict[str, object]:
    """Report only arity and expression shapes of inline assignToDate callers."""
    arities: list[int] = []
    kinds: list[str] = []
    param_names: list[str] = []
    for tag in soup.find_all(True):
        for raw_value in tag.attrs.values():
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for value in values:
                if not isinstance(value, str) or "assignToDate" not in value:
                    continue
                masked = base._mask_strings_and_comments(value)
                for match in re.finditer(r"\\bassignToDate\\s*\\(", masked):
                    start = masked.find("(", match.start())
                    inner, _ = base._balanced_segment(value, start, "(", ")")
                    if inner is None:
                        continue
                    args = base._split_top_level(inner) if inner.strip() else []
                    if len(args) not in arities:
                        arities.append(len(args))
                    for arg in args:
                        kind = base._expression_kind(arg)
                        if kind not in kinds:
                            kinds.append(kind)
                        detail = _param_structure(arg)
                        for name in detail["param_names"]:
                            if name not in param_names:
                                param_names.append(name)
    return {
        "assign_call_arities": arities[:20],
        "assign_call_arg_kinds": kinds[:20],
        "assign_call_param_names": param_names[:20],
    }


def assign_to_date_detail_contract(markup: str) -> dict[str, object]:
    contract = base.assign_to_date_contract(markup)
    functions, soup = base._script_functions(markup)
    body = functions.get("assignToDate")
    contract.update(
        {
            "f_present": False,
            "f_kind": None,
            "f": None,
            "f_dynamic": False,
            "f_refs": [],
            "f_functions": [],
            "f_role": "unknown",
            "render_safe": False,
            "params_kind": None,
            "param_names_dynamic": False,
            "param_name_refs": [],
            "component_refs": [],
            "render_length": None,
            "render_selector_kind": None,
            "render_reserved": None,
            "render_char_classes": [],
            "render_safe_fragments": [],
            "render_css_id_refs": [],
            "render_attr_name": None,
            "render_attr_operator": None,
            "render_attr_value": None,
            "params_source": None,
            "assign_param_names": [],
            "assign_params_dynamic": False,
            "params_expr_length": None,
            "params_arguments_mode": None,
            "params_argument_indexes": [],
            "params_expr_char_classes": [],
            "params_function_refs": [],
            "assign_call_arities": [],
            "assign_call_arg_kinds": [],
            "assign_call_param_names": [],
        }
    )
    if body is None:
        return contract
    obj = _primefaces_object(body)
    if obj is None:
        return contract

    members = base._parse_object_members(obj)
    by_key = {key.lower(): expr for key, expr in members}
    f_expr = by_key.get("f")
    u_expr = by_key.get("u") or by_key.get("update") or by_key.get("render")
    pa_expr = by_key.get("pa") or by_key.get("params")

    assign_param_names, assign_params_dynamic = _assign_to_date_params(markup)
    contract["assign_param_names"] = assign_param_names
    contract["assign_params_dynamic"] = assign_params_dynamic
    if pa_expr is not None:
        contract["params_source"] = (
            "arguments" if pa_expr.strip() == "arguments" else "other"
        )

    if f_expr is not None:
        contract["f_present"] = True
        contract["f_kind"] = base._expression_kind(f_expr)
        literal = base._literal(f_expr)
        if literal is not None:
            safe_value, safe, refs = _safe_component_string(literal)
            contract["f"] = safe_value if safe else None
            contract["f_dynamic"] = False
            contract["f_refs"] = refs if safe else []
            form = soup.find("form", id=literal) or soup.find("form", attrs={"name": literal})
            contract["f_role"] = "form" if form is not None else "unknown"
        else:
            contract["f_dynamic"] = True
            contract["f_refs"] = base._safe_expr_names(f_expr)
            contract["f_functions"] = base._function_names(f_expr)

    if u_expr is not None and base._expression_kind(u_expr) == "static_string":
        literal = base._literal(u_expr)
        safe_value, safe, refs = _safe_component_string(literal)
        contract["render_safe"] = safe
        contract["render"] = safe_value if safe else None
        contract["render_refs"] = refs if safe else _literal_component_refs(u_expr)
        contract.update(_render_structure(literal))
    elif u_expr is not None:
        contract["render_safe"] = False

    contract.update(_param_structure(pa_expr))
    contract.update(_arguments_structure(pa_expr))
    contract.update(_assign_call_structure(soup))

    component_refs: list[str] = []
    source = contract.get("source")
    if isinstance(source, str) and not source.startswith("<") and ":" in source:
        component_refs.append(source)
    # Parameter values are deliberately excluded, even if they look like component IDs.
    for expr in (f_expr, u_expr):
        for ref in _literal_component_refs(expr):
            if ref not in component_refs:
                component_refs.append(ref)
    contract["component_refs"] = component_refs[:30]
    return contract


class AssignToDateDetailDiagnosticSessionProxy(InlineDateHandlerDiagnosticSessionProxy):
    """Log the focused f/u/pa structure without changing any portal request."""

    async def _diagnose(self, method: str, data: Any, response: Any) -> None:
        await super()._diagnose(method, data, response)
        try:
            body = await response.text()
        except Exception:  # pragma: no cover
            return
        markup = _markup_from_response(body)
        if not markup or "assignToDate" not in markup:
            return
        c = assign_to_date_detail_contract(markup)
        if not c.get("found"):
            return
        _LOGGER.warning(
            "LINZ NETZ diagnostic-assign-to-date: step=%s ajax=%s ajax_type=%s ajax_direct=%s primefaces_keys=%s "
            "source_present=%s source_kind=%s source=%s source_dynamic=%s source_refs=%s "
            "f_present=%s f_kind=%s f=%s f_dynamic=%s f_refs=%s f_functions=%s f_role=%s "
            "render_present=%s render_kind=%s render=%s render_safe=%s render_dynamic=%s render_refs=%s render_functions=%s "
            "render_length=%s render_selector_kind=%s render_reserved=%s render_char_classes=%s "
            "render_safe_fragments=%s render_css_id_refs=%s render_attr_name=%s render_attr_operator=%s render_attr_value=%s "
            "params_present=%s params_kind=%s param_names=%s param_names_dynamic=%s param_name_refs=%s "
            "params_source=%s assign_param_names=%s assign_params_dynamic=%s params_expr_length=%s "
            "params_arguments_mode=%s params_argument_indexes=%s params_expr_char_classes=%s params_function_refs=%s "
            "assign_call_arities=%s assign_call_arg_kinds=%s assign_call_param_names=%s "
            "component_refs=%s execute_present=%s event_present=%s partial_event=%s called_by=%s submit=%s",
            _request_step(method, data), c["ajax"], c["ajax_type"], c["ajax_direct"], c["primefaces_keys"],
            c["source_present"], c["source_kind"], c["source"], c["source_dynamic"], c["source_refs"],
            c["f_present"], c["f_kind"], c["f"], c["f_dynamic"], c["f_refs"], c["f_functions"], c["f_role"],
            c["render_present"], c["render_kind"], c["render"], c["render_safe"], c["render_dynamic"], c["render_refs"], c["render_functions"],
            c["render_length"], c["render_selector_kind"], c["render_reserved"], c["render_char_classes"],
            c["render_safe_fragments"], c["render_css_id_refs"], c["render_attr_name"], c["render_attr_operator"], c["render_attr_value"],
            c["params_present"], c["params_kind"], c["param_names"], c["param_names_dynamic"], c["param_name_refs"],
            c["params_source"], c["assign_param_names"], c["assign_params_dynamic"], c["params_expr_length"],
            c["params_arguments_mode"], c["params_argument_indexes"], c["params_expr_char_classes"], c["params_function_refs"],
            c["assign_call_arities"], c["assign_call_arg_kinds"], c["assign_call_param_names"],
            c["component_refs"], c["execute_present"], c["event_present"], c["partial_event"], c["called_by"], c["submit"],
        )
