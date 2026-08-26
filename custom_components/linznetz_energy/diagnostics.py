"""Privacy-safe diagnostics for the LINZ NETZ JSF/PrimeFaces portal.

This module intentionally exposes only structural metadata. It must never log
cookies, credentials, session identifiers, customer data, meter identifiers,
complete ViewState values, or raw HTML/XML bodies.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Final

from bs4 import BeautifulSoup, Tag

_LOGGER = logging.getLogger(__name__)
_VIEW_STATE_RE: Final = re.compile(r"(?:jakarta|javax)\.faces\.ViewState", re.IGNORECASE)
_PARTIAL_UPDATE_RE: Final = re.compile(
    r'<update\s+id=["\']([^"\']+)["\']\s*>\s*<!\[CDATA\[(.*?)\]\]>\s*</update>',
    re.DOTALL,
)
_PRIMEFACES_AJAX_RE: Final = re.compile(
    r"PrimeFaces\.ab\(\s*\{(.*?)\}\s*(?:,|\))", re.DOTALL
)
_DATETIME_RE: Final = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\b")
_TABLE_ID_RE: Final = re.compile(r":consumptionsTable$", re.IGNORECASE)


@dataclass(frozen=True)
class RequestContract:
    """Structural browser request contract discovered from rendered markup."""

    request_type: str
    source: str | None = None
    execute: str | None = None
    render: str | None = None
    event: str | None = None


def _parse_primefaces_options(config: str) -> dict[str, str]:
    options: dict[str, str] = {}
    for match in re.finditer(
        r"\b(s|source|e|event|p|process|u|update|f|form)\s*:\s*"
        r"(?:\"([^\"]*)\"|'([^']*)')",
        config,
        flags=re.IGNORECASE,
    ):
        options[match.group(1).lower()] = match.group(2) or match.group(3) or ""
    return options


def classify_button_contract(button: Tag, form_id: str) -> RequestContract:
    """Classify a rendered JSF/PrimeFaces button without exposing raw JavaScript."""
    onclick = str(button.get("onclick") or "")
    kind = str(button.get("type") or "").lower()

    for match in _PRIMEFACES_AJAX_RE.finditer(onclick):
        options = _parse_primefaces_options(match.group(1))
        return RequestContract(
            request_type="partial_ajax",
            source=options.get("s") or options.get("source") or str(button.get("id") or "") or None,
            execute=options.get("p") or options.get("process"),
            render=options.get("u") or options.get("update"),
            event=options.get("e") or options.get("event") or "action",
        )

    low = onclick.lower()
    if "jsf.ajax.request" in low or "mojarra.ab(" in low:
        return RequestContract(request_type="partial_ajax")
    if "jsfcljs" in low or ".submit(" in low or "form.submit" in low:
        return RequestContract(request_type="full_post")
    if button.name in {"button", "input"} and kind in {"submit", ""}:
        return RequestContract(request_type="full_post")
    if button.name == "a" and form_id and form_id in onclick and "submit" in low:
        return RequestContract(request_type="full_post")
    return RequestContract(request_type="unknown")


def find_component_ajax_contract(soup: BeautifulSoup, component_ids: set[str]) -> RequestContract | None:
    """Find a rendered PrimeFaces AJAX contract for one of the component IDs."""
    if not component_ids:
        return None
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text or not any(component_id in text for component_id in component_ids):
            continue
        for match in _PRIMEFACES_AJAX_RE.finditer(text):
            options = _parse_primefaces_options(match.group(1))
            source = options.get("s") or options.get("source")
            if source not in component_ids:
                continue
            return RequestContract(
                request_type="partial_ajax",
                source=source,
                execute=options.get("p") or options.get("process") or source,
                render=options.get("u") or options.get("update"),
                event=options.get("e") or options.get("event"),
            )
    return None


def safe_partial_update_ids(body: str) -> list[str]:
    """Return only structural update IDs, never update content or ViewState values."""
    result: list[str] = []
    for match in _PARTIAL_UPDATE_RE.finditer(body):
        identifier = match.group(1)
        if _VIEW_STATE_RE.search(identifier):
            result.append("<ViewState>")
        else:
            result.append(identifier[:160])
    return result[:30]


def response_type(body: str) -> str:
    """Classify the response body structurally."""
    return "partial_response" if "<partial-response" in body else "html"


def response_structure(body: str, table_id_pattern: re.Pattern[str] = _TABLE_ID_RE) -> dict[str, object]:
    """Return privacy-safe structural response facts."""
    partial = response_type(body) == "partial_response"
    fragments = [
        match.group(2)
        for match in _PARTIAL_UPDATE_RE.finditer(body)
        if not _VIEW_STATE_RE.search(match.group(1))
    ]
    soup = BeautifulSoup("\n".join(fragments) if partial and fragments else body, "html.parser")
    table = soup.find(id=table_id_pattern)
    table_id = str(table.get("id")) if table is not None and table.get("id") else None
    if table_id is None:
        match = re.search(r'id=["\']([^"\']+:consumptionsTable)["\']', body, re.IGNORECASE)
        table_id = match.group(1) if match else None
    row_count = len(table.find_all("tr")) if table is not None else 0
    candidate_value_rows = 0
    for row in soup.find_all("tr"):
        if _DATETIME_RE.search(row.get_text(" ", strip=True)):
            candidate_value_rows += 1
    return {
        "response_type": "partial_response" if partial else "html",
        "update_count": len(_PARTIAL_UPDATE_RE.findall(body)),
        "update_ids": safe_partial_update_ids(body),
        "table_found": table_id is not None,
        "table_id": table_id,
        "table_rows": row_count,
        "candidate_value_rows": candidate_value_rows,
        "paginator_found": "paginator" in body.lower() or "rowCount" in body,
    }


def date_control_candidates(form: Tag, base_suffix: str) -> list[dict[str, str]]:
    """Describe only date-related input controls and their date-shaped values."""
    found: list[dict[str, str]] = []
    base_re = re.compile(re.escape(base_suffix), re.IGNORECASE)
    for tag in form.find_all("input"):
        name = str(tag.get("name") or "")
        identifier = str(tag.get("id") or "")
        if not base_re.search(name) and not base_re.search(identifier):
            continue
        value = str(tag.get("value") or "").strip()
        safe_value = value if re.fullmatch(r"\d{2}\.\d{2}\.\d{2,4}|\d{4}-\d{2}-\d{2}", value) else "<non-date>" if value else ""
        found.append(
            {
                "id": identifier[:160],
                "name": name[:160],
                "type": str(tag.get("type") or "text")[:32],
                "value": safe_value,
            }
        )
    return found[:10]


def _find_consumption_form(soup: BeautifulSoup) -> Tag | None:
    """Find a likely consumption form using only structural markers."""
    for form in soup.find_all("form"):
        markup = str(form)
        if "calendarFromRegion" in markup and "calendarToRegion" in markup:
            return form
    return None


def _find_display_button(form: Tag) -> Tag | None:
    for tag in form.find_all(["button", "input", "a"]):
        label = tag.get_text(" ", strip=True) + " " + str(tag.get("value") or "")
        if "ANZEIGEN" in label.upper():
            return tag
    return form.find(id=re.compile(r"btnIdA1$", re.IGNORECASE))


def _selected_class_components(form: Tag) -> list[dict[str, str | None]]:
    """Return only known selection component structure, never arbitrary hidden fields."""
    result: list[dict[str, str | None]] = []
    for tag in form.find_all(["input", "select"]):
        name = str(tag.get("name") or "")
        if not name.endswith(":selectedClass"):
            continue
        value = str(tag.get("value") or "")
        if value not in {"ConsumQuarter", "KWH"}:
            continue
        result.append(
            {
                "value": value,
                "name": name[:160],
                "component": name[: -len(":selectedClass")][:160],
            }
        )
    return result


def _safe_payload_shape(data: Any) -> dict[str, object]:
    if not isinstance(data, dict):
        return {"field_count": None}
    keys = [str(key) for key in data]
    values = {str(value) for value in data.values() if isinstance(value, str)}
    return {
        "field_count": len(keys),
        "viewstate_present": any(_VIEW_STATE_RE.search(key) for key in keys),
        "period_range_present": any(key.lower().endswith("periodrange") for key in keys),
        "date_from_present": any("calendarFromRegion" in key for key in keys),
        "date_to_present": any("calendarToRegion" in key for key in keys),
        "consumquarter_present": "ConsumQuarter" in values,
        "kwh_present": "KWH" in values,
        "pagination_present": any(key.endswith("_pagination") for key in keys),
    }


def _request_step(method: str, data: Any) -> str:
    if method == "GET":
        return "get"
    if not isinstance(data, dict):
        return "post"
    if any(str(key).endswith("_pagination") for key in data):
        return "pagination"
    event = str(data.get("jakarta.faces.behavior.event") or data.get("javax.faces.behavior.event") or "")
    source = str(data.get("jakarta.faces.source") or data.get("javax.faces.source") or "")
    values = {str(value) for value in data.values() if isinstance(value, str)}
    if event.lower() in {"dateselect", "change"} and "calendar" in source.lower():
        return "date"
    if event.lower() == "valuechange" or (event.lower() == "change" and "ConsumQuarter" in values):
        return "consumquarter"
    if event.lower() == "change" and "KWH" in values:
        return "kwh"
    if event.lower() == "action" or str(data.get("jakarta.faces.partial.event") or "").lower() == "click":
        return "display"
    return "post"


def _log_markup_contract(body: str) -> None:
    """Log the rendered browser contract without raw markup or sensitive values."""
    soup = BeautifulSoup(body, "html.parser")
    form = _find_consumption_form(soup)
    if form is None:
        return
    form_id = str(form.get("id") or form.get("name") or "")[:160]
    button = _find_display_button(form)
    if button is not None:
        contract = classify_button_contract(button, form_id)
        _LOGGER.warning(
            "LINZ NETZ diagnostic: markup=display form=%s button_id=%s tag=%s type=%s browser_request_type=%s source=%s execute=%s render=%s event=%s",
            form_id,
            str(button.get("id") or "")[:160],
            button.name,
            str(button.get("type") or "")[:32],
            contract.request_type,
            contract.source,
            contract.execute,
            contract.render,
            contract.event,
        )
    _LOGGER.warning(
        "LINZ NETZ diagnostic: markup=date form=%s from_controls=%s to_controls=%s",
        form_id,
        date_control_candidates(form, "calendarFromRegion"),
        date_control_candidates(form, "calendarToRegion"),
    )
    for selected in _selected_class_components(form):
        component = str(selected["component"] or "")
        contract = find_component_ajax_contract(soup, {component})
        _LOGGER.warning(
            "LINZ NETZ diagnostic: markup=choice value=%s component=%s browser_request_type=%s source=%s execute=%s render=%s event=%s",
            selected["value"],
            component,
            contract.request_type if contract else "none_detected",
            contract.source if contract else None,
            contract.execute if contract else None,
            contract.render if contract else None,
            contract.event if contract else None,
        )


class DiagnosticSessionProxy:
    """Transparent aiohttp session proxy that logs only privacy-safe request structure."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._sequence = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._session.get(*args, **kwargs)
        await self._diagnose("GET", None, response)
        return response

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        data = kwargs.get("data")
        response = await self._session.post(*args, **kwargs)
        await self._diagnose("POST", data, response)
        return response

    async def _diagnose(self, method: str, data: Any, response: Any) -> None:
        self._sequence += 1
        step = _request_step(method, data)
        try:
            body = await response.text()
        except Exception:  # pragma: no cover - diagnostics must never break portal traffic
            _LOGGER.warning(
                "LINZ NETZ diagnostic: seq=%s step=%s method=%s status=%s response_text_unavailable=true",
                self._sequence,
                step,
                method,
                getattr(response, "status", None),
            )
            return

        structure = response_structure(body)
        source = None
        execute = None
        render = None
        behavior = None
        partial_event = None
        request_type = "full_get" if method == "GET" else "full_post"
        if isinstance(data, dict):
            source = data.get("jakarta.faces.source") or data.get("javax.faces.source")
            execute = data.get("jakarta.faces.partial.execute") or data.get("javax.faces.partial.execute")
            render = data.get("jakarta.faces.partial.render") or data.get("javax.faces.partial.render")
            behavior = data.get("jakarta.faces.behavior.event") or data.get("javax.faces.behavior.event")
            partial_event = data.get("jakarta.faces.partial.event") or data.get("javax.faces.partial.event")
            if str(data.get("jakarta.faces.partial.ajax") or data.get("javax.faces.partial.ajax") or "").lower() == "true":
                request_type = "partial_ajax"
        shape = _safe_payload_shape(data)
        _LOGGER.warning(
            "LINZ NETZ diagnostic: seq=%s step=%s request_type=%s source=%s execute=%s render=%s behavior_event=%s partial_event=%s fields=%s http_status=%s response_type=%s updates=%s update_ids=%s table_found=%s table_id=%s table_rows=%s candidate_value_rows=%s paginator=%s",
            self._sequence,
            step,
            request_type,
            str(source)[:160] if source is not None else None,
            str(execute)[:160] if execute is not None else None,
            str(render)[:160] if render is not None else None,
            str(behavior)[:80] if behavior is not None else None,
            str(partial_event)[:80] if partial_event is not None else None,
            shape,
            getattr(response, "status", None),
            structure["response_type"],
            structure["update_count"],
            structure["update_ids"],
            structure["table_found"],
            structure["table_id"],
            structure["table_rows"],
            structure["candidate_value_rows"],
            structure["paginator_found"],
        )
        if structure["response_type"] == "html":
            _log_markup_contract(body)
        elif structure["update_count"]:
            fragments = [
                match.group(2)
                for match in _PARTIAL_UPDATE_RE.finditer(body)
                if not _VIEW_STATE_RE.search(match.group(1))
            ]
            if fragments:
                _log_markup_contract("\n".join(fragments))
