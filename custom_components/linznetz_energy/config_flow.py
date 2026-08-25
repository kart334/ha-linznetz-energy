"""Config flow for LINZ NETZ Energy."""

from __future__ import annotations

import logging

from aiohttp import CookieJar
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import LinzNetzAuthError, LinzNetzClient, LinzNetzError
from .const import CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS, DOMAIN

_LOGGER = logging.getLogger(__name__)


class LinzNetzConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LINZ NETZ Energy."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Create the entry after validating the portal login."""
        errors = {}
        if user_input is not None:
            client = LinzNetzClient(
                async_create_clientsession(self.hass, cookie_jar=CookieJar()),
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.async_login()
            except LinzNetzAuthError:
                errors["base"] = "invalid_auth"
            except LinzNetzError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - config flow must fail closed.
                _LOGGER.exception("Unerwarteter Fehler beim LINZ-NETZ-Login")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id("linznetz_energy_portal")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="LINZ NETZ Strom",
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(
                    CONF_BACKFILL_DAYS,
                    default=DEFAULT_BACKFILL_DAYS,
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
