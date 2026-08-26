"""Extended privacy-safe diagnostics for calendar/date browser contracts.

This module is intentionally diagnostic-only. It does not alter portal requests.
Only structural metadata is logged; raw JavaScript/HTML, ViewState values,
credentials, cookies, session IDs and customer/meter data are never logged.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Final

from bs4 import BeautifulSoup, Tag

from .diagnostics import DiagnosticSessionProxy

_LOGGER = logging.getLogger(__name__)
_VIEW_STATE_RE: Final = re.compile(r"(?:jakarta|javax)\.faces\.ViewState", re.IGNORECASE)
_PARTIAL_UPDATE_RE: Final = re.compile(
    r'<update\s+id=["\']([^"\']+)["\']\s*>\s*<!\[CDATA\[(.*?)\]\]>\s*</update>',
    re.DOTALL,
)
_PRIMEFACES_AJAX_RE: Final = re.compile(
    r"PrimeFaces\.ab\(\s*\{(.*?)\}\s*(?:,|\))", re.DOTALL
)
_PRIMEFACES_CW_RE: Final = re.compile(
    r"PrimeFaces\.cw\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*\{(.*?)\}\s*\)",
    re.DOTALL,
)
_DATE_VALUE_RE: Final = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_SAFE_COMPONENT_RE: Final = re.compile(r"^[A-Za-z0-9_:\-.@]+$")


def _safe_component(value: str | None) -> str | None:
    """Return only component-like identifiers, never arbitrary script values."""
    if not value:
        return None
    value = value[:160]
    return value if _SAFE_COMPONENT_RE.fullmatch(value) else "<non-component>"


def _parse_pf_options(config: str) -> dict[str, str]:
    """Parse only structural PrimeFaces AJAX option strings."""
    options: dict[str, str] = {}
    for match in re.finditer(
        r"\b(s|source|e|event|p|process|u|update|f|form)\s*:\s*"
        r"(?:\"([^\"]*)\"|'([^']*)')",
        config,
        flags=re.IGNORECASE,
    ):
        options[match.group(1).lower()] = match.group(2) or match.group(3) or ""
    return options


def _safe_ajax_contracts(script: str, anchors: set[str]) -> list[dict[str, str | None]]:
    """Return AJAX contracts from scripts that reference a relevant component."""
    if not anchors or not any(anchor in script for anchor in anchors):
        return []
    contracts: list[dict[str, str | None]] = []
    for match in _PRIMEFACES_AJAX_RE.finditer(script):
        options = _parse_pf_options(match.group(1))
        source = options.get("s") or options.get("source")
        if not source:
            continue
        # Exact ID, child ID, parent/wrapper relation, or a script anchored by the
        # relevant component. This intentionally broadens 0.1.12 diagnostics,
        # which required exact source equality and could miss generated child IDs.
        related = any(
            source == anchor
            or source.startswith(f"{anchor}:")
            or anchor.startswith(f"{source}:")
            for anchor in anchors
        )
        if not related and not any(anchor in match.group(0) for anchor in anchors):
            continue
        contracts.append(
            {
                "source": _safe_component(source),
                "execute": _safe_component(options.get("p") or options.get("process")),
                "render": _safe_component(options.get("u") or options.get("update")),
                "event": (options.get("e") or options.get("event") or "")[:40] or None,
            }
        )
    return contracts[:10]


def _safe_widget_configs(script: str, anchors: set[str]) -> list[dict[str, object]]:
    """Return PrimeFaces widget metadata without raw widget configuration."""
    if not anchors or not any(anchor in script for anchor in anchors):
        return []
    widgets: list[dict[str, object]] = []
    for match in _PRIMEFACES_CW_RE.finditer(script):
        config = match.group(3)
        if not any(anchor in config or anchor in match.group(0) for anchor in anchors):
            continue
        tokens = {
            token: bool(re.search(token, config, re.IGNORECASE))
            for token in ("dateSelect", "change", "onchange", "onblur", "oninput", "behaviors")
        }
        widget_id = None
        id_match = re.search(
            r"\bid\s*:\s*(?:\"([^\"]*)\"|'([^']*)')", config, re.IGNORECASE
        )
        if id_match:
            widget_id = id_match.group(1) or id_match.group(2)
        widgets.append(
            {
                "widget_type": match.group(1)[:80],
                "widget_var": _safe_component(match.group(2)),
                "component_id": _safe_component(widget_id),
                "tokens": tokens,
            }
        )
    return widgets[:10]


def _input_handlers(control: Tag) -> dict[str, object]:
    """Describe event attributes structurally without logging handler bodies."""
    handlers: dict[str, object] = {}
    for name in ("onchange", "onblur", "oninput", "onclick", "onselect"):
        raw = str(control.get(name) or "")
        if not raw:
            continue
        handlers[name] = {
            "present": True,
            "primefaces_ajax": "PrimeFaces.ab" in raw,
            "jsf_ajax": "jsf.ajax" in raw or "mojarra.ab" in raw,
            "submit": ".submit(" in raw or "form.submit" in raw or "jsfcljs" in raw,
        }
    return handlers


def _calendar_controls(soup: BeautifulSoup, suffix: str) -> list[Tag]:
    result: list[Tag] = []
    for control in soup.find_all("input"):
        identifier = str(control.get("id") or "")
        name = str(control.get("name") or "")
        if suffix.lower() in identifier.lower() or suffix.lower() in name.lower():
            result.append(control)
    return result[:10]


def calendar_contract_summary(markup: str, suffix: str) -> dict[str, object]:
    """Summarize where a calendar's server-side browser contract is rendered."""
    soup = BeautifulSoup(markup, "html.parser")
    controls = _calendar_controls(soup, suffix)
    anchors: set[str] = set()
    control_summary: list[dict[str, object]] = []
    for control in controls:
        identifier = str(control.get("id") or "")
        name = str(control.get("name") or "")
        anchors.update(value for value in (identifier, name) if value)
        value = str(control.get("value") or "").strip()
        control_summary.append(
            {
                "id": _safe_component(identifier),
                "name": _safe_component(name),
                "type": str(control.get("type") or "text")[:32],
                "format_ddMMyyyy": bool(_DATE_VALUE_RE.fullmatch(value)),
                "handlers": _input_handlers(control),
            }
        )

    scripts_ref = 0
    ajax_contracts: list[dict[str, str | None]] = []
    widget_configs: list[dict[str, object]] = []
    script_tokens = {
        "primefaces_ab": False,
        "primefaces_cw": False,
        "dateSelect": False,
        "change": False,
        "onchange": False,
        "onblur": False,
        "behaviors": False,
    }
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text or not anchors or not any(anchor in text for anchor in anchors):
            continue
        scripts_ref += 1
        ajax_contracts.extend(_safe_ajax_contracts(text, anchors))
        widget_configs.extend(_safe_widget_configs(text, anchors))
        script_tokens["primefaces_ab"] |= "PrimeFaces.ab" in text
        script_tokens["primefaces_cw"] |= "PrimeFaces.cw" in text
        script_tokens["dateSelect"] |= bool(re.search(r"dateSelect", text, re.IGNORECASE))
        script_tokens["change"] |= bool(re.search(r"\bchange\b", text, re.IGNORECASE))
        script_tokens["onchange"] |= bool(re.search(r"onchange", text, re.IGNORECASE))
        script_tokens["onblur"] |= bool(re.search(r"onblur", text, re.IGNORECASE))
        script_tokens["behaviors"] |= bool(re.search(r"behaviors", text, re.IGNORECASE))

    inline_event = any(bool(item["handlers"]) for item in control_summary)
    detected = inline_event or bool(ajax_contracts) or bool(widget_configs)
    return {
        "suffix": suffix,
        "controls": control_summary,
        "script_refs": scripts_ref,
        "ajax_contracts": ajax_contracts[:10],
        "widget_configs": widget_configs[:10],
        "script_tokens": script_tokens,
        "server_contract_detected": detected,
    }


