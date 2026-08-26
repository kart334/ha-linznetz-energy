"""LINZ NETZ Energy integration."""

from __future__ import annotations

import logging

from aiohttp import CookieJar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import LinzNetzClient
from .backfill import migrate_legacy_backfill_options
from .coordinator import LinzNetzCoordinator
from .const import DOMAIN
from .date_contract_diagnostics import DateContractDiagnosticSessionProxy

PLATFORMS = ["sensor"]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up LINZ NETZ Energy from a config entry."""
    session = async_create_clientsession(hass, cookie_jar=CookieJar())
    diagnostic_session = DateContractDiagnosticSessionProxy(session)
    client = LinzNetzClient(
        diagnostic_session,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = LinzNetzCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate legacy config entries without replaying stale backfill triggers."""
    if config_entry.version == 1:
        options, legacy_trigger_cleared = migrate_legacy_backfill_options(
            config_entry.options
        )
        hass.config_entries.async_update_entry(
            config_entry,
            options=options,
            version=2,
        )
        if legacy_trigger_cleared:
            _LOGGER.warning(
                "LINZ NETZ legacy manual-backfill trigger cleared during "
                "config-entry migration; no historical backfill was started"
            )
        else:
            _LOGGER.info("LINZ NETZ config entry migrated to version 2")
        return True

    if config_entry.version == 2:
        return True

    _LOGGER.error(
        "LINZ NETZ config-entry migration from unsupported version %s failed",
        config_entry.version,
    )
    return False


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options such as tariffs or backfill change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
