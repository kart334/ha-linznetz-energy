"""Privacy-safe structural diagnostics for the portal's assignToDate JavaScript.

The productive JavaScript is inspected in memory only. Logs contain structural
field/function names and request metadata, never JavaScript source, field values,
ViewState, cookies, credentials, session data, customer data, or meter data.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Final

from bs4 import BeautifulSoup

from .date_contract_diagnostics import _markup_from_response, _request_step
from .inline_date_handler_diagnostics import InlineDateHandlerDiagnosticSessionProxy

_LOGGER = logging.getLogger(__name__)
_SAFE_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_:@.\-$]+$")
_SENSITIVE_NAME_RE: Final = re.compile(
    r"password|passwd|secret|token|session|viewstate|customer|kunde|meter|zaehl|account|username|email",
    re.IGNORECASE,
)
_CALL_RE: Final = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_FUNCTION_DEF_RE: Final = re.compile(
    r"(?:\bfunction\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)|"
    r"\b(?:window\.)?([A-Za-z_$][\w$]*)\s*=\s*function\s*\([^)]*\)|"
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\([^)]*\)\s*=>)\s*\{",
    re.IGNORECASE | re.MULTILINE,
)
_GET_BY_ID_RE: Final = re.compile(r"document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", re.I)
_GET_BY_NAME_RE: Final = re.compile(r"document\.getElementsByName\(\s*['\"]([^'\"]+)['\"]\s*\)", re.I)
_JQUERY_ID_RE: Final = re.compile(r"(?:\$|jQuery)\(\s*['\"]#([^'\"]+)['\"]\s*\)")


def _safe_name(value: str | None) -> str | None:
    if not value:
        return None
    value = value[:160]
    if _SENSITIVE_NAME_RE.search(value):
        return "<redacted-field>"
    return value if _SAFE_NAME_RE.fullmatch(value) else "<non-component>"


def _mask_strings_and_comments(text: str) -> str:
    """Mask string/comment contents while preserving indices for structural scans."""
    out = list(text)
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if ch in "\r\n":
                line_comment = False
            else:
                out[i] = " "
            i += 1
            continue
        if block_comment:
            out[i] = " "
            if ch == "*" and nxt == "/":
                out[i + 1] = " "
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
                out[i] = " "
            elif ch == "\\":
                escaped = True
                out[i] = " "
            elif ch == quote:
                quote = None
            else:
                out[i] = " "
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "/" and nxt == "/":
            out[i] = out[i + 1] = " "
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            out[i] = out[i + 1] = " "
            block_comment = True
            i += 2
            continue
        i += 1
    return "".join(out)


def _balanced_segment(text: str, start: int, open_ch: str, close_ch: str) -> tuple[str | None, int]:
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    i = start
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if ch in "\r\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    return None, len(text)


def _script_functions(markup: str) -> tuple[dict[str, str], BeautifulSoup]:
    soup = BeautifulSoup(markup, "html.parser")
    functions: dict[str, str] = {}
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text:
            continue
        for match in _FUNCTION_DEF_RE.finditer(text):
            name = next((group for group in match.groups() if group), None)
            if not name:
                continue
            brace = text.find("{", match.start(), match.end() + 1)
            if brace < 0:
                continue
            body, _ = _balanced_segment(text, brace, "{", "}")
            if body is not None:
                functions.setdefault(name, body)
    return functions, soup


def _field_accesses(body: str) -> tuple[list[str], list[str]]:
    reads: list[str] = []
    writes: list[str] = []
    for pattern in (_GET_BY_ID_RE, _GET_BY_NAME_RE, _JQUERY_ID_RE):
        for match in pattern.finditer(body):
            name = _safe_name(match.group(1))
            if not name:
                continue
            tail = body[match.end() : match.end() + 120]
            is_write = bool(
                re.match(r"\s*\.value(?:AsDate)?\s*=", tail)
                or re.match(r"\s*\.val\s*\([^)]", tail)
                or re.match(r"\s*\.setAttribute\s*\(\s*['\"]value['\"]\s*,", tail, re.I)
            )
            target = writes if is_write else reads
            if name not in target:
                target.append(name)
    return reads[:20], writes[:20]


def _calls(body: str) -> list[str]:
    masked = _mask_strings_and_comments(body)
    blocked = {
        "document.getElementById", "document.getElementsByName", "document.querySelector",
        "PrimeFaces.ab", "jsf.ajax.request", "faces.ajax.request", "$", "jQuery",
    }
    result: list[str] = []
    for match in _CALL_RE.finditer(masked):
        name = match.group(1)
        if name in blocked or name.startswith("document."):
            continue
        safe = _safe_name(name)
        if safe and safe not in result:
            result.append(safe)
    return result[:20]


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {"(": ")", "{": "}", "[": "]"}
    for i, ch in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch == delimiter and not stack:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _split_member(member: str) -> tuple[str | None, str | None]:
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {"(": ")", "{": "}", "[": "]"}
    for i, ch in enumerate(member):
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch == ":" and not stack:
            raw_key = member[:i].strip()
            value = member[i + 1 :].strip()
            literal_key = _literal(raw_key)
            key = literal_key if literal_key is not None else raw_key
            return (key, value) if re.fullmatch(r"[A-Za-z_$][\w$]*", key) else (None, None)
    return None, None


def _literal(expr: str) -> str | None:
    match = re.fullmatch(r"\s*(['\"])(.*?)\1\s*", expr, re.DOTALL)
    return match.group(2) if match else None


def _safe_expr_names(expr: str) -> list[str]:
    stripped = _mask_strings_and_comments(expr)
    names: list[str] = []
    for raw in re.findall(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\b", stripped):
        if raw in {"true", "false", "null", "undefined", "return", "function"}:
            continue
        safe = _safe_name(raw)
        if safe and safe not in names:
            names.append(safe)
    return names[:12]


def _function_names(expr: str) -> list[str]:
    masked = _mask_strings_and_comments(expr)
    result: list[str] = []
    for match in _CALL_RE.finditer(masked):
        safe = _safe_name(match.group(1))
        if safe and safe not in result:
            result.append(safe)
    return result[:10]


def _expression_kind(expr: str) -> str:
    value = expr.strip()
    if _literal(value) is not None:
        return "static_string"
    if value.startswith("[") and value.endswith("]"):
        return "array"
    if value.startswith("{") and value.endswith("}"):
        return "object"
    if "?" in _mask_strings_and_comments(value) and ":" in _mask_strings_and_comments(value):
        return "ternary"
    if "+" in _mask_strings_and_comments(value):
        return "concatenation"
    if re.fullmatch(r"[A-Za-z_$][\w$]*", value):
        return "identifier"
    if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+", value):
        return "property_access"
    if re.match(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\(", value):
        return "function_call"
    return "other_dynamic"


def _parse_object_members(obj_expr: str) -> list[tuple[str, str]]:
    value = obj_expr.strip()
    if not (value.startswith("{") and value.endswith("}")):
        return []
    result: list[tuple[str, str]] = []
    for member in _split_top_level(value[1:-1]):
        key, expr = _split_member(member)
        if key and expr is not None:
            result.append((key, expr))
    return result


def _params_names(expr: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"\b(?:name|n)\s*:\s*(['\"])(.*?)\1", expr, re.I | re.S):
        safe = _safe_name(match.group(2))
        if safe and safe not in names:
            names.append(safe)
    return names[:20]


def _field_result(expr: str | None, present: bool) -> dict[str, object]:
    if not present or expr is None:
        return {"present": False, "kind": None, "value": None, "dynamic": False, "refs": [], "functions": []}
    kind = _expression_kind(expr)
    literal = _literal(expr)
    static = kind == "static_string" and literal is not None
    return {
        "present": True,
        "kind": kind,
        "value": _safe_name(literal) if static else None,
        "dynamic": not static,
        "refs": [] if static else _safe_expr_names(expr),
        "functions": [] if static else _function_names(expr),
    }


def _primefaces_options(obj_expr: str) -> dict[str, object]:
    members = _parse_object_members(obj_expr)
    keys = [key for key, _ in members][:40]
    aliases = {
        "s": "source", "source": "source",
        "p": "execute", "process": "execute", "execute": "execute",
        "u": "render", "update": "render", "render": "render",
        "e": "event", "event": "event",
        "pa": "params", "params": "params",
    }
    canonical: dict[str, str] = {}
    for key, expr in members:
        target = aliases.get(key.lower())
        if target and target not in canonical:
            canonical[target] = expr
    result: dict[str, object] = {"primefaces_keys": keys}
    for field in ("source", "execute", "render", "event"):
        info = _field_result(canonical.get(field), field in canonical)
        result[f"{field}_present"] = info["present"]
        result[f"{field}_kind"] = info["kind"]
        result[field] = info["value"]
        result[f"{field}_dynamic"] = info["dynamic"]
        result[f"{field}_refs"] = info["refs"]
        result[f"{field}_functions"] = info["functions"]
    params_expr = canonical.get("params")
    result["params_present"] = params_expr is not None
    result["param_names"] = _params_names(params_expr or "")
    result["partial_event"] = None
    return result


def _find_direct_ajax(body: str) -> dict[str, object] | None:
    masked = _mask_strings_and_comments(body)
    pf = re.search(r"PrimeFaces\.ab\s*\(", masked)
    if pf:
        start = masked.find("(", pf.start())
        inner, _ = _balanced_segment(body, start, "(", ")")
        if inner is not None:
            args = _split_top_level(inner)
            contract: dict[str, object] = {"ajax_type": "PrimeFaces.ab", "ajax_direct": True}
            contract.update(_primefaces_options(args[0] if args else ""))
            return contract
    jsf = re.search(r"(?:jsf|faces)\.ajax\.request\s*\(", masked)
    if jsf:
        start = masked.find("(", jsf.start())
        inner, _ = _balanced_segment(body, start, "(", ")")
        args = _split_top_level(inner or "")
        source_expr = args[0] if len(args) > 0 else None
        event_expr = args[1] if len(args) > 1 else None
        options = args[2] if len(args) > 2 else "{}"
        option_members = dict(_parse_object_members(options))
        contract: dict[str, object] = {
            "ajax_type": "jsf.ajax.request" if masked[jsf.start():].startswith("jsf") else "faces.ajax.request",
            "ajax_direct": True, "primefaces_keys": [], "params_present": False, "param_names": [], "partial_event": None,
        }
        for field, expr in (("source", source_expr), ("event", event_expr), ("execute", option_members.get("execute")), ("render", option_members.get("render"))):
            info = _field_result(expr, expr is not None)
            contract[f"{field}_present"] = info["present"]
            contract[f"{field}_kind"] = info["kind"]
            contract[field] = info["value"]
            contract[f"{field}_dynamic"] = info["dynamic"]
            contract[f"{field}_refs"] = info["refs"]
            contract[f"{field}_functions"] = info["functions"]
        return contract
    return None


def _resolve_ajax(functions: dict[str, str], start: str, max_depth: int = 3) -> dict[str, object] | None:
    seen: set[str] = set()
    queue: list[tuple[str, int, str | None]] = [(start, 0, None)]
    while queue:
        name, depth, via = queue.pop(0)
        if name in seen or depth > max_depth:
            continue
        seen.add(name)
        body = functions.get(name)
        if body is None:
            continue
        direct = _find_direct_ajax(body)
        if direct:
            direct["ajax_via_function"] = via
            direct["resolved_function"] = name
            return direct
        if depth == max_depth:
            continue
        for called in _calls(body):
            simple = called.split(".")[-1]
            if simple in functions and simple not in seen:
                queue.append((simple, depth + 1, simple if depth == 0 else (via or name)))
    return None


def _copy_pairs(body: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\.value\s*=\s*"
        r"document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\.value", re.I
    )
    result: list[dict[str, str]] = []
    for match in pattern.finditer(body):
        target, source = _safe_name(match.group(1)), _safe_name(match.group(2))
        if target and source:
            result.append({"from": source, "to": target})
    return result[:10]


def assign_to_date_contract(markup: str) -> dict[str, object]:
    functions, soup = _script_functions(markup)
    body = functions.get("assignToDate")
    base: dict[str, object] = {
        "found": body is not None, "ajax": False, "ajax_type": None, "ajax_direct": False,
        "ajax_via_function": None, "primefaces_keys": [],
        "source_present": False, "source_kind": None, "source": None, "source_dynamic": False, "source_refs": [], "source_functions": [],
        "execute_present": False, "execute_kind": None, "execute": None, "execute_dynamic": False, "execute_refs": [], "execute_functions": [],
        "render_present": False, "render_kind": None, "render": None, "render_dynamic": False, "render_refs": [], "render_functions": [],
        "event_present": False, "event_kind": None, "event": None, "event_dynamic": False, "event_refs": [], "event_functions": [],
        "params_present": False, "param_names": [], "partial_event": None,
        "reads": [], "writes": [], "calls": [], "copies": [], "hidden_fields": [], "called_by": [], "submit": False,
    }
    if body is None:
        return base
    reads, writes = _field_accesses(body)
    base["reads"], base["writes"] = reads, writes
    base["calls"] = _calls(body)
    base["copies"] = _copy_pairs(body)
    base["submit"] = bool(re.search(r"(?:\.submit\s*\(|\bsubmit\s*\()", _mask_strings_and_comments(body)))
    hidden: list[str] = []
    for name in list(dict.fromkeys(reads + writes)):
        if name.startswith("<"):
            continue
        control = soup.find(id=name) or soup.find(attrs={"name": name})
        if control is not None and str(control.get("type") or "").lower() == "hidden":
            hidden.append(name)
    base["hidden_fields"] = hidden[:20]
    called_by: list[str] = []
    for control in soup.find_all(["input", "select", "textarea", "button"]):
        for attr in ("onchange", "onblur", "onclick", "oninput"):
            if "assignToDate" in str(control.get(attr) or ""):
                ident = _safe_name(str(control.get("id") or control.get("name") or ""))
                if ident and ident not in called_by:
                    called_by.append(ident)
    base["called_by"] = called_by[:10]
    ajax_contract = _resolve_ajax(functions, "assignToDate")
    if ajax_contract:
        base["ajax"] = True
        base.update(ajax_contract)
    return base


class AssignToDateDiagnosticSessionProxy(InlineDateHandlerDiagnosticSessionProxy):
    """Resolve assignToDate AJAX structure without changing portal requests."""

    async def _diagnose(self, method: str, data: Any, response: Any) -> None:
        await super()._diagnose(method, data, response)
        try:
            body = await response.text()
        except Exception:  # pragma: no cover
            return
        markup = _markup_from_response(body)
        if not markup or "assignToDate" not in markup:
            return
        c = assign_to_date_contract(markup)
        if not c["found"]:
            return
        _LOGGER.warning(
            "LINZ NETZ diagnostic-assign-to-date: step=%s ajax=%s ajax_type=%s ajax_direct=%s ajax_via_function=%s primefaces_keys=%s "
            "source_present=%s source_kind=%s source=%s source_dynamic=%s source_refs=%s source_functions=%s "
            "execute_present=%s execute_kind=%s execute=%s execute_dynamic=%s execute_refs=%s execute_functions=%s "
            "render_present=%s render_kind=%s render=%s render_dynamic=%s render_refs=%s render_functions=%s "
            "event_present=%s event_kind=%s event=%s event_dynamic=%s event_refs=%s event_functions=%s "
            "params_present=%s param_names=%s partial_event=%s reads=%s writes=%s calls=%s copies=%s hidden_fields=%s called_by=%s submit=%s",
            _request_step(method, data), c["ajax"], c["ajax_type"], c["ajax_direct"], c["ajax_via_function"], c["primefaces_keys"],
            c["source_present"], c["source_kind"], c["source"], c["source_dynamic"], c["source_refs"], c["source_functions"],
            c["execute_present"], c["execute_kind"], c["execute"], c["execute_dynamic"], c["execute_refs"], c["execute_functions"],
            c["render_present"], c["render_kind"], c["render"], c["render_dynamic"], c["render_refs"], c["render_functions"],
            c["event_present"], c["event_kind"], c["event"], c["event_dynamic"], c["event_refs"], c["event_functions"],
            c["params_present"], c["param_names"], c["partial_event"], c["reads"], c["writes"], c["calls"], c["copies"], c["hidden_fields"], c["called_by"], c["submit"],
        )
