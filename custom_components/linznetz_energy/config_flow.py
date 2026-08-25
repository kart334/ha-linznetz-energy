"""Config flow for LINZ NETZ Energy."""

from __future__ import annotations

from datetime import datetime
import json
import logging

from aiohttp import CookieJar
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import LinzNetzAuthError, LinzNetzClient, LinzNetzError
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_RUN_BACKFILL,
    CONF_TARIFF_HISTORY,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_TARIFF_HISTORY,
    DOMAIN,
    MAX_BACKFILL_DAYS,
)

_LOGGER = logging.getLogger(__name__)


def _default_tariff_json() -> str:
    return json.dumps(DEFAULT_TARIFF_HISTORY, ensure_ascii=False, indent=2)


def _validate_tariff_history(value: str) -> str:
    """Validate editable tariff history without persisting any private data."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("invalid_json") from err
    if not isinstance(parsed, list):
        raise vol.Invalid("invalid_tariff_history")
    for item in parsed:
        if not isinstance(item, dict):
            raise vol.Invalid("invalid_tariff_history")
        try:
            datetime.strptime(str(item["valid_from"]), "%Y-%m-%d")
            energy_price = float(item["energy_price"])
            base_price = float(item["base_price_month"])
        except (KeyError, TypeError, ValueError) as err:
            raise vol.Invalid("invalid_tariff_history") from err
        if energy_price < 0 or base_price < 0:
            raise vol.Invalid("invalid_tariff_history")
    return value


class LinzNetzConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LINZ NETZ Energy."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return LinzNetzOptionsFlow()

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
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_BACKFILL_DAYS)),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )


class LinzNetzOptionsFlow(OptionsFlow):
    """Configure tariff history and one-shot historical backfill."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                _validate_tariff_history(user_input[CONF_TARIFF_HISTORY])
            except vol.Invalid:
                errors[CONF_TARIFF_HISTORY] = "invalid_tariff_history"
            else:
                return self.async_create_entry(
                    data=self.config_entry.options | user_input
                )

        current_backfill = int(
            self.config_entry.options.get(
                CONF_BACKFILL_DAYS,
                self.config_entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
            )
        )
        current_tariffs = self.config_entry.options.get(
            CONF_TARIFF_HISTORY,
            _default_tariff_json(),
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_BACKFILL_DAYS,
                    default=current_backfill,
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_BACKFILL_DAYS)),
                vol.Optional(
                    CONF_RUN_BACKFILL,
                    default=False,
                ): bool,
                vol.Required(
                    CONF_TARIFF_HISTORY,
                    default=current_tariffs,
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
