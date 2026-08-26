"""Client for the LINZ NETZ customer portal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import logging
import math
import re
from typing import Final
from urllib.parse import urljoin

from aiohttp import ClientResponseError, ClientSession
from bs4 import BeautifulSoup, Tag

from .const import PORTAL_URL

_LOGGER = logging.getLogger(__name__)
_DATE_RE: Final = re.compile(r"\b(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})\b")
_NUMBER_RE: Final = re.compile(r"-?\d+(?:[.,]\d+)?")
_VIEW_STATE_RE: Final = re.compile(r"(jakarta|javax)\.faces\.ViewState$")
_FROM_RE: Final = re.compile(r"calendarFromRegion$", re.IGNORECASE)
_TO_RE: Final = re.compile(r"calendarToRegion$", re.IGNORECASE)
_SELECTED_CLASS_RE: Final = re.compile(r":selectedClass$", re.IGNORECASE)
_QUARTER_TEXT_RE: Final = re.compile(r"viertelstunden|quarter", re.IGNORECASE)
_KWH_TEXT_RE: Final = re.compile(r"\bkwh\b|energiemenge", re.IGNORECASE)
_TABLE_ID_RE: Final = re.compile(r":consumptionsTable$", re.IGNORECASE)
_DATE_INPUT_SUFFIX_RE: Final = re.compile(r"_input$", re.IGNORECASE)
_PRIMEFACES_AJAX_RE: Final = re.compile(
    r"PrimeFaces\.ab\(\s*\{(.*?)\}\s*(?:,|\))", re.DOTALL
)
_PARTIAL_UPDATE_RE: Final = re.compile(
    r'<update\s+id=["\']([^"\']+)["\']\s*>\s*<!\[CDATA\[(.*?)\]\]>\s*</update>',
    re.DOTALL,
)


class LinzNetzError(Exception):
    """Base LINZ NETZ error."""


class LinzNetzAuthError(LinzNetzError):
    """Authentication failed."""


class LinzNetzParseError(LinzNetzError):
    """Portal response could not be parsed."""


@dataclass(frozen=True)
class QuarterReading:
    start_local: datetime
    kwh: float


@dataclass(frozen=True)
class ChoiceField:
    name: str
    value: str


@dataclass(frozen=True)
class PaginationInfo:
    table_id: str
    rows: int
    row_count: int


@dataclass(frozen=True)
class AjaxBehavior:
    """PrimeFaces AJAX behavior discovered from rendered widget JavaScript."""

    source: str
    execute: str
    render: str
    event: str


class LinzNetzClient:
    """Client around the LINZ NETZ JSF/PrimeFaces customer portal."""

    def __init__(self, session: ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password

    async def async_login(self) -> None:
        response = await self._session.get(PORTAL_URL, allow_redirects=True)
        response.raise_for_status()
        html = await response.text()
        if not self._contains_password_form(html):
            return
        soup = BeautifulSoup(html, "html.parser")
        form = next(
            (
                f
                for f in soup.find_all("form")
                if f.find("input", attrs={"type": "password"}) is not None
            ),
            None,
        )
        if form is None:
            raise LinzNetzParseError("SSO-Anmeldeformular nicht gefunden")
        action = form.get("action")
        if not action:
            raise LinzNetzParseError("SSO-Anmeldeziel nicht gefunden")
        payload: dict[str, str] = {}
        for tag in form.find_all("input"):
            name = tag.get("name")
            if not name:
                continue
            kind = (tag.get("type") or "").lower()
            if kind == "password":
                payload[name] = self._password
            elif kind in {"text", "email"} and any(
                x in name.lower() for x in ("user", "mail", "login")
            ):
                payload[name] = self._username
            elif kind == "hidden":
                payload[name] = tag.get("value", "")
        payload.setdefault("username", self._username)
        payload.setdefault("password", self._password)
        try:
            result = await self._session.post(
                urljoin(str(response.url), action),
                data=payload,
                allow_redirects=True,
            )
            result.raise_for_status()
        except ClientResponseError as err:
            raise LinzNetzAuthError(f"SSO HTTP-Fehler: {err.status}") from err
        if self._contains_password_form(await result.text()):
            raise LinzNetzAuthError("Anmeldung bei LINZ NETZ abgelehnt")
        verify = await self._session.get(PORTAL_URL, allow_redirects=True)
        verify.raise_for_status()
        if self._contains_password_form(await verify.text()):
            raise LinzNetzAuthError("SSO-Sitzung wurde nicht übernommen")

    async def async_fetch_quarter_readings(self, day: date) -> list[QuarterReading]:
        response = await self._session.get(PORTAL_URL, allow_redirects=True)
        response.raise_for_status()
        html = await response.text()
        if self._contains_password_form(html):
            await self.async_login()
            response = await self._session.get(PORTAL_URL, allow_redirects=True)
            response.raise_for_status()
            html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        form = self._find_consumption_form(soup)
        if form is None:
            self._log_safe_form_diagnostics(soup, None)
            raise LinzNetzParseError("Verbrauchsformular nicht gefunden")
        form_id = str(form.get("id") or form.get("name") or "")
        if not form_id:
            raise LinzNetzParseError("Formular-ID nicht gefunden")
        view_state = form.find("input", attrs={"name": _VIEW_STATE_RE}) or soup.find(
            "input", attrs={"name": _VIEW_STATE_RE}
        )
        if view_state is None or not view_state.get("name"):
            raise LinzNetzParseError("JSF ViewState nicht gefunden")
        view_state_name = str(view_state.get("name"))
        view_state_value = str(view_state.get("value", ""))

        quarter = self._find_choice_field(form, "ConsumQuarter", _QUARTER_TEXT_RE)
        if quarter is None:
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Auswahl 'Viertelstundenwerte' nicht gefunden")

        working_form = form
        behavior_soups = [soup]
        kwh = self._find_choice_field(form, "KWH", _KWH_TEXT_RE)
        if kwh is None:
            stage = await self._async_apply_choice(
                form_id, quarter, view_state_name, view_state_value
            )
            view_state_value = self._extract_partial_view_state(stage) or view_state_value
            working_form = self._merge_partial_response_form(
                working_form, form_id, stage
            )
            stage_soup = self._partial_response_soup(stage)
            behavior_soups.insert(0, stage_soup)
            kwh = self._find_choice_field(working_form, "KWH", _KWH_TEXT_RE)
            if kwh is None:
                kwh = self._find_choice_field(stage_soup, "KWH", _KWH_TEXT_RE)
            if kwh is None:
                self._log_safe_form_diagnostics(stage_soup, working_form)
                raise LinzNetzParseError(
                    "Auswahl 'kWh' nach Viertelstunden-Auswahl nicht gefunden"
                )

        date_from = self._find_named_control(working_form, _FROM_RE)
        date_to = self._find_named_control(working_form, _TO_RE)
        if date_from is None or date_to is None:
            date_from = self._find_named_control(form, _FROM_RE)
            date_to = self._find_named_control(form, _TO_RE)
        if date_from is None or date_to is None:
            raise LinzNetzParseError("Datumsfelder nicht gefunden")
        if not date_from.get("name") or not date_to.get("name"):
            raise LinzNetzParseError("Wirksame Datumsfeldnamen nicht gefunden")

        view_state_value, working_form, date_ajax_complete = await self._async_select_day(
            behavior_soups=behavior_soups,
            form=working_form,
            form_id=form_id,
            date_from=date_from,
            date_to=date_to,
            quarter=quarter,
            kwh=kwh,
            requested_day=day,
            view_state_name=view_state_name,
            view_state_value=view_state_value,
        )

        date_from = self._find_named_control(working_form, _FROM_RE) or date_from
        date_to = self._find_named_control(working_form, _TO_RE) or date_to
        button = self._find_display_button(working_form) or self._find_display_button(form)
        if button is None or not button.get("id"):
            raise LinzNetzParseError("Button 'Anzeigen' nicht gefunden")
        button_id = str(button.get("id"))
        day_text = day.strftime("%d.%m.%Y")

        # Always process the current merged form. 0.1.10 could execute only the
        # button after successful DatePicker AJAX, while the client still held a
        # stale DOM snapshot. That allowed updated component/hidden state from
        # Partial Responses to be skipped and could leave the result table empty.
        payload = self._build_display_payload(
            working_form,
            form_id,
            button_id,
            date_from,
            date_to,
            quarter,
            kwh,
            day_text,
            view_state_name,
            view_state_value,
        )
        if date_ajax_complete:
            _LOGGER.debug(
                "LINZ NETZ Datumsauswahl serverseitig per PrimeFaces-AJAX verarbeitet: requested=%s",
                day.isoformat(),
            )
        period = working_form.find(
            "input", attrs={"name": re.compile(r"periodRange$", re.IGNORECASE)}
        )
        if period is None:
            period = form.find(
                "input", attrs={"name": re.compile(r"periodRange$", re.IGNORECASE)}
            )
        if period is not None and period.get("name"):
            payload[str(period.get("name"))] = str(period.get("value", "valid"))
        headers = {
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
        }
        result = await self._session.post(
            PORTAL_URL,
            data=payload,
            headers=headers,
            allow_redirects=True,
        )
        result.raise_for_status()
        body = await result.text()
        first_page = self._parse_readings(body)
        _LOGGER.debug(
            "LINZ NETZ Tabellenantwort: requested=%s rows=%s returned_days=%s",
            day.isoformat(),
            len(first_page),
            sorted({reading.start_local.date().isoformat() for reading in first_page}),
        )
        if not first_page:
            raise LinzNetzParseError(
                f"Keine Viertelstundenwerte für {day_text} gefunden"
            )
        self._validate_requested_day(day, first_page)

        pagination = self._parse_pagination(body)
        if pagination is None or pagination.row_count <= len(first_page):
            return self._validate_requested_day(
                day, self._deduplicate_readings(first_page)
            )

        latest_view_state = self._extract_partial_view_state(body) or view_state_value
        by_time = {r.start_local: r for r in first_page}
        expected_pages = math.ceil(pagination.row_count / pagination.rows)
        loaded_pages = 1
        for first in range(
            pagination.rows, pagination.row_count, pagination.rows
        ):
            page_payload = self._build_pagination_payload(
                payload,
                pagination,
                first,
                view_state_name,
                latest_view_state,
            )
            page_result = await self._session.post(
                PORTAL_URL,
                data=page_payload,
                headers=headers,
                allow_redirects=True,
            )
            page_result.raise_for_status()
            page_body = await page_result.text()
            page_readings = self._parse_readings(page_body)
            if not page_readings:
                self._log_pagination_warning(
                    pagination, expected_pages, loaded_pages, len(by_time)
                )
                raise LinzNetzParseError(
                    f"PrimeFaces-Seite ab Datensatz {first} lieferte keine Werte"
                )
            self._validate_requested_day(day, page_readings)
            loaded_pages += 1
            for reading in page_readings:
                by_time.setdefault(reading.start_local, reading)
            latest_view_state = (
                self._extract_partial_view_state(page_body) or latest_view_state
            )

        parsed = len(by_time)
        _LOGGER.info(
            "LINZ NETZ Pagination: rows=%s rowCount=%s pages=%s loaded=%s parsed=%s",
            pagination.rows,
            pagination.row_count,
            expected_pages,
            loaded_pages,
            parsed,
        )
        if parsed != pagination.row_count or loaded_pages != expected_pages:
            self._log_pagination_warning(
                pagination, expected_pages, loaded_pages, parsed
            )
            raise LinzNetzParseError(
                "PrimeFaces-Paginierung unvollständig: "
                f"{parsed} von {pagination.row_count} Werten gelesen"
            )
        return self._validate_requested_day(
            day, [by_time[key] for key in sorted(by_time)]
        )

    @staticmethod
    def _validate_requested_day(
        requested_day: date, readings: list[QuarterReading]
    ) -> list[QuarterReading]:
        """Fail closed if portal readings do not belong to the requested day."""
        returned_days = sorted({reading.start_local.date() for reading in readings})
        if returned_days != [requested_day]:
            returned = ", ".join(day.isoformat() for day in returned_days) or "none"
            raise LinzNetzParseError(
                "Portalantwort gehört nicht vollständig zum angeforderten Datum: "
                f"requested={requested_day.isoformat()} returned={returned}"
            )
        return readings

    async def _async_select_day(
        self,
        *,
        behavior_soups: list[BeautifulSoup],
        form: Tag,
        form_id: str,
        date_from: Tag,
        date_to: Tag,
        quarter: ChoiceField,
        kwh: ChoiceField,
        requested_day: date,
        view_state_name: str,
        view_state_value: str,
    ) -> tuple[str, Tag, bool]:
        """Apply the requested day through discovered PrimeFaces DatePicker AJAX."""
        current_form = form
        current_view_state = view_state_value
        day_text = requested_day.strftime("%d.%m.%Y")
        all_ajaxed = True

        for control, pattern in ((date_from, _FROM_RE), (date_to, _TO_RE)):
            behavior = None
            for behavior_soup in behavior_soups:
                behavior = self._find_date_behavior(behavior_soup, control, form_id)
                if behavior is not None:
                    break
            if behavior is None:
                all_ajaxed = False
                continue

            current_from = self._find_named_control(current_form, _FROM_RE) or date_from
            current_to = self._find_named_control(current_form, _TO_RE) or date_to
            payload = self._build_date_selection_payload(
                current_form,
                form_id,
                current_from,
                current_to,
                quarter,
                kwh,
                day_text,
                behavior,
                view_state_name,
                current_view_state,
            )
            _LOGGER.debug(
                "LINZ NETZ DatePicker-AJAX: requested=%s source=%s event=%s execute=%s render=%s",
                requested_day.isoformat(),
                behavior.source,
                behavior.event,
                behavior.execute,
                behavior.render,
            )
            result = await self._session.post(
                PORTAL_URL,
                data=payload,
                headers={
                    "Faces-Request": "partial/ajax",
                    "X-Requested-With": "XMLHttpRequest",
                },
                allow_redirects=True,
            )
            result.raise_for_status()
            body = await result.text()
            updates = self._parse_partial_updates(body)
            _LOGGER.debug(
                "LINZ NETZ Partial Response: requested=%s updates=%s ids=%s",
                requested_day.isoformat(),
                len(updates),
                self._safe_partial_update_ids(updates),
            )
            current_view_state = (
                self._extract_partial_view_state(body) or current_view_state
            )
            current_form = self._merge_partial_response_form(
                current_form, form_id, body
            )
            self._verify_rendered_day_if_present(
                current_form, pattern, requested_day
            )
            updated_soup = self._partial_response_soup(body)
            behavior_soups.insert(0, updated_soup)

        return current_view_state, current_form, all_ajaxed

    @classmethod
    def _find_date_behavior(
        cls, soup: BeautifulSoup, control: Tag, form_id: str
    ) -> AjaxBehavior | None:
        """Discover a DatePicker AJAX behavior from PrimeFaces widget JavaScript."""
        candidates = cls._date_component_candidates(control)
        if not candidates:
            return None
        for script in soup.find_all("script"):
            text = script.string or script.get_text(" ", strip=False)
            if not text or not any(candidate in text for candidate in candidates):
                continue
            for match in _PRIMEFACES_AJAX_RE.finditer(text):
                options = cls._parse_primefaces_ajax_options(match.group(1))
                source = options.get("s") or options.get("source")
                event = options.get("e") or options.get("event")
                if source not in candidates or not event:
                    continue
                if event.lower() not in {"dateselect", "change"}:
                    continue
                execute = options.get("p") or options.get("process") or source
                render = options.get("u") or options.get("update") or form_id
                return AjaxBehavior(source, execute, render, event)
        return None

    @staticmethod
    def _parse_primefaces_ajax_options(config: str) -> dict[str, str]:
        """Parse string-valued PrimeFaces.ab options without evaluating JavaScript."""
        options: dict[str, str] = {}
        for match in re.finditer(
            r"\b(s|source|e|event|p|process|u|update|f|form)\s*:\s*"
            r"(?:\"([^\"]*)\"|'([^']*)')",
            config,
            flags=re.IGNORECASE,
        ):
            options[match.group(1).lower()] = match.group(2) or match.group(3) or ""
        return options

    @staticmethod
    def _date_component_candidates(control: Tag) -> set[str]:
        """Return possible DatePicker component IDs for a rendered input control."""
        candidates: set[str] = set()
        for attr in ("name", "id"):
            raw = str(control.get(attr) or "")
            if raw:
                candidates.add(raw)
                candidates.add(_DATE_INPUT_SUFFIX_RE.sub("", raw))
        return {candidate for candidate in candidates if candidate}

    @classmethod
    def _build_date_selection_payload(
        cls,
        form: Tag,
        form_id: str,
        date_from: Tag,
        date_to: Tag,
        quarter: ChoiceField,
        kwh: ChoiceField,
        day_text: str,
        behavior: AjaxBehavior,
        view_state_name: str,
        view_state_value: str,
    ) -> dict[str, str]:
        """Build the browser-equivalent DatePicker AJAX payload."""
        payload = cls._collect_form_payload(form)
        payload.update(
            {
                "jakarta.faces.partial.ajax": "true",
                "jakarta.faces.source": behavior.source,
                "jakarta.faces.partial.execute": behavior.execute,
                "jakarta.faces.partial.render": behavior.render,
                "jakarta.faces.behavior.event": behavior.event,
                "jakarta.faces.partial.event": behavior.event,
                form_id: form_id,
                str(date_from.get("name")): day_text,
                str(date_to.get("name")): day_text,
                quarter.name: quarter.value,
                kwh.name: kwh.value,
                view_state_name: view_state_value,
            }
        )
        return payload

    @classmethod
    def _build_display_payload(
        cls,
        form: Tag,
        form_id: str,
        button_id: str,
        date_from: Tag,
        date_to: Tag,
        quarter: ChoiceField,
        kwh: ChoiceField,
        day_text: str,
        view_state_name: str,
        view_state_value: str,
    ) -> dict[str, str]:
        """Build final display request from the latest merged JSF form state."""
        payload = cls._collect_form_payload(form)
        payload.update(
            {
                "jakarta.faces.partial.ajax": "true",
                "jakarta.faces.source": button_id,
                "jakarta.faces.partial.execute": "@form",
                "jakarta.faces.partial.render": cls._find_render_target(form, form_id),
                "jakarta.faces.behavior.event": "action",
                "jakarta.faces.partial.event": "click",
                form_id: form_id,
                quarter.name: quarter.value,
                str(date_from.get("name")): day_text,
                str(date_to.get("name")): day_text,
                kwh.name: kwh.value,
                view_state_name: view_state_value,
            }
        )
        return payload

    @staticmethod
    def _verify_rendered_day_if_present(
        form: Tag, pattern: re.Pattern[str], requested_day: date
    ) -> None:
        """Fail closed if an updated date field explicitly renders another day."""
        control = LinzNetzClient._find_named_control(form, pattern)
        if control is None:
            return
        value = str(control.get("value") or "").strip()
        if value and value != requested_day.strftime("%d.%m.%Y"):
            raise LinzNetzParseError(
                "Portal bestätigte Datumswechsel nicht: "
                f"requested={requested_day.isoformat()}"
            )

    @staticmethod
    def _collect_form_payload(form: Tag) -> dict[str, str]:
        """Serialize successful controls from the consumption form in memory only."""
        payload: dict[str, str] = {}
        for tag in form.find_all(["input", "select", "textarea"]):
            name = str(tag.get("name") or "")
            if not name:
                continue
            if tag.name == "input":
                kind = str(tag.get("type") or "text").lower()
                if kind in {"password", "file", "submit", "button", "image", "reset"}:
                    continue
                if kind in {"checkbox", "radio"} and not tag.has_attr("checked"):
                    continue
                payload[name] = str(tag.get("value") or "")
            elif tag.name == "select":
                selected = tag.find("option", selected=True) or tag.find("option")
                if selected is not None:
                    payload[name] = str(selected.get("value") or "")
            else:
                payload[name] = tag.get_text()
        return payload

    @staticmethod
    def _parse_partial_updates(body: str) -> dict[str, str]:
        """Parse all JSF Partial Response updates without exposing their values."""
        return {match.group(1): match.group(2) for match in _PARTIAL_UPDATE_RE.finditer(body)}

    @staticmethod
    def _safe_partial_update_ids(updates: dict[str, str]) -> list[str]:
        """Return update IDs only; never log update contents or ViewState values."""
        return [key[:160] for key in updates][:30]

    @classmethod
    def _merge_partial_response_form(
        cls, form: Tag, form_id: str, body: str
    ) -> Tag:
        """Apply relevant JSF Partial Response component updates to a form snapshot."""
        updates = cls._parse_partial_updates(body)
        snapshot = BeautifulSoup(str(form), "html.parser")
        current = snapshot.find("form")
        if current is None:
            raise LinzNetzParseError("Verbrauchsformular konnte nicht fortgeschrieben werden")

        for update_id, html in updates.items():
            if _VIEW_STATE_RE.search(update_id):
                continue
            fragment_soup = BeautifulSoup(html, "html.parser")

            if update_id == form_id:
                replacement_form = fragment_soup.find("form", id=form_id)
                if replacement_form is None:
                    replacement_form = fragment_soup.find("form")
                if replacement_form is not None:
                    current = replacement_form
                    continue

            target = current.find(id=update_id)
            replacement = fragment_soup.find(id=update_id)
            if target is not None and replacement is not None:
                target.replace_with(replacement)
                continue

            # Some JSF renderers update a region whose CDATA contains only the
            # region's inner markup. Keep the wrapper and replace its children.
            if target is not None and replacement is None:
                target.clear()
                for child in list(fragment_soup.contents):
                    target.append(child.extract())

        return current

    @staticmethod
    def _build_pagination_payload(
        base: dict[str, str],
        pagination: PaginationInfo,
        first: int,
        view_state_name: str,
        view_state_value: str,
    ) -> dict[str, str]:
        payload = {
            k: v
            for k, v in base.items()
            if k
            not in {
                "jakarta.faces.source",
                "jakarta.faces.partial.execute",
                "jakarta.faces.partial.render",
                "jakarta.faces.behavior.event",
                "jakarta.faces.partial.event",
            }
        }
        table = pagination.table_id
        payload.update(
            {
                "jakarta.faces.partial.ajax": "true",
                "jakarta.faces.source": table,
                "jakarta.faces.partial.execute": table,
                "jakarta.faces.partial.render": table,
                table: table,
                f"{table}_pagination": "true",
                f"{table}_first": str(first),
                f"{table}_rows": str(pagination.rows),
                f"{table}_skipChildren": "true",
                f"{table}_encodeFeature": "true",
                view_state_name: view_state_value,
            }
        )
        return payload

    @staticmethod
    def _log_pagination_warning(
        p: PaginationInfo, pages: int, loaded: int, parsed: int
    ) -> None:
        _LOGGER.warning(
            "LINZ NETZ Pagination unvollständig: rows=%s rowCount=%s pages=%s "
            "loaded=%s parsed=%s",
            p.rows,
            p.row_count,
            pages,
            loaded,
            parsed,
        )

    async def _async_apply_choice(
        self,
        form_id: str,
        choice: ChoiceField,
        view_state_name: str,
        view_state_value: str,
    ) -> str:
        if not _SELECTED_CLASS_RE.search(choice.name):
            raise LinzNetzParseError(
                "Dynamisches selectedClass-Feld für Viertelstundenwerte fehlt"
            )
        component_id = _SELECTED_CLASS_RE.sub("", choice.name)
        payload = {
            "jakarta.faces.partial.ajax": "true",
            "jakarta.faces.source": component_id,
            "jakarta.faces.partial.execute": component_id,
            "jakarta.faces.partial.render": form_id,
            "jakarta.faces.behavior.event": "valueChange",
            "jakarta.faces.partial.event": "change",
            form_id: form_id,
            choice.name: choice.value,
            view_state_name: view_state_value,
        }
        result = await self._session.post(
            PORTAL_URL,
            data=payload,
            headers={
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=True,
        )
        result.raise_for_status()
        return await result.text()

    @staticmethod
    def _partial_response_soup(body: str) -> BeautifulSoup:
        updates = LinzNetzClient._parse_partial_updates(body)
        parts = [value for key, value in updates.items() if not _VIEW_STATE_RE.search(key)]
        if not parts:
            parts = re.findall(r"<!\[CDATA\[(.*?)\]\]>", body, flags=re.DOTALL)
        return BeautifulSoup("\n".join(parts) if parts else body, "html.parser")

    @staticmethod
    def _contains_password_form(html: str) -> bool:
        return (
            BeautifulSoup(html, "html.parser").find(
                "input", attrs={"type": "password"}
            )
            is not None
        )

    @classmethod
    def _find_consumption_form(cls, soup: BeautifulSoup) -> Tag | None:
        best: Tag | None = None
        score_best = -1
        for form in soup.find_all("form"):
            score = 0
            score += 4 if cls._find_named_control(form, _FROM_RE) is not None else 0
            score += 4 if cls._find_named_control(form, _TO_RE) is not None else 0
            score += (
                3 if cls._contains_marker(form, "ConsumQuarter", _QUARTER_TEXT_RE) else 0
            )
            score += 3 if cls._contains_marker(form, "KWH", _KWH_TEXT_RE) else 0
            score += (
                2
                if form.find("input", attrs={"name": _SELECTED_CLASS_RE})
                is not None
                else 0
            )
            score += 2 if cls._find_display_button(form) is not None else 0
            if score > score_best:
                best, score_best = form, score
        return best if score_best >= 4 else None

    @staticmethod
    def _find_named_control(form: Tag, pattern: re.Pattern[str]) -> Tag | None:
        for tag in form.find_all(["input", "select", "textarea"]):
            name = str(tag.get("name") or "")
            identifier = str(tag.get("id") or "")
            if name and (pattern.search(name) or pattern.search(identifier)):
                return tag
        return None

    @classmethod
    def _find_choice_field(
        cls, form: Tag, desired: str, label_re: re.Pattern[str]
    ) -> ChoiceField | None:
        selected = [
            tag
            for tag in form.find_all(["input", "select"])
            if _SELECTED_CLASS_RE.search(str(tag.get("name") or ""))
        ]
        for tag in selected:
            if (
                str(tag.get("value") or "").lower() == desired.lower()
                and tag.get("name")
            ):
                return ChoiceField(str(tag.get("name")), desired)
        if desired.startswith("Consum"):
            for tag in selected:
                if (
                    str(tag.get("value") or "").lower().startswith("consum")
                    and tag.get("name")
                ):
                    return ChoiceField(str(tag.get("name")), desired)
        for tag in selected:
            parent = tag.parent
            for _ in range(5):
                if not isinstance(parent, Tag) or parent is form:
                    break
                text = parent.get_text(" ", strip=True)
                if len(text) <= 500 and label_re.search(text) and tag.get("name"):
                    return ChoiceField(str(tag.get("name")), desired)
                parent = parent.parent
        for tag in form.find_all(["input", "button"]):
            if (
                str(tag.get("value") or "").lower() == desired.lower()
                and tag.get("name")
            ):
                return ChoiceField(str(tag.get("name")), desired)
        for option in form.find_all("option"):
            value = str(option.get("value") or "")
            if value.lower() == desired.lower() or label_re.search(
                option.get_text(" ", strip=True)
            ):
                select = option.find_parent("select")
                if select is not None and select.get("name"):
                    return ChoiceField(str(select.get("name")), value or desired)
        return None

    @staticmethod
    def _contains_marker(
        form: Tag, desired: str, label_re: re.Pattern[str]
    ) -> bool:
        low = desired.lower()
        for tag in form.find_all(["input", "option", "select", "label", "button"]):
            if (
                low == str(tag.get("value") or "").lower()
                or low in str(tag.get("name") or "").lower()
                or low in str(tag.get("id") or "").lower()
                or label_re.search(tag.get_text(" ", strip=True))
            ):
                return True
        return False

    @staticmethod
    def _find_display_button(form: Tag) -> Tag | None:
        for tag in form.find_all(["button", "input", "a"]):
            if "ANZEIGEN" in (
                tag.get_text(" ", strip=True) + " " + str(tag.get("value", ""))
            ).upper():
                return tag
        return form.find(id=re.compile(r"btnIdA1$", re.IGNORECASE))

    @staticmethod
    def _find_render_target(form: Tag, form_id: str) -> str:
        candidate = form.find(id=re.compile(r":list$", re.IGNORECASE))
        return (
            str(candidate.get("id"))
            if candidate is not None and candidate.get("id")
            else f"{form_id}:list"
        )

    @staticmethod
    def _extract_partial_view_state(body: str) -> str | None:
        match = re.search(
            r'<update\s+id=["\'](?:jakarta|javax)\.faces\.ViewState[^"\']*["\']\s*>'
            r"\s*<!\[CDATA\[(.*?)\]\]>",
            body,
            flags=re.DOTALL,
        )
        return match.group(1) if match else None

    @classmethod
    def _parse_pagination(cls, body: str) -> PaginationInfo | None:
        soup = cls._partial_response_soup(body)
        root = soup.find(id=_TABLE_ID_RE)
        table_id = str(root.get("id")) if root is not None else ""
        if not table_id:
            match = re.search(
                r'id=["\']([^"\']+:consumptionsTable)["\']',
                body,
                flags=re.IGNORECASE,
            )
            table_id = match.group(1) if match else ""
        if not table_id:
            match = re.search(
                r'["\']([^"\']+:consumptionsTable)["\']',
                body,
                flags=re.IGNORECASE,
            )
            table_id = match.group(1) if match else ""
        rows = re.search(r'["\']?rows["\']?\s*:\s*(\d+)', body)
        count = re.search(r'["\']?rowCount["\']?\s*:\s*(\d+)', body)
        if not table_id or rows is None or count is None:
            return None
        rows_n, count_n = int(rows.group(1)), int(count.group(1))
        return (
            PaginationInfo(table_id, rows_n, count_n)
            if rows_n > 0 and count_n > 0
            else None
        )

    @staticmethod
    def _deduplicate_readings(
        readings: list[QuarterReading],
    ) -> list[QuarterReading]:
        by_time = {r.start_local: r for r in readings}
        return [by_time[key] for key in sorted(by_time)]

    @classmethod
    def _log_safe_form_diagnostics(
        cls, soup: BeautifulSoup, form: Tag | None
    ) -> None:
        names: list[str] = []
        values: list[str] = []
        if form is not None:
            relevant = re.compile(
                r"calendar|consum|quarter|kwh|energy|period|btn|list|selectedClass",
                re.IGNORECASE,
            )
            for tag in form.find_all(["input", "select", "button", "textarea"]):
                for attr in ("name", "id"):
                    value = str(tag.get(attr) or "")
                    if value and relevant.search(value) and value not in names:
                        names.append(value[:120])
            for tag in form.find_all("option") + form.find_all(
                "input", attrs={"type": "radio"}
            ):
                value = str(tag.get("value") or "").strip()
                if value and len(value) <= 64 and value not in values:
                    values.append(value)
        _LOGGER.warning(
            "LINZ NETZ Parser-Diagnose: forms=%s selected_form=%s "
            "relevant_controls=%s choice_values=%s",
            len(soup.find_all("form")),
            str(form.get("id") or form.get("name") or "")
            if form is not None
            else None,
            names[:30],
            values[:30],
        )

    @staticmethod
    def _parse_readings(body: str) -> list[QuarterReading]:
        parts = re.findall(r"<!\[CDATA\[(.*?)\]\]>", body, flags=re.DOTALL)
        soup = BeautifulSoup("\n".join(parts) if parts else body, "html.parser")
        readings: list[QuarterReading] = []
        for row in soup.find_all("tr"):
            cells = [
                cell.get_text(" ", strip=True) for cell in row.find_all("td")
            ]
            if len(cells) < 2:
                continue
            timestamp = _DATE_RE.search(cells[0])
            value = _NUMBER_RE.search(cells[1])
            if timestamp and value:
                readings.append(
                    QuarterReading(
                        datetime.strptime(
                            timestamp.group(1), "%d.%m.%Y %H:%M"
                        ),
                        float(value.group(0).replace(",", ".")),
                    )
                )
        return readings
