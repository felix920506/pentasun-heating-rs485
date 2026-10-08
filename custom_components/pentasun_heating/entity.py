"""Base entity for Pentasun thermostats."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAMES, DOMAIN, MANUFACTURER, MODEL
from .coordinator import PentasunCoordinator, ThermostatState


class PentasunEntity(CoordinatorEntity[PentasunCoordinator]):
    """An entity belonging to one thermostat on the bus."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PentasunCoordinator, address: int, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.address = address
        entry = coordinator.config_entry
        entry_id = entry.entry_id
        names = entry.options.get(CONF_NAMES, {})
        self._attr_unique_id = f"{entry_id}_{address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{address}")},
            name=names.get(str(address)) or f"Thermostat {address}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=f"Modbus address {address}",
        )

    @property
    def available(self) -> bool:
        """Return True if the thermostat answered the last poll."""
        return super().available and self.address in self.coordinator.data

    @property
    def state_data(self) -> ThermostatState:
        """Return the latest state of this thermostat."""
        return self.coordinator.data[self.address]
