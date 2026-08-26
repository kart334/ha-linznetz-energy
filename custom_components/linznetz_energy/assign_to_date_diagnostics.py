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
            name = next((g for g in match.groups() if g), None)
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
    blocked = {
        "document.getElementById", "document.getElementsByName", "document.querySelector",
        "PrimeFaces.ab", "jsf.ajax.request", "faces.ajax.request", "$", "jQuery",
    }
    result: list[str] = []
    for match in _CALL_RE.finditer(body):
        name = match.group(1)
        if name in blocked or name.startswith("document."):
            continue
        safe = _safe_name(name)
        if safe and safe not in result:
            result.append(safe)
    return result[:20]


def _split_top_level_args(text: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    pairs = {"(": ")", "{": "}", "[": "]"}
    closers = set(pairs.values())
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
        elif ch in pairs:
            depth += 1
        elif ch in closers:
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
    args.append(text[start:].strip())
    return args


def _literal(expr: str) -> str | None:
    m = re.fullmatch(r"\s*(['\"])(.*?)\1\s*", expr, re.DOTALL)
    return m.group(2) if m else None


def _safe_expr_names(expr: str) -> list[str]:
    names: list[str] = []
    for raw in re.findall(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\b", expr):
        if raw in {"true", "false", "null", "undefined"}:
            continue
        safe = _safe_name(raw)
        if safe and safe not in names:
            names.append(safe)
    return names[:10]


def _pf_options(obj: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in re.findall(
        r"\b(s|source|e|event|p|process|u|update)\s*:\s*([^,}]+)", obj, re.I | re.S
    ):
        canon = {"s": "source", "p": "execute", "process": "execute", "u": "render", "update": "render", "e": "event"}.get(key.lower(), key.lower())
        lit = _literal(value.strip())
        result[canon] = _safe_name(lit) if lit is not None else None
        result[f"{canon}_dynamic"] = lit is None
        if lit is None:
            result[f"{canon}_refs"] = _safe_expr_names(value)
    names: list[str] = []
    for match in re.finditer(r"(?:name|n)\s*:\s*(['\"])(.*?)\1", obj, re.I | re.S):
        safe = _safe_name(match.group(2))
        if safe and safe not in names:
            names.append(safe)
    result["param_names"] = names[:20]
    return result


def _find_direct_ajax(body: str) -> dict[str, object] | None:
    pf = re.search(r"PrimeFaces\.ab\s*\(", body)
    if pf:
        start = body.find("(", pf.start())
        inner, _ = _balanced_segment(body, start, "(", ")")
        if inner is not None:
            args = _split_top_level_args(inner)
            obj = args[0] if args else ""
            contract = {"ajax_type": "PrimeFaces.ab", "ajax_direct": True}
            contract.update(_pf_options(obj))
            contract.setdefault("partial_event", None)
            contract.setdefault("partial_event_dynamic", False)
            return contract
    jsf = re.search(r"(?:jsf|faces)\.ajax\.request\s*\(", body)
    if jsf:
        start = body.find("(", jsf.start())
        inner, _ = _balanced_segment(body, start, "(", ")")
        args = _split_top_level_args(inner or "")
        source_expr = args[0] if len(args) > 0 else ""
        event_expr = args[1] if len(args) > 1 else ""
        opts = args[2] if len(args) > 2 else ""
        source_lit = _literal(source_expr)
        event_lit = _literal(event_expr)
        contract: dict[str, object] = {
            "ajax_type": "jsf.ajax.request" if body[jsf.start():].startswith("jsf") else "faces.ajax.request",
            "ajax_direct": True,
            "source": _safe_name(source_lit) if source_lit is not None else None,
            "source_dynamic": source_lit is None,
            "source_refs": _safe_expr_names(source_expr) if source_lit is None else [],
            "event": _safe_name(event_lit) if event_lit is not None else None,
            "event_dynamic": event_lit is None,
            "event_refs": _safe_expr_names(event_expr) if event_lit is None else [],
            "execute": None, "render": None, "partial_event": None, "param_names": [],
        }
        for jsf_key, canon in (("execute", "execute"), ("render", "render"), ("onevent", "partial_event")):
            m = re.search(rf"\b{jsf_key}\s*:\s*([^,}}]+)", opts, re.I | re.S)
            if m:
                lit = _literal(m.group(1).strip())
                contract[canon] = _safe_name(lit) if lit is not None else None
                contract[f"{canon}_dynamic"] = lit is None
                if lit is None:
                    contract[f"{canon}_refs"] = _safe_expr_names(m.group(1))
        return contract
    return None


def _resolve_ajax(functions: dict[str, str], start: str, max_depth: int = 3) -> tuple[dict[str, object] | None, str | None]:
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
            return direct, name
        if depth == max_depth:
            continue
        for called in _calls(body):
            simple = called.split(".")[-1]
            if simple in functions and simple not in seen:
                queue.append((simple, depth + 1, simple if start == name else (via or name)))
    return None, None


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
        "found": body is not None,
        "ajax": False,
        "ajax_type": None,
        "ajax_direct": False,
        "ajax_via_function": None,
        "source": None,
        "execute": None,
        "render": None,
        "event": None,
        "partial_event": None,
        "param_names": [],
        "reads": [],
        "writes": [],
        "calls": [],
        "copies": [],
        "hidden_fields": [],
        "called_by": [],
    }
    if body is None:
        return base

    reads, writes = _field_accesses(body)
    base["reads"] = reads
    base["writes"] = writes
    base["calls"] = _calls(body)
    base["copies"] = _copy_pairs(body)
    base["submit"] = bool(re.search(r"(?:\.submit\s*\(|\bsubmit\s*\()", body))

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

    ajax_contract, _ = _resolve_ajax(functions, "assignToDate")
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
        contract = assign_to_date_contract(markup)
        if not contract["found"]:
            return
        _LOGGER.warning(
            "LINZ NETZ diagnostic-assign-to-date: step=%s ajax=%s ajax_type=%s ajax_direct=%s ajax_via_function=%s source=%s execute=%s render=%s event=%s partial_event=%s param_names=%s source_dynamic=%s execute_dynamic=%s render_dynamic=%s event_dynamic=%s reads=%s writes=%s calls=%s copies=%s hidden_fields=%s called_by=%s",
            _request_step(method, data),
            contract.get("ajax"), contract.get("ajax_type"), contract.get("ajax_direct"),
            contract.get("ajax_via_function"), contract.get("source"), contract.get("execute"),
            contract.get("render"), contract.get("event"), contract.get("partial_event"),
            contract.get("param_names"), contract.get("source_dynamic", False),
            contract.get("execute_dynamic", False), contract.get("render_dynamic", False),
            contract.get("event_dynamic", False), contract.get("reads"), contract.get("writes"),
            contract.get("calls"), contract.get("copies"), contract.get("hidden_fields"),
            contract.get("called_by"),
        )
