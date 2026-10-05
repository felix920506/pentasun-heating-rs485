"""Heating status binary sensor for Pentasun thermostats."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PentasunConfigEntry, PentasunCoordinator
from .entity import PentasunEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PentasunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the heating binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        PentasunHeating(coordinator, address) for address in coordinator.addresses
    )


class PentasunHeating(PentasunEntity, BinarySensorEntity):
    """On while the thermostat is calling for heat."""

    _attr_translation_key = "heating"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: PentasunCoordinator, address: int) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, address, "heating")

    @property
    def is_on(self) -> bool | None:
        """Return True while heating."""
        return self.state_data.heating
