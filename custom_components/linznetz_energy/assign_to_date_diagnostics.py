"""Privacy-safe structural diagnostics for the portal's assignToDate JavaScript.

The productive function body is inspected in memory only. Logs contain field and
function names plus boolean behavior flags, never JavaScript source, field values,
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
_FUNCTION_PATTERNS: Final = (
    re.compile(r"\bfunction\s+assignToDate\s*\([^)]*\)\s*\{", re.IGNORECASE),
    re.compile(r"\b(?:window\.)?assignToDate\s*=\s*function\s*\([^)]*\)\s*\{", re.IGNORECASE),
    re.compile(r"\b(?:const|let|var)\s+assignToDate\s*=\s*\([^)]*\)\s*=>\s*\{", re.IGNORECASE),
)
_GET_BY_ID_RE: Final = re.compile(
    r"document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", re.IGNORECASE
)
_GET_BY_NAME_RE: Final = re.compile(
    r"document\.getElementsByName\(\s*['\"]([^'\"]+)['\"]\s*\)", re.IGNORECASE
)
_FORM_ELEMENT_RE: Final = re.compile(
    r"(?:\.elements\[|\.elements\.)(?:\s*['\"])?([A-Za-z0-9_:@.\-$]+)", re.IGNORECASE
)
_JQUERY_ID_RE: Final = re.compile(r"(?:\$|jQuery)\(\s*['\"]#([^'\"]+)['\"]\s*\)")
_CALL_RE: Final = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")


def _safe_name(value: str | None) -> str | None:
    if not value:
        return None
    value = value[:160]
    if _SENSITIVE_NAME_RE.search(value):
        return "<redacted-field>"
    return value if _SAFE_NAME_RE.fullmatch(value) else "<non-component>"


def _balanced_body(script: str, brace_start: int) -> str | None:
    """Return one JS block body while respecting strings and comments."""
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    i = brace_start
    while i < len(script):
        ch = script[i]
        nxt = script[i + 1] if i + 1 < len(script) else ""
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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return script[brace_start + 1 : i]
        i += 1
    return None


def extract_assign_to_date_body(markup: str) -> tuple[str | None, BeautifulSoup]:
    """Find assignToDate in rendered scripts; never return it to logs."""
    soup = BeautifulSoup(markup, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text or "assignToDate" not in text:
            continue
        for pattern in _FUNCTION_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            brace = text.find("{", match.start(), match.end() + 1)
            if brace >= 0:
                return _balanced_body(text, brace), soup
    return None, soup


def _field_accesses(body: str) -> list[tuple[str, int, int]]:
    accesses: list[tuple[str, int, int]] = []
    for pattern in (_GET_BY_ID_RE, _GET_BY_NAME_RE, _JQUERY_ID_RE):
        for match in pattern.finditer(body):
            name = _safe_name(match.group(1))
            if name:
                accesses.append((name, match.start(), match.end()))
    for match in _FORM_ELEMENT_RE.finditer(body):
        name = _safe_name(match.group(1))
        if name:
            accesses.append((name, match.start(), match.end()))
    return accesses


def _is_write(body: str, end: int) -> bool:
    tail = body[end : end + 120]
    return bool(
        re.match(r"\s*\.value\s*=", tail)
        or re.match(r"\s*\.valueAsDate\s*=", tail)
        or re.match(r"\s*\.val\s*\([^)]", tail)
        or re.match(r"\s*\.setAttribute\s*\(\s*['\"]value['\"]\s*,", tail, re.IGNORECASE)
    )


def _function_calls(body: str) -> list[str]:
    blocked = {
        "document.getElementById",
        "document.getElementsByName",
        "document.querySelector",
        "PrimeFaces.ab",
        "jsf.ajax.request",
        "faces.ajax.request",
        "$",
        "jQuery",
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


def _copy_pairs(body: str) -> list[dict[str, str]]:
    """Detect direct DOM .value copies and return field names only."""
    pattern = re.compile(
        r"document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\.value\s*=\s*"
        r"document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\.value",
        re.IGNORECASE,
    )
    pairs: list[dict[str, str]] = []
    for match in pattern.finditer(body):
        target = _safe_name(match.group(1))
        source = _safe_name(match.group(2))
        if target and source:
            pairs.append({"from": source, "to": target})
    return pairs[:10]


def assign_to_date_contract(markup: str) -> dict[str, object]:
    """Summarize assignToDate semantics without exposing its source or values."""
    body, soup = extract_assign_to_date_body(markup)
    if body is None:
        return {
            "found": False,
            "reads": [],
            "writes": [],
            "calls": [],
            "ajax": False,
            "submit": False,
            "copies": [],
            "hidden_fields": [],
            "called_by": [],
        }

    reads: list[str] = []
    writes: list[str] = []
    for name, _start, end in _field_accesses(body):
        target = writes if _is_write(body, end) else reads
        if name not in target:
            target.append(name)

    # A write may also semantically read the same control via a different access;
    # preserve both lists rather than forcing exclusivity.
    hidden: list[str] = []
    for name in list(dict.fromkeys(reads + writes)):
        if name.startswith("<"):
            continue
        control = soup.find(id=name) or soup.find(attrs={"name": name})
        if control is not None and str(control.get("type") or "").lower() == "hidden":
            hidden.append(name)

    called_by: list[str] = []
    for control in soup.find_all(["input", "select", "textarea", "button"]):
        for attr in ("onchange", "onblur", "onclick", "oninput"):
            handler = str(control.get(attr) or "")
            if "assignToDate" not in handler:
                continue
            ident = _safe_name(str(control.get("id") or control.get("name") or ""))
            if ident and ident not in called_by:
                called_by.append(ident)

    ajax = any(token in body for token in ("PrimeFaces.ab", "jsf.ajax", "faces.ajax"))
    submit = bool(re.search(r"(?:\.submit\s*\(|\bsubmit\s*\()", body))
    return {
        "found": True,
        "reads": reads[:20],
        "writes": writes[:20],
        "calls": _function_calls(body),
        "ajax": ajax,
        "submit": submit,
        "copies": _copy_pairs(body),
        "hidden_fields": hidden[:20],
        "called_by": called_by[:10],
    }


class AssignToDateDiagnosticSessionProxy(InlineDateHandlerDiagnosticSessionProxy):
    """Add assignToDate structure while leaving all portal requests untouched."""

    async def _diagnose(self, method: str, data: Any, response: Any) -> None:
        await super()._diagnose(method, data, response)
        try:
            body = await response.text()
        except Exception:  # pragma: no cover - diagnostics must never break I/O
            return
        markup = _markup_from_response(body)
        if not markup or "assignToDate" not in markup:
            return
        contract = assign_to_date_contract(markup)
        if not contract["found"]:
            return
        _LOGGER.warning(
            "LINZ NETZ diagnostic-assign-to-date: step=%s reads=%s writes=%s calls=%s ajax=%s submit=%s copies=%s hidden_fields=%s called_by=%s",
            _request_step(method, data),
            contract["reads"],
            contract["writes"],
            contract["calls"],
            contract["ajax"],
            contract["submit"],
            contract["copies"],
            contract["hidden_fields"],
            contract["called_by"],
        )
