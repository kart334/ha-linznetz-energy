"""Client for the LINZ NETZ customer portal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import logging
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


class LinzNetzError(Exception):
    """Base LINZ NETZ error."""


class LinzNetzAuthError(LinzNetzError):
    """Authentication failed."""


class LinzNetzParseError(LinzNetzError):
    """Portal response could not be parsed."""


@dataclass(frozen=True)
class QuarterReading:
    """One 15-minute consumption value."""

    start_local: datetime
    kwh: float


@dataclass(frozen=True)
class ChoiceField:
    """Resolved HTML field name plus the value to submit."""

    name: str
    value: str


@dataclass(frozen=True)
class PaginationInfo:
    """PrimeFaces DataTable pagination metadata."""

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
        """Open the protected page and complete the SSO login form if needed."""
        response = await self._session.get(PORTAL_URL, allow_redirects=True)
        response.raise_for_status()
        html = await response.text()

        if not self._contains_password_form(html):
            return

        soup = BeautifulSoup(html, "html.parser")
        form = next(
            (
                candidate
                for candidate in soup.find_all("form")
                if candidate.find("input", attrs={"type": "password"}) is not None
            ),
            None,
        )
        if form is None:
            raise LinzNetzParseError("SSO-Anmeldeformular nicht gefunden")

        action = form.get("action")
        if not action:
            raise LinzNetzParseError("SSO-Anmeldeziel nicht gefunden")

        payload: dict[str, str] = {}
        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            if not name:
                continue
            input_type = (input_tag.get("type") or "").lower()
            if input_type == "password":
                payload[name] = self._password
            elif input_type in {"text", "email"} and (
                "user" in name.lower()
                or "mail" in name.lower()
                or "login" in name.lower()
            ):
                payload[name] = self._username
            elif input_type == "hidden":
                payload[name] = input_tag.get("value", "")

        payload.setdefault("username", self._username)
        payload.setdefault("password", self._password)

        login_url = urljoin(str(response.url), action)
        try:
            login_response = await self._session.post(
                login_url, data=payload, allow_redirects=True
            )
            login_response.raise_for_status()
        except ClientResponseError as err:
            raise LinzNetzAuthError(f"SSO HTTP-Fehler: {err.status}") from err

        login_html = await login_response.text()
        if self._contains_password_form(login_html):
            raise LinzNetzAuthError("Anmeldung bei LINZ NETZ abgelehnt")

        verify = await self._session.get(PORTAL_URL, allow_redirects=True)
        verify.raise_for_status()
        verify_html = await verify.text()
        if self._contains_password_form(verify_html):
            raise LinzNetzAuthError("SSO-Sitzung wurde nicht übernommen")

    async def async_fetch_quarter_readings(self, day: date) -> list[QuarterReading]:
        """Fetch all 15-minute kWh values for one local calendar day."""
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
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Formular-ID nicht gefunden")

        view_state = form.find("input", attrs={"name": _VIEW_STATE_RE})
        if view_state is None:
            view_state = soup.find("input", attrs={"name": _VIEW_STATE_RE})
        if view_state is None or not view_state.get("name"):
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("JSF ViewState nicht gefunden")
        view_state_name = str(view_state.get("name"))
        view_state_value = str(view_state.get("value", ""))

        quarter_field = self._find_choice_field(
            form,
            desired_value="ConsumQuarter",
            label_re=_QUARTER_TEXT_RE,
        )
        if quarter_field is None:
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Auswahl 'Viertelstundenwerte' nicht gefunden")

        # On the live portal the unit selector is rendered only after the
        # consumption period has been changed to quarter-hour values.
        kwh_field = self._find_choice_field(
            form,
            desired_value="KWH",
            label_re=_KWH_TEXT_RE,
        )
        if kwh_field is None:
            stage_body = await self._async_apply_choice(
                form_id=form_id,
                choice=quarter_field,
                view_state_name=view_state_name,
                view_state_value=view_state_value,
            )
            view_state_value = (
                self._extract_partial_view_state(stage_body) or view_state_value
            )
            stage_soup = self._partial_response_soup(stage_body)
            kwh_field = self._find_choice_field(
                stage_soup,
                desired_value="KWH",
                label_re=_KWH_TEXT_RE,
            )
            if kwh_field is None:
                self._log_safe_form_diagnostics(stage_soup, stage_soup)
                raise LinzNetzParseError(
                    "Auswahl 'kWh' nach Viertelstunden-Auswahl nicht gefunden"
                )

        from_input = self._find_named_control(form, _FROM_RE)
        to_input = self._find_named_control(form, _TO_RE)
        if from_input is None or to_input is None:
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Datumsfelder nicht gefunden")

        button = self._find_display_button(form)
        if button is None or not button.get("id"):
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Button 'Anzeigen' nicht gefunden")
        button_id = str(button.get("id"))

        render_id = self._find_render_target(form, form_id)
        day_text = day.strftime("%d.%m.%Y")

        payload: dict[str, str] = {
            "jakarta.faces.partial.ajax": "true",
            "jakarta.faces.source": button_id,
            "jakarta.faces.partial.execute": button_id,
            "jakarta.faces.partial.render": render_id,
            "jakarta.faces.behavior.event": "action",
            "jakarta.faces.partial.event": "click",
            form_id: form_id,
            quarter_field.name: quarter_field.value,
            str(from_input.get("name")): day_text,
            str(to_input.get("name")): day_text,
            kwh_field.name: kwh_field.value,
            view_state_name: view_state_value,
        }

        period_input = form.find(
            "input", attrs={"name": re.compile(r"periodRange$", re.IGNORECASE)}
        )
        if period_input is not None and period_input.get("name"):
            payload[str(period_input.get("name"))] = str(
                period_input.get("value", "valid")
            )

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

        readings = self._parse_readings(body)
        if not readings:
            raise LinzNetzParseError(
                f"Keine Viertelstundenwerte für {day_text} gefunden"
            )

        pagination = self._parse_pagination(body)
        if pagination is None or pagination.row_count <= len(readings):
            return readings

        latest_view_state = self._extract_partial_view_state(body) or view_state_value
        all_readings = list(readings)

        for first in range(pagination.rows, pagination.row_count, pagination.rows):
            page_payload = dict(payload)
            page_payload.update(
                {
                    "jakarta.faces.source": pagination.table_id,
                    "jakarta.faces.partial.execute": pagination.table_id,
                    "jakarta.faces.partial.render": pagination.table_id,
                    pagination.table_id: pagination.table_id,
                    f"{pagination.table_id}_pagination": "true",
                    f"{pagination.table_id}_first": str(first),
                    f"{pagination.table_id}_rows": str(pagination.rows),
                    f"{pagination.table_id}_skipChildren": "true",
                    f"{pagination.table_id}_encodeFeature": "true",
                    view_state_name: latest_view_state,
                }
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
                raise LinzNetzParseError(
                    f"PrimeFaces-Seite ab Datensatz {first} lieferte keine Werte"
                )
            all_readings.extend(page_readings)
            latest_view_state = (
                self._extract_partial_view_state(page_body) or latest_view_state
            )

        if len(all_readings) < pagination.row_count:
            raise LinzNetzParseError(
                "PrimeFaces-Paginierung unvollständig: "
                f"{len(all_readings)} von {pagination.row_count} Werten gelesen"
            )

        return all_readings[: pagination.row_count]

    async def _async_apply_choice(
        self,
        *,
        form_id: str,
        choice: ChoiceField,
        view_state_name: str,
        view_state_value: str,
    ) -> str:
        """Apply a dynamic selectedClass choice and return the partial response."""
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
        return await result.text()

    @staticmethod
    def _partial_response_soup(body: str) -> BeautifulSoup:
        """Parse HTML fragments from a JSF partial response."""
        cdata_blocks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", body, flags=re.DOTALL)
        html = "\n".join(cdata_blocks) if cdata_blocks else body
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _contains_password_form(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        return soup.find("input", attrs={"type": "password"}) is not None

    @classmethod
    def _find_consumption_form(cls, soup: BeautifulSoup) -> Tag | None:
        """Pick the form that actually contains the consumption controls."""
        forms = soup.find_all("form")
        if not forms:
            return None

        best_form: Tag | None = None
        best_score = -1
        for form in forms:
            score = 0
            if cls._find_named_control(form, _FROM_RE) is not None:
                score += 4
            if cls._find_named_control(form, _TO_RE) is not None:
                score += 4
            if cls._contains_semantic_marker(form, "ConsumQuarter", _QUARTER_TEXT_RE):
                score += 3
            if cls._contains_semantic_marker(form, "KWH", _KWH_TEXT_RE):
                score += 3
            if form.find("input", attrs={"name": _SELECTED_CLASS_RE}) is not None:
                score += 2
            if cls._find_display_button(form) is not None:
                score += 2
            if form.find(id=re.compile(r":list$", re.IGNORECASE)) is not None:
                score += 1

            if score > best_score:
                best_form = form
                best_score = score

        return best_form if best_score >= 4 else None

    @staticmethod
    def _find_named_control(form: Tag, pattern: re.Pattern[str]) -> Tag | None:
        """Find an input/select/textarea by name or id suffix."""
        for tag in form.find_all(["input", "select", "textarea"]):
            name = str(tag.get("name") or "")
            tag_id = str(tag.get("id") or "")
            if pattern.search(name) or pattern.search(tag_id):
                return tag
        return None

    @classmethod
    def _find_choice_field(
        cls,
        form: Tag,
        *,
        desired_value: str,
        label_re: re.Pattern[str],
    ) -> ChoiceField | None:
        """Resolve dynamic PrimeFaces/JSF choice controls semantically."""
        desired_lower = desired_value.lower()

        selected_class_fields = [
            tag
            for tag in form.find_all(["input", "select"])
            if _SELECTED_CLASS_RE.search(str(tag.get("name") or ""))
        ]
        for tag in selected_class_fields:
            value = str(tag.get("value") or "")
            if value.lower() == desired_lower and tag.get("name"):
                return ChoiceField(str(tag.get("name")), desired_value)

        if desired_value.startswith("Consum"):
            for tag in selected_class_fields:
                value = str(tag.get("value") or "")
                if value.lower().startswith("consum") and tag.get("name"):
                    return ChoiceField(str(tag.get("name")), desired_value)

        for tag in selected_class_fields:
            if cls._control_context_matches(tag, form, label_re) and tag.get("name"):
                return ChoiceField(str(tag.get("name")), desired_value)

        for tag in form.find_all(["input", "button"]):
            value = str(tag.get("value") or "")
            if value.lower() == desired_lower and tag.get("name"):
                return ChoiceField(str(tag.get("name")), desired_value)

        for option in form.find_all("option"):
            value = str(option.get("value") or "")
            text = option.get_text(" ", strip=True)
            if value.lower() == desired_lower or label_re.search(text):
                parent = option.find_parent("select")
                if parent is not None and parent.get("name"):
                    return ChoiceField(str(parent.get("name")), value or desired_value)

        token_re = re.compile(re.escape(desired_value), re.IGNORECASE)
        for tag in form.find_all(["input", "select", "button"]):
            name = str(tag.get("name") or "")
            tag_id = str(tag.get("id") or "")
            if (token_re.search(name) or token_re.search(tag_id)) and name:
                return ChoiceField(name, desired_value)

        for label in form.find_all("label"):
            text = label.get_text(" ", strip=True)
            if not label_re.search(text):
                continue
            target_id = label.get("for")
            if target_id:
                target = form.find(id=target_id)
                resolved = cls._choice_from_target(target, desired_value)
                if resolved is not None:
                    return resolved

        return None

    @staticmethod
    def _control_context_matches(
        control: Tag,
        form: Tag,
        label_re: re.Pattern[str],
    ) -> bool:
        """Check nearby component containers for semantic visible text."""
        ancestor: Tag | None = control
        for _ in range(5):
            parent = ancestor.parent if ancestor is not None else None
            if not isinstance(parent, Tag) or parent is form:
                break
            text = parent.get_text(" ", strip=True)
            if text and len(text) <= 500 and label_re.search(text):
                return True
            ancestor = parent
        return False

    @staticmethod
    def _choice_from_target(
        target: Tag | None, desired_value: str
    ) -> ChoiceField | None:
        if target is None:
            return None
        if target.name == "select" and target.get("name"):
            return ChoiceField(str(target.get("name")), desired_value)
        if target.name == "input" and target.get("name"):
            value = str(target.get("value") or desired_value)
            if value in {"", "on"}:
                value = desired_value
            return ChoiceField(str(target.get("name")), value)
        if target.get("name"):
            return ChoiceField(str(target.get("name")), desired_value)
        return None

    @classmethod
    def _contains_semantic_marker(
        cls,
        form: Tag,
        desired_value: str,
        label_re: re.Pattern[str],
    ) -> bool:
        desired_lower = desired_value.lower()
        for tag in form.find_all(["input", "option", "select", "label", "button"]):
            value = str(tag.get("value") or "")
            name = str(tag.get("name") or "")
            tag_id = str(tag.get("id") or "")
            text = tag.get_text(" ", strip=True)
            if (
                value.lower() == desired_lower
                or desired_lower in name.lower()
                or desired_lower in tag_id.lower()
                or label_re.search(text)
            ):
                return True
        return False

    @staticmethod
    def _find_display_button(form: Tag) -> Tag | None:
        for tag in form.find_all(["button", "input", "a"]):
            text = tag.get_text(" ", strip=True)
            value = tag.get("value", "")
            if "ANZEIGEN" in text.upper() or "ANZEIGEN" in str(value).upper():
                return tag
        return form.find(id=re.compile(r"btnIdA1$", re.IGNORECASE))

    @staticmethod
    def _find_render_target(form: Tag, form_id: str) -> str:
        candidate = form.find(id=re.compile(r":list$", re.IGNORECASE))
        if candidate is not None and candidate.get("id"):
            return str(candidate.get("id"))
        return f"{form_id}:list"

    @staticmethod
    def _extract_partial_view_state(body: str) -> str | None:
        """Read updated JSF ViewState from a partial response without logging it."""
        match = re.search(
            r'<update\s+id=["\'](?:jakarta|javax)\.faces\.ViewState[^"\']*["\']\s*>\s*<!\[CDATA\[(.*?)\]\]>',
            body,
            flags=re.DOTALL,
        )
        return match.group(1) if match else None

    @staticmethod
    def _parse_pagination(body: str) -> PaginationInfo | None:
        """Resolve PrimeFaces DataTable id, page size and total row count."""
        table_match = re.search(
            r'<table[^>]+id=["\']([^"\']+:consumptionsTable)["\']',
            body,
            flags=re.IGNORECASE,
        )
        if table_match is None:
            return None

        rows_match = re.search(r'["\']?rows["\']?\s*:\s*(\d+)', body)
        row_count_match = re.search(r'["\']?rowCount["\']?\s*:\s*(\d+)', body)
        if rows_match is None or row_count_match is None:
            return None

        rows = int(rows_match.group(1))
        row_count = int(row_count_match.group(1))
        if rows <= 0 or row_count <= 0:
            return None

        return PaginationInfo(
            table_id=table_match.group(1),
            rows=rows,
            row_count=row_count,
        )

    @staticmethod
    def _safe_control_names(form: Tag | None) -> list[str]:
        """Return only structural field names, never field values."""
        if form is None:
            return []
        names: list[str] = []
        relevant_re = re.compile(
            r"calendar|consum|quarter|kwh|energy|period|btn|list|selectedClass",
            re.IGNORECASE,
        )
        for tag in form.find_all(["input", "select", "button", "textarea"]):
            for attribute in ("name", "id"):
                value = str(tag.get(attribute) or "")
                if value and relevant_re.search(value) and value not in names:
                    names.append(value[:120])
        return names[:30]

    @staticmethod
    def _safe_choice_values(form: Tag | None) -> list[str]:
        """Return short radio/option values only; never hidden input values."""
        if form is None:
            return []
        values: list[str] = []
        for option in form.find_all("option"):
            value = str(option.get("value") or "").strip()
            if value and len(value) <= 64 and value not in values:
                values.append(value)
        for input_tag in form.find_all("input", attrs={"type": "radio"}):
            value = str(input_tag.get("value") or "").strip()
            if value and len(value) <= 64 and value not in values:
                values.append(value)
        return values[:30]

    @classmethod
    def _log_safe_form_diagnostics(
        cls, soup: BeautifulSoup, form: Tag | None
    ) -> None:
        """Log structural diagnostics without credentials, sessions or HTML dumps."""
        forms = soup.find_all("form")
        form_id = None
        if form is not None:
            form_id = str(form.get("id") or form.get("name") or "") or None
        _LOGGER.warning(
            "LINZ NETZ Parser-Diagnose: forms=%s selected_form=%s "
            "relevant_controls=%s choice_values=%s",
            len(forms),
            form_id,
            cls._safe_control_names(form),
            cls._safe_choice_values(form),
        )

    @staticmethod
    def _parse_readings(body: str) -> list[QuarterReading]:
        """Parse timestamp and measured-consumption columns from table rows."""
        cdata_blocks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", body, flags=re.DOTALL)
        html = "\n".join(cdata_blocks) if cdata_blocks else body
        soup = BeautifulSoup(html, "html.parser")

        readings: list[QuarterReading] = []
        for row in soup.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
            if len(cells) < 2:
                continue
            date_match = _DATE_RE.search(cells[0])
            if not date_match:
                continue
            value_match = _NUMBER_RE.search(cells[1])
            if not value_match:
                continue
            start = datetime.strptime(date_match.group(1), "%d.%m.%Y %H:%M")
            value = float(value_match.group(0).replace(", ".replace(" ", ""), "."))
            readings.append(QuarterReading(start_local=start, kwh=value))

        return readings
