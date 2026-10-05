"""The Pentasun floor heating thermostat integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import PentasunConfigEntry, PentasunCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: PentasunConfigEntry) -> bool:
    """Set up a thermostat bus from a config entry."""
    coordinator = PentasunCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.client.close()
        raise
    entry.runtime_data = coordinator

    _remove_stale_devices(hass, entry, coordinator.addresses)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PentasunConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _remove_stale_devices(
    hass: HomeAssistant, entry: PentasunConfigEntry, addresses: list[int]
) -> None:
    """Remove devices for addresses that were removed in the options."""
    registry = dr.async_get(hass)
    wanted = {(DOMAIN, f"{entry.entry_id}_{address}") for address in addresses}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & wanted:
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