def choice_contract_summary(markup: str, value: str) -> dict[str, object]:
    """Broaden choice diagnostics to generated child IDs in surrounding scripts."""
    soup = BeautifulSoup(markup, "html.parser")
    anchors: set[str] = set()
    for control in soup.find_all(["input", "select"]):
        name = str(control.get("name") or "")
        current_value = str(control.get("value") or "")
        if current_value != value or not name.endswith(":selectedClass"):
            continue
        component = name[: -len(":selectedClass")]
        anchors.add(component)
        # PrimeFaces often emits a generated clickable child, e.g. :grid_eval.
        anchors.add(f"{component}:grid_eval")
    contracts: list[dict[str, str | None]] = []
    scripts_ref = 0
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text or not anchors or not any(anchor in text for anchor in anchors):
            continue
        scripts_ref += 1
        contracts.extend(_safe_ajax_contracts(text, anchors))
    return {
        "value": value,
        "anchors": sorted(_safe_component(anchor) for anchor in anchors if _safe_component(anchor))[:10],
        "script_refs": scripts_ref,
        "ajax_contracts": contracts[:10],
        "server_contract_detected": bool(contracts),
    }


def _markup_from_response(body: str) -> str:
    """Extract non-ViewState partial update markup, or return full HTML."""
    if "<partial-response" not in body:
        return body
    parts = [
        match.group(2)
        for match in _PARTIAL_UPDATE_RE.finditer(body)
        if not _VIEW_STATE_RE.search(match.group(1))
    ]
    return "\n".join(parts)


