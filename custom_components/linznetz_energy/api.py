"""Client for the LINZ NETZ customer portal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Final
from urllib.parse import urljoin

from aiohttp import ClientResponseError, ClientSession
from bs4 import BeautifulSoup

from .const import PORTAL_URL

_DATE_RE: Final = re.compile(r"\b(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})\b")
_NUMBER_RE: Final = re.compile(r"-?\d+(?:[.,]\d+)?")


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
        form = soup.find("form")
        if form is None:
            raise LinzNetzParseError("Verbrauchsformular nicht gefunden")

        form_id = form.get("id") or form.get("name")
        if not form_id:
            raise LinzNetzParseError("Formular-ID nicht gefunden")

        view_state = form.find(
            "input", attrs={"name": re.compile(r"(jakarta|javax)\.faces\.ViewState")}
        )
        if view_state is None or not view_state.get("name"):
            raise LinzNetzParseError("JSF ViewState nicht gefunden")

        quarter_input = form.find("input", attrs={"value": "ConsumQuarter"})
        kwh_input = form.find("input", attrs={"value": "KWH"})
        if quarter_input is None or not quarter_input.get("name"):
            raise LinzNetzParseError("Auswahl 'Viertelstundenwerte' nicht gefunden")
        if kwh_input is None or not kwh_input.get("name"):
            raise LinzNetzParseError("Auswahl 'kWh' nicht gefunden")

        from_input = form.find("input", attrs={"name": re.compile(r"calendarFromRegion$")})
        to_input = form.find("input", attrs={"name": re.compile(r"calendarToRegion$")})
        if from_input is None or to_input is None:
            raise LinzNetzParseError("Datumsfelder nicht gefunden")

        button = self._find_display_button(form)
        if button is None or not button.get("id"):
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
            str(quarter_input.get("name")): "ConsumQuarter",
            str(from_input.get("name")): day_text,
            str(to_input.get("name")): day_text,
            str(kwh_input.get("name")): "KWH",
            str(view_state.get("name")): str(view_state.get("value", "")),
        }

        period_input = form.find("input", attrs={"name": re.compile(r"periodRange$")})
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

    @staticmethod
    def _find_display_button(form):
        for tag in form.find_all(["button", "input", "a"]):
            text = tag.get_text(" ", strip=True)
            value = tag.get("value", "")
            if "ANZEIGEN" in text.upper() or "ANZEIGEN" in str(value).upper():
                return tag
        # Observed current portal id; fallback only after semantic lookup.
        return form.find(id=re.compile(r"btnIdA1$"))

    @staticmethod
    def _find_render_target(form, form_id: str) -> str:
        candidate = form.find(id=re.compile(r":list$"))
        if candidate is not None and candidate.get("id"):
            return str(candidate.get("id"))
        return f"{form_id}:list"

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
