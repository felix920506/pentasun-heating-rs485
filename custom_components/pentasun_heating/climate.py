"""Climate entity for Pentasun thermostats."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    MODES,
    SETTABLE_MODES,
    REG_MODE,
    REG_POWER,
    REG_SETPOINT,
)
from .coordinator import PentasunConfigEntry, PentasunCoordinator
from .entity import PentasunEntity

PARALLEL_UPDATES = 0  # writes are serialized by the Modbus client


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PentasunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the climate entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        PentasunClimate(coordinator, address) for address in coordinator.addresses
    )


class PentasunClimate(PentasunEntity, ClimateEntity):
    """A floor heating thermostat."""

    _attr_name = None
    _attr_translation_key = "thermostat"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = PRECISION_HALVES

    def __init__(self, coordinator: PentasunCoordinator, address: int) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, address, "climate")
        options = coordinator.config_entry.options
        self._attr_min_temp = options.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)
        self._attr_max_temp = options.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP)

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature."""
        return self.state_data.current_temperature

    @property
    def target_temperature(self) -> float:
        """Return the set point."""
        return self.state_data.target_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        """Return heat when the thermostat is powered on."""
        return HVACMode.HEAT if self.state_data.power else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return whether the floor is currently being heated."""
        state = self.state_data
        if not state.power:
            return HVACAction.OFF
        if state.heating is None:
            return None
        return HVACAction.HEATING if state.heating else HVACAction.IDLE

    @property
    def preset_modes(self) -> list[str]:
        """Return the modes the thermostat accepts, plus its current one."""
        # Read even while the thermostat is unavailable, so don't rely on state.
        if self.address not in self.coordinator.data:
            return SETTABLE_MODES
        if (current := self.preset_mode) is not None and current not in SETTABLE_MODES:
            return [*SETTABLE_MODES, current]
        return SETTABLE_MODES

    @property
    def preset_mode(self) -> str | None:
        """Return the operating mode (manual, timer, schedule)."""
        mode = self.state_data.mode
        return MODES[mode] if 0 <= mode < len(MODES) else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Turn the thermostat on or off."""
        power = hvac_mode == HVACMode.HEAT
        await self.coordinator.async_write(self.address, REG_POWER, int(power))

    async def async_turn_on(self) -> None:
        """Turn the thermostat on."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Turn the thermostat off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a new target temperature."""
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        raw = round(temperature * 2) * 5  # thermostat resolution is 0.5 °C
        await self.coordinator.async_write(self.address, REG_SETPOINT, raw)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Change the operating mode."""
        mode = MODES.index(preset_mode)
        await self.coordinator.async_write(self.address, REG_MODE, mode)
