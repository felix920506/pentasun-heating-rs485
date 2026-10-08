"""Polling coordinator for the thermostats on one RS485 bus."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from modbus_connection import ModbusError, ModbusUnit

from homeassistant.components.modbus import async_get_unit
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .bus import (
    async_read_thermostat,
    async_write_register,
    async_write_verified,
    build_params,
    configure_unit,
)
from .const import (
    CLOCK_DRIFT_TOLERANCE,
    CONF_ADDRESSES,
    CONF_AUTO_SYNC_CLOCK,
    DEFAULT_AUTO_SYNC_CLOCK,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_MISSED_POLLS,
    REG_HEATING,
    REG_HOUR,
    REG_LOCK,
    REG_MINUTE,
    REG_MODE,
    REG_POWER,
    REG_ROOM_TEMP,
    REG_SETPOINT,
    REG_WEEKDAY,
    UNAVAILABLE_RETRY_INTERVAL,
    IDENTIFY_TIME,
    WRITE_VERIFY_ATTEMPTS,
)

_LOGGER = logging.getLogger(__name__)

type PentasunConfigEntry = ConfigEntry[PentasunCoordinator]


class WriteNotAcceptedError(HomeAssistantError):
    """The thermostat acknowledged a write but kept its old value."""

    def __init__(self, address: int, register: int, value: int) -> None:
        """Initialize the error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="not_accepted",
            translation_placeholders={
                "address": str(address),
                "register": str(40001 + register),
                "value": str(value),
            },
        )

MINUTES_PER_WEEK = 7 * 24 * 60


