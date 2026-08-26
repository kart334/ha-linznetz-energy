"""Privacy-safe diagnostics for the LINZ NETZ JSF/PrimeFaces portal.

This module intentionally exposes only structural metadata. It must never return
cookies, credentials, session identifiers, customer data, meter identifiers,
complete ViewState values, or raw HTML/XML bodies.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final

from bs4 import BeautifulSoup, Tag

_VIEW_STATE_RE: Final = re.compile(r"(?:jakarta|javax)\.faces\.ViewState", re.IGNORECASE)
_PARTIAL_UPDATE_RE: Final = re.compile(
    r'<update\s+id=["\']([^"\']+)["\']\s*>\s*<!\[CDATA\[(.*?)\]\]>\s*</update>',
    re.DOTALL,
)
_PRIMEFACES_AJAX_RE: Final = re.compile(
    r"PrimeFaces\.ab\(\s*\{(.*?)\}\s*(?:,|\))", re.DOTALL
)
_DATETIME_RE: Final = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\b")


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


def response_structure(body: str, table_id_pattern: re.Pattern[str]) -> dict[str, object]:
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
        text = row.get_text(" ", strip=True)
        if _DATETIME_RE.search(text):
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
