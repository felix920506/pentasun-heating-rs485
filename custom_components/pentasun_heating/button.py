"""Clock synchronization button for Pentasun thermostats."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
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
    """Set up the buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        PentasunSyncClock(coordinator, address) for address in coordinator.addresses
    )


class PentasunSyncClock(PentasunEntity, ButtonEntity):
    """Sets the thermostat clock to Home Assistant's local time."""

    _attr_translation_key = "sync_clock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PentasunCoordinator, address: int) -> None:
        """Initialize the button."""
        super().__init__(coordinator, address, "sync_clock")

    async def async_press(self) -> None:
        """Write the current time to the thermostat."""
        await self.coordinator.async_sync_clock(self.address)