def _request_step(method: str, data: Any) -> str:
    if method == "GET":
        return "get"
    if not isinstance(data, dict):
        return "post"
    if any(str(key).endswith("_pagination") for key in data):
        return "pagination"
    event = str(data.get("jakarta.faces.behavior.event") or data.get("javax.faces.behavior.event") or "")
    source = str(data.get("jakarta.faces.source") or data.get("javax.faces.source") or "")
    values = {str(item) for item in data.values() if isinstance(item, str)}
    if event.lower() in {"dateselect", "change"} and "calendar" in source.lower():
        return "date"
    if event.lower() == "valuechange" or (event.lower() == "change" and "ConsumQuarter" in values):
        return "consumquarter"
    if event.lower() == "change" and "KWH" in values:
        return "kwh"
    if event.lower() == "action" or str(data.get("jakarta.faces.partial.event") or "").lower() == "click":
        return "display"
    return "post"


class DateContractDiagnosticSessionProxy(DiagnosticSessionProxy):
    """Add calendar/widget contract diagnostics without modifying requests."""

    async def _diagnose(self, method: str, data: Any, response: Any) -> None:
        await super()._diagnose(method, data, response)
        try:
            body = await response.text()
        except Exception:  # pragma: no cover - diagnostic only
            return
        markup = _markup_from_response(body)
        if not markup:
            return
        step = _request_step(method, data)
        # The most important point is the form re-render returned by ConsumQuarter;
        # log all structural contracts there, plus initial GET for comparison.
        if step not in {"get", "consumquarter", "post"}:
            return
        for suffix in ("calendarFromRegion", "calendarToRegion"):
            summary = calendar_contract_summary(markup, suffix)
            if summary["controls"]:
                _LOGGER.warning(
                    "LINZ NETZ diagnostic-date: step=%s calendar=%s controls=%s script_refs=%s ajax_contracts=%s widget_configs=%s script_tokens=%s server_contract_detected=%s",
                    step,
                    suffix,
                    summary["controls"],
                    summary["script_refs"],
                    summary["ajax_contracts"],
                    summary["widget_configs"],
                    summary["script_tokens"],
                    summary["server_contract_detected"],
                )
        for value in ("ConsumQuarter", "KWH"):
            summary = choice_contract_summary(markup, value)
            if summary["anchors"]:
                _LOGGER.warning(
                    "LINZ NETZ diagnostic-choice: step=%s value=%s anchors=%s script_refs=%s ajax_contracts=%s server_contract_detected=%s",
                    step,
                    value,
                    summary["anchors"],
                    summary["script_refs"],
                    summary["ajax_contracts"],
                    summary["server_contract_detected"],
                )
