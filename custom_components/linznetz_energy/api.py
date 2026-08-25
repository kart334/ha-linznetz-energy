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


class LinzNetzClient:
    """Very small client around the LINZ NETZ JSF portal."""

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

        # Keycloak-style forms normally use these exact names.
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

        # Ensure the service application itself now accepts the SSO session.
        verify = await self._session.get(PORTAL_URL, allow_redirects=True)
        verify.raise_for_status()
        verify_html = await verify.text()
        if self._contains_password_form(verify_html):
            raise LinzNetzAuthError("SSO-Sitzung wurde nicht übernommen")

    async def async_fetch_quarter_readings(self, day: date) -> list[QuarterReading]:
        """Fetch the 15-minute kWh values for one local calendar day."""
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

        form_id = form.get("id") or form.get("name")
        if not form_id:
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Formular-ID nicht gefunden")
        form_id = str(form_id)

        view_state = form.find("input", attrs={"name": _VIEW_STATE_RE})
        if view_state is None:
            # Some JSF pages place ViewState outside the business form.
            view_state = soup.find("input", attrs={"name": _VIEW_STATE_RE})
        if view_state is None or not view_state.get("name"):
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("JSF ViewState nicht gefunden")

        quarter_field = self._find_choice_field(
            form,
            desired_value="ConsumQuarter",
            label_re=_QUARTER_TEXT_RE,
        )
        if quarter_field is None:
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Auswahl 'Viertelstundenwerte' nicht gefunden")

        kwh_field = self._find_choice_field(
            form,
            desired_value="KWH",
            label_re=_KWH_TEXT_RE,
        )
        if kwh_field is None:
            self._log_safe_form_diagnostics(soup, form)
            raise LinzNetzParseError("Auswahl 'kWh' nicht gefunden")

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
            str(view_state.get("name")): str(view_state.get("value", "")),
        }

        period_input = form.find("input", attrs={"name": re.compile(r"periodRange$", re.IGNORECASE)})
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
        return readings

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
            if cls._find_display_button(form) is not None:
                score += 2
            if form.find(id=re.compile(r":list$", re.IGNORECASE)) is not None:
                score += 1

            if score > best_score:
                best_form = form
                best_score = score

        # A score below 4 means we did not even find one strong consumption marker.
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
        """Resolve PrimeFaces/JSF radio, select, hidden or label-backed choices."""
        desired_lower = desired_value.lower()

        # 1. Direct controls, including hidden inputs and radio buttons.
        for tag in form.find_all(["input", "button"]):
            value = str(tag.get("value") or "")
            if value.lower() == desired_lower and tag.get("name"):
                return ChoiceField(str(tag.get("name")), desired_value)

        # 2. Native select/option structures: the field name belongs to <select>.
        for option in form.find_all("option"):
            value = str(option.get("value") or "")
            text = option.get_text(" ", strip=True)
            if value.lower() == desired_lower or label_re.search(text):
                parent = option.find_parent("select")
                if parent is not None and parent.get("name"):
                    submit_value = value or desired_value
                    return ChoiceField(str(parent.get("name")), submit_value)

        # 3. Name/id may carry the semantic marker even when the option value is
        # rendered later by PrimeFaces JavaScript.
        token_re = re.compile(re.escape(desired_value), re.IGNORECASE)
        for tag in form.find_all(["input", "select", "button"]):
            name = str(tag.get("name") or "")
            tag_id = str(tag.get("id") or "")
            if (token_re.search(name) or token_re.search(tag_id)) and name:
                return ChoiceField(name, desired_value)

        # 4. Visible labels. PrimeFaces often renders a label beside a hidden or
        # radio input. Resolve the label's `for` target, then nearby controls.
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

            for ancestor in label.parents:
                if ancestor is form:
                    break
                if not isinstance(ancestor, Tag):
                    continue
                for candidate in ancestor.find_all(["input", "select"], limit=10):
                    resolved = cls._choice_from_target(candidate, desired_value)
                    if resolved is not None:
                        return resolved

        # 5. Last semantic fallback: inspect short visible component text and its
        # nearby controls without relying on a specific PrimeFaces widget tree.
        for container in form.find_all(["div", "span", "td", "li"]):
            text = container.get_text(" ", strip=True)
            if not text or len(text) > 160 or not label_re.search(text):
                continue
            for candidate in container.find_all(["input", "select"], limit=10):
                resolved = cls._choice_from_target(candidate, desired_value)
                if resolved is not None:
                    return resolved

        return None

    @staticmethod
    def _choice_from_target(target: Tag | None, desired_value: str) -> ChoiceField | None:
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

        nested = target.find(["input", "select"]) if isinstance(target, Tag) else None
        if nested is not None and nested.get("name"):
            value = str(nested.get("value") or desired_value)
            if value in {"", "on"}:
                value = desired_value
            return ChoiceField(str(nested.get("name")), value)
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
        # Observed current portal id; fallback only after semantic lookup.
        return form.find(id=re.compile(r"btnIdA1$", re.IGNORECASE))

    @staticmethod
    def _find_render_target(form: Tag, form_id: str) -> str:
        candidate = form.find(id=re.compile(r":list$", re.IGNORECASE))
        if candidate is not None and candidate.get("id"):
            return str(candidate.get("id"))
        return f"{form_id}:list"

    @staticmethod
    def _safe_control_names(form: Tag | None) -> list[str]:
        """Return only structural field names, never field values."""
        if form is None:
            return []
        names: list[str] = []
        relevant_re = re.compile(
            r"calendar|consum|quarter|kwh|energy|period|btn|list",
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
    def _log_safe_form_diagnostics(cls, soup: BeautifulSoup, form: Tag | None) -> None:
        """Log structural diagnostics without credentials, sessions or HTML dumps."""
        forms = soup.find_all("form")
        form_id = None
        if form is not None:
            form_id = str(form.get("id") or form.get("name") or "") or None
        _LOGGER.warning(
            "LINZ NETZ Parser-Diagnose: forms=%s selected_form=%s relevant_controls=%s choice_values=%s",
            len(forms),
            form_id,
            cls._safe_control_names(form),
            cls._safe_choice_values(form),
        )

    @staticmethod
    def _parse_readings(body: str) -> list[QuarterReading]:
        update_match = re.search(
            r"<!\[CDATA\[(.*?)\]\]>",
            body,
            flags=re.DOTALL,
        )
        html = update_match.group(1) if update_match else body
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
            value = float(value_match.group(0).replace(",", "."))
            readings.append(QuarterReading(start_local=start, kwh=value))

        return readings
