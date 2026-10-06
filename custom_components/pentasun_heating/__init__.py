"""The Pentasun floor heating thermostat integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .bus import async_get_bus_usage, build_params
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
    try:
        coordinator = PentasunCoordinator(hass, entry)
    except HomeAssistantError as err:
        # Another integration holds this port or host with other serial settings.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="link_conflict",
            translation_placeholders={"error": str(err)},
        ) from err
    _async_check_yaml_hubs(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    _remove_stale_devices(hass, entry, coordinator.addresses)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PentasunConfigEntry) -> bool:
    """Unload a config entry; the modbus integration closes the shared link."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PentasunConfigEntry) -> None:
    """Clean up the repair issue of a removed entry."""
    ir.async_delete_issue(hass, DOMAIN, f"yaml_hub_{entry.entry_id}")


def _async_check_yaml_hubs(hass: HomeAssistant, entry: PentasunConfigEntry) -> None:
    """Warn when a Modbus YAML hub opens a second link to the same bus.

    Config entries share one connection per port or host, but YAML hubs open
    their own, so their requests can collide with ours on the wire.
    """
    issue_id = f"yaml_hub_{entry.entry_id}"
    hubs = async_get_bus_usage(hass, build_params(entry.data)).yaml_hubs
    if not hubs:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="yaml_hub_shared",
        translation_placeholders={"hubs": ", ".join(sorted(hubs)), "title": entry.title},
    )


def _remove_stale_devices(
    hass: HomeAssistant, entry: PentasunConfigEntry, addresses: list[int]
) -> None:
    """Remove devices for addresses that were removed in the options."""
    registry = dr.async_get(hass)
    wanted = {(DOMAIN, f"{entry.entry_id}_{address}") for address in addresses}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & wanted:
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
