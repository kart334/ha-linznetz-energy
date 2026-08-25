"""Sensors for LINZ NETZ Energy."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LinzNetzCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up LINZ NETZ sensors."""
    coordinator: LinzNetzCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LinzNetzYesterdaySensor(coordinator, entry),
            LinzNetzLastSyncSensor(coordinator, entry),
        ]
    )


class _LinzNetzBaseSensor(CoordinatorEntity[LinzNetzCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: LinzNetzCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LINZ NETZ Smart Meter",
            manufacturer="LINZ NETZ",
            model="Serviceportal",
        )


class LinzNetzYesterdaySensor(_LinzNetzBaseSensor):
    """Yesterday's consumption."""

    _attr_name = "Verbrauch gestern"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: LinzNetzCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_yesterday"

    @property
    def native_value(self):
        return self.coordinator.data.get("yesterday_kwh")


class LinzNetzLastSyncSensor(_LinzNetzBaseSensor):
    """Last successful portal import."""

    _attr_name = "Letzter Import"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: LinzNetzCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_sync"

    @property
    def native_value(self):
        return self.coordinator.data.get("last_sync")
