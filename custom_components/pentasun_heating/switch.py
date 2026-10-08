"""Child lock switch for Pentasun thermostats."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import REG_LOCK
from .coordinator import PentasunConfigEntry, PentasunCoordinator
from .entity import PentasunEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PentasunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the child lock switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        PentasunChildLock(coordinator, address) for address in coordinator.addresses
    )


class PentasunChildLock(PentasunEntity, SwitchEntity):
    """Keypad lock of a thermostat."""

    _attr_translation_key = "child_lock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PentasunCoordinator, address: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, address, "child_lock")

    @property
    def is_on(self) -> bool:
        """Return True if the keypad is locked."""
        return self.state_data.locked

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the keypad."""
        await self.coordinator.async_write(self.address, REG_LOCK, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unlock the keypad."""
        await self.coordinator.async_write(self.address, REG_LOCK, 0)
