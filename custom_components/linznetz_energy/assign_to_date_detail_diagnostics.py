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

    # PrimeFaces parameter arrays can contain nested object literals. Parse those too.
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
    elif u_expr is not None:
        contract["render_safe"] = False

    param_info = _param_structure(pa_expr)
    contract.update(param_info)

    component_refs: list[str] = []
    source = contract.get("source")
    if isinstance(source, str) and not source.startswith("<") and ":" in source:
        component_refs.append(source)
    for expr in (f_expr, u_expr, pa_expr):
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
            "params_present=%s params_kind=%s param_names=%s param_names_dynamic=%s param_name_refs=%s "
            "component_refs=%s execute_present=%s event_present=%s partial_event=%s called_by=%s submit=%s",
            _request_step(method, data), c["ajax"], c["ajax_type"], c["ajax_direct"], c["primefaces_keys"],
            c["source_present"], c["source_kind"], c["source"], c["source_dynamic"], c["source_refs"],
            c["f_present"], c["f_kind"], c["f"], c["f_dynamic"], c["f_refs"], c["f_functions"], c["f_role"],
            c["render_present"], c["render_kind"], c["render"], c["render_safe"], c["render_dynamic"], c["render_refs"], c["render_functions"],
            c["params_present"], c["params_kind"], c["param_names"], c["param_names_dynamic"], c["param_name_refs"],
            c["component_refs"], c["execute_present"], c["event_present"], c["partial_event"], c["called_by"], c["submit"],
        )
