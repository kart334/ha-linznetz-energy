"""Privacy-safe diagnostics for inline calendar event handlers.

This module extracts only structural request metadata from rendered inline
handlers. It never logs handler bodies, parameter values, ViewState values,
cookies, credentials, session IDs, or customer/meter data.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Final

from bs4 import BeautifulSoup, Tag

from .date_contract_diagnostics import (
    DateContractDiagnosticSessionProxy,
    _markup_from_response,
    _request_step,
)

_LOGGER = logging.getLogger(__name__)

_PRIMEFACES_AB_RE: Final = re.compile(
    r"PrimeFaces\.ab\(\s*\{(.*?)\}\s*(?:,|\))", re.DOTALL
)
_OPTION_RE: Final = re.compile(
    r"\b(s|source|e|event|p|process|u|update|f|form)\s*:\s*"
    r"(?:\"([^\"]*)\"|'([^']*)')",
    re.IGNORECASE,
)
_PARAM_NAME_RE: Final = re.compile(
    r"(?:name|n)\s*:\s*(?:\"([^\"]+)\"|'([^']+)')",
    re.IGNORECASE,
)
_ASSIGNMENT_RE: Final = re.compile(
    r"(?:document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)|"
    r"(?:PrimeFaces\.escapeClientId\()?['\"]#?([^'\"]*(?:calendarFromRegion|calendarToRegion)[^'\"]*)['\"]\)?|"
    r"\$\(\s*['\"]#?([^'\"]*(?:calendarFromRegion|calendarToRegion)[^'\"]*)['\"]\s*\))"
    r"[^;]{0,200}?\.value\s*=",
    re.IGNORECASE,
)
_FUNCTION_CALL_RE: Final = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_SAFE_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_:@.\-$]+$")


def _safe_name(value: str | None) -> str | None:
    if not value:
        return None
    value = value[:160]
    return value if _SAFE_NAME_RE.fullmatch(value) else "<non-component>"


def _parse_options(config: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in _OPTION_RE.finditer(config):
        result[match.group(1).lower()] = match.group(2) or match.group(3) or ""
    return result


def _param_names(config: str) -> list[str]:
    """Extract parameter names only, never parameter values."""
    names: list[str] = []
    for match in _PARAM_NAME_RE.finditer(config):
        name = _safe_name(match.group(1) or match.group(2))
        if name and name not in names:
            names.append(name)
    return names[:20]


def _pre_ajax_assignments(handler: str, ajax_start: int) -> list[str]:
    """Extract only date-field names assigned before PrimeFaces.ab."""
    before = handler[:ajax_start]
    fields: list[str] = []
    for match in _ASSIGNMENT_RE.finditer(before):
        raw = next((group for group in match.groups() if group), None)
        safe = _safe_name(raw)
        if safe and safe not in fields:
            fields.append(safe)
    return fields[:10]


def _safe_function_calls(handler: str) -> list[str]:
    """Return only non-framework function names; never source text or args."""
    result: list[str] = []
    blocked = {
        "PrimeFaces.ab",
        "document.getElementById",
        "$",
        "jQuery",
    }
    for match in _FUNCTION_CALL_RE.finditer(handler):
        name = match.group(1)
        if name in blocked or name.startswith("PrimeFaces."):
            continue
        safe = _safe_name(name)
        if safe and safe not in result:
            result.append(safe)
    return result[:10]


def inline_handler_contract(control: Tag, event_attr: str = "onchange") -> dict[str, object]:
    """Extract structural PrimeFaces contract from one inline handler."""
    handler = str(control.get(event_attr) or "")
    identifier = str(control.get("id") or control.get("name") or "")
    base = {
        "calendar": _safe_name(identifier),
        "event_attr": event_attr,
        "present": bool(handler),
        "primefaces_ajax": "PrimeFaces.ab" in handler,
        "source": None,
        "execute": None,
        "render": None,
        "event": None,
        "partial_event": None,
        "related_controls": [],
        "param_names": [],
        "pre_ajax_assignments": [],
        "function_calls": _safe_function_calls(handler) if handler else [],
    }
    if not handler:
        return base

    match = _PRIMEFACES_AB_RE.search(handler)
    if match is None:
        related = [
            name
            for name in ("calendarFromRegion", "calendarToRegion")
            if name in handler
        ]
        base["related_controls"] = related
        return base

    options = _parse_options(match.group(1))
    source = options.get("s") or options.get("source")
    execute = options.get("p") or options.get("process")
    render = options.get("u") or options.get("update")
    event = options.get("e") or options.get("event")

    related: list[str] = []
    structural_text = " ".join(
        value for value in (source, execute, render, match.group(1)) if value
    )
    for name in ("calendarFromRegion", "calendarToRegion"):
        if name in structural_text or name in handler[: match.start()]:
            related.append(name)

    base.update(
        {
            "source": _safe_name(source),
            "execute": _safe_name(execute),
            "render": _safe_name(render),
            "event": (event or "")[:40] or None,
            # PrimeFaces inline handlers may not spell out javax.faces.partial.event;
            # record only an explicitly discoverable value rather than guessing.
            "partial_event": None,
            "related_controls": related,
            "param_names": _param_names(match.group(1)),
            "pre_ajax_assignments": _pre_ajax_assignments(handler, match.start()),
        }
    )
    return base


def calendar_inline_contracts(markup: str) -> list[dict[str, object]]:
    """Extract direct From/To onchange structure from rendered markup."""
    soup = BeautifulSoup(markup, "html.parser")
    result: list[dict[str, object]] = []
    for suffix in ("calendarFromRegion", "calendarToRegion"):
        for control in soup.find_all("input"):
            identifier = str(control.get("id") or "")
            name = str(control.get("name") or "")
            if suffix.lower() not in identifier.lower() and suffix.lower() not in name.lower():
                continue
            result.append(inline_handler_contract(control, "onchange"))
            break
    return result


class InlineDateHandlerDiagnosticSessionProxy(DateContractDiagnosticSessionProxy):
    """Add exact inline onchange structure without changing portal requests."""

    async def _diagnose(self, method: str, data: Any, response: Any) -> None:
        await super()._diagnose(method, data, response)
        try:
            body = await response.text()
        except Exception:  # pragma: no cover - diagnostic only
            return
        step = _request_step(method, data)
        if step not in {"get", "consumquarter", "post"}:
            return
        markup = _markup_from_response(body)
        if not markup:
            return
        for contract in calendar_inline_contracts(markup):
            _LOGGER.warning(
                "LINZ NETZ diagnostic-date-contract: step=%s calendar=%s event_attr=%s present=%s primefaces_ajax=%s source=%s execute=%s render=%s event=%s partial_event=%s related_controls=%s param_names=%s pre_ajax_assignments=%s function_calls=%s",
                step,
                contract["calendar"],
                contract["event_attr"],
                contract["present"],
                contract["primefaces_ajax"],
                contract["source"],
                contract["execute"],
                contract["render"],
                contract["event"],
                contract["partial_event"],
                contract["related_controls"],
                contract["param_names"],
                contract["pre_ajax_assignments"],
                contract["function_calls"],
            )
