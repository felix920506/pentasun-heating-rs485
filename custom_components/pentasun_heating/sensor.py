"""Sensors for Pentasun thermostats."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PentasunConfigEntry, PentasunCoordinator
from .entity import PentasunEntity

PARALLEL_UPDATES = 0

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PentasunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for address in coordinator.addresses:
        entities.append(PentasunTemperature(coordinator, address))
        entities.append(PentasunClock(coordinator, address))
    async_add_entities(entities)


class PentasunTemperature(PentasunEntity, SensorEntity):
    """Room temperature measured by the thermostat."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: PentasunCoordinator, address: int) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, address, "temperature")

    @property
    def native_value(self) -> float | None:
        """Return the room temperature."""
        return self.state_data.current_temperature


class PentasunClock(PentasunEntity, SensorEntity):
    """The thermostat's internal clock, e.g. "Mon 07:30" (disabled by default)."""

    _attr_translation_key = "device_clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: PentasunCoordinator, address: int) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, address, "device_clock")

    @property
    def native_value(self) -> str | None:
        """Return the clock as weekday and time."""
        state = self.state_data
        if state.minute_of_week is None:
            return None
        return f"{WEEKDAYS[state.weekday - 1]} {state.hour:02d}:{state.minute:02d}"