def _signed(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


@dataclass(frozen=True, slots=True)
class ThermostatState:
    """Decoded register values of one thermostat."""

    power: bool
    mode: int
    target_temperature: float
    locked: bool
    minute: int
    hour: int
    weekday: int
    current_temperature: float | None
    heating: bool | None

    @classmethod
    def from_registers(cls, regs: list[int]) -> ThermostatState:
        """Decode a block of registers starting at register 0."""
        return cls(
            power=bool(regs[REG_POWER]),
            mode=regs[REG_MODE],
            target_temperature=_signed(regs[REG_SETPOINT]) / 10,
            locked=bool(regs[REG_LOCK]),
            minute=regs[REG_MINUTE],
            hour=regs[REG_HOUR],
            weekday=regs[REG_WEEKDAY],
            current_temperature=(
                _signed(regs[REG_ROOM_TEMP]) / 10 if len(regs) > REG_ROOM_TEMP else None
            ),
            heating=bool(regs[REG_HEATING]) if len(regs) > REG_HEATING else None,
        )

    @property
    def minute_of_week(self) -> int | None:
        """Return the thermostat clock as minutes since Monday 00:00."""
        if not (1 <= self.weekday <= 7 and 0 <= self.hour < 24 and 0 <= self.minute < 60):
            return None
        return ((self.weekday - 1) * 24 + self.hour) * 60 + self.minute


class PentasunCoordinator(DataUpdateCoordinator[dict[int, ThermostatState]]):
    """Polls every thermostat on the bus.

    ``data`` maps a Modbus address to its state. A thermostat that misses
    ``MAX_MISSED_POLLS`` polls in a row is left out, which makes only that
    device unavailable. Every request to a silent unit holds up the whole
    shared bus until it times out, so an unavailable thermostat is only
    retried every ``UNAVAILABLE_RETRY_INTERVAL``.
    """

    config_entry: PentasunConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PentasunConfigEntry) -> None:
        """Initialize the coordinator.

        Raises ``HomeAssistantError`` when another integration already uses the
        same port or host with different link settings.
        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
            always_update=False,
        )
        params = build_params(entry.data)
        self.addresses: list[int] = sorted(entry.options[CONF_ADDRESSES])
        # The modbus integration shares one connection per port or host between
        # all config entries and closes it when the last one unloads.
        self.units: dict[int, ModbusUnit] = {}
        for address in self.addresses:
            unit = self.units[address] = async_get_unit(hass, entry, params, address)
            # Our timing requests stay on the shared link after we unload
            # unless withdrawn.
            entry.async_on_unload(configure_unit(unit, entry.options))
        self._auto_sync_clock: bool = entry.options.get(
            CONF_AUTO_SYNC_CLOCK, DEFAULT_AUTO_SYNC_CLOCK
        )
        self._missed: dict[int, int] = {}
        self._next_retry: dict[int, datetime] = {}
        self._last_clock_sync: dict[int, datetime] = {}
        self._identifying: set[int] = set()

    async def _async_update_data(self) -> dict[int, ThermostatState]:
        previous = self.data or {}
        data: dict[int, ThermostatState] = {}
        last_error: ModbusError | None = None
        now = dt_util.utcnow()
        for address, unit in self.units.items():
            if (retry_at := self._next_retry.get(address)) and now < retry_at:
                continue
            try:
                regs = await async_read_thermostat(unit)
            except ModbusError as err:
                if not unit.connected:
                    # The link itself is down; don't wait for every address.
                    raise UpdateFailed(f"Cannot reach the RS485 bus: {err}") from err
                last_error = err
                missed = self._missed[address] = self._missed.get(address, 0) + 1
                if missed < MAX_MISSED_POLLS and address in previous:
                    data[address] = previous[address]
                    continue
                if missed == MAX_MISSED_POLLS or address not in previous:
                    _LOGGER.warning(
                        "Thermostat %s did not respond, retrying every %s: %s",
                        address, UNAVAILABLE_RETRY_INTERVAL, err,
                    )
                self._next_retry[address] = now + UNAVAILABLE_RETRY_INTERVAL
                continue
            self._next_retry.pop(address, None)
            if self._missed.pop(address, 0) >= MAX_MISSED_POLLS:
                _LOGGER.info("Thermostat %s is responding again", address)
            data[address] = state = ThermostatState.from_registers(regs)
            if self._auto_sync_clock:
                data[address] = await self._async_maybe_sync_clock(address, state)

        if not data and last_error is not None:
            raise UpdateFailed(f"No thermostat responded: {last_error}")
        return data

    async def _async_maybe_sync_clock(
        self, address: int, state: ThermostatState
    ) -> ThermostatState:
        now = dt_util.now()
        if (device_minute := state.minute_of_week) is not None:
            drift = abs(_minute_of_week(now) - device_minute)
            if min(drift, MINUTES_PER_WEEK - drift) <= CLOCK_DRIFT_TOLERANCE:
                return state
        # Don't hammer a thermostat that ignores the write.
        last = self._last_clock_sync.get(address)
        if last is not None and now - last < timedelta(hours=1):
            return state
        self._last_clock_sync[address] = now
        _LOGGER.info("Synchronizing clock of thermostat %s", address)
        try:
            return await self._async_write_clock(address, state, now)
        except (ModbusError, WriteNotAcceptedError) as err:
            _LOGGER.warning("Could not set clock of thermostat %s: %s", address, err)
            return state

    async def _async_write_clock(
        self, address: int, state: ThermostatState, now: datetime
    ) -> ThermostatState:
        """Write weekday, hour and minute; return the state read back."""
        unit = self.units[address]
        clock = {REG_WEEKDAY: now.isoweekday(), REG_HOUR: now.hour, REG_MINUTE: now.minute}
        for _ in range(WRITE_VERIFY_ATTEMPTS):
            for register, value in clock.items():
                await async_write_register(unit, register, value)
            regs = await async_read_thermostat(unit)
            # The thermostat's clock keeps running, so the minute may have ticked.
            if (
                regs[REG_WEEKDAY] == clock[REG_WEEKDAY]
                and regs[REG_HOUR] == clock[REG_HOUR]
                and (regs[REG_MINUTE] - clock[REG_MINUTE]) % 60 <= 1
            ):
                return ThermostatState.from_registers(regs)
        raise WriteNotAcceptedError(address, REG_WEEKDAY, clock[REG_WEEKDAY])

    async def async_sync_clock(self, address: int) -> None:
        """Set the thermostat clock to Home Assistant's local time."""
        state = self._require_state(address)
        try:
            new_state = await self._async_write_clock(address, state, dt_util.now())
        except ModbusError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"address": str(address), "error": str(err)},
            ) from err
        self._last_clock_sync[address] = dt_util.now()
        self._publish(address, new_state)

    async def async_write(self, address: int, register: int, value: int) -> None:
        """Write one register and check that the thermostat kept the value.

        The registers are read back and the state as read is shown; a value
        the thermostat ignores raises ``WriteNotAcceptedError``.
        """
        self._require_state(address)
        try:
            kept, regs = await async_write_verified(self.units[address], register, value)
            if not kept:
                self._publish(address, ThermostatState.from_registers(regs))
                raise WriteNotAcceptedError(address, register, value & 0xFFFF)
        except ModbusError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"address": str(address), "error": str(err)},
            ) from err
        self._publish(address, ThermostatState.from_registers(regs))

    async def async_identify(self, address: int) -> None:
        """Switch the thermostat's power for a while, so its display changes."""
        if address in self._identifying:
            return
        power = self._require_state(address).power
        self._identifying.add(address)
        try:
            await self.async_write(address, REG_POWER, int(not power))
            await asyncio.sleep(IDENTIFY_TIME)
        finally:
            try:
                await self.async_write(address, REG_POWER, int(power))
            finally:
                self._identifying.discard(address)

    def _require_state(self, address: int) -> ThermostatState:
        if self.data is None or (state := self.data.get(address)) is None:
            raise HomeAssistantError(f"Thermostat {address} is not available")
        return state

    def _publish(self, address: int, state: ThermostatState) -> None:
        self.async_set_updated_data({**self.data, address: state})


def _minute_of_week(now: datetime) -> int:
    return ((now.isoweekday() - 1) * 24 + now.hour) * 60 + now.minute
