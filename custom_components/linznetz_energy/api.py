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
        kwh = self._find_choice_field(form, "KWH", _KWH_TEXT_RE)
        if kwh is None:
            stage = await self._async_apply_choice(
                form_id, quarter, view_state_name, view_state_value
            )
            view_state_value = self._extract_partial_view_state(stage) or view_state_value
            stage_soup = self._partial_response_soup(stage)
            kwh = self._find_choice_field(stage_soup, "KWH", _KWH_TEXT_RE)
            if kwh is None:
                self._log_safe_form_diagnostics(stage_soup, stage_soup)
                raise LinzNetzParseError(
                    "Auswahl 'kWh' nach Viertelstunden-Auswahl nicht gefunden"
                )

        date_from = self._find_named_control(form, _FROM_RE)
        date_to = self._find_named_control(form, _TO_RE)
        button = self._find_display_button(form)
        if date_from is None or date_to is None:
            raise LinzNetzParseError("Datumsfelder nicht gefunden")
        if not date_from.get("name") or not date_to.get("name"):
            raise LinzNetzParseError("Wirksame Datumsfeldnamen nicht gefunden")
        if button is None or not button.get("id"):
            raise LinzNetzParseError("Button 'Anzeigen' nicht gefunden")
        button_id = str(button.get("id"))
        day_text = day.strftime("%d.%m.%Y")
        payload = {
            "jakarta.faces.partial.ajax": "true",
            "jakarta.faces.source": button_id,
            "jakarta.faces.partial.execute": button_id,
            "jakarta.faces.partial.render": self._find_render_target(form, form_id),
            "jakarta.faces.behavior.event": "action",
            "jakarta.faces.partial.event": "click",
            form_id: form_id,
            quarter.name: quarter.value,
            str(date_from.get("name")): day_text,
            str(date_to.get("name")): day_text,
            kwh.name: kwh.value,
            view_state_name: view_state_value,
        }
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
