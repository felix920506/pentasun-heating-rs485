"""Polling coordinator for the thermostats on one RS485 bus."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CLOCK_DRIFT_TOLERANCE,
    CONF_ADDRESSES,
    CONF_AUTO_SYNC_CLOCK,
    CONF_BAUDRATE,
    CONF_CONNECTION_TYPE,
    CONF_MESSAGE_DELAY,
    CONF_PARITY,
    CONF_STOPBITS,
    CONF_TIMEOUT,
    CONN_MODBUS_TCP,
    CONN_RFC2217,
    CONN_RTU_OVER_TCP,
    CONN_SERIAL,
    DEFAULT_AUTO_SYNC_CLOCK,
    DEFAULT_BAUDRATE,
    DEFAULT_MESSAGE_DELAY,
    DEFAULT_PARITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STOPBITS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    REG_HEATING,
    REG_HOUR,
    REG_LOCK,
    REG_MINUTE,
    REG_MODE,
    REG_POWER,
    REG_ROOM_TEMP,
    REG_SETPOINT,
    REG_WEEKDAY,
    REGISTER_COUNT,
)
from .modbus import (
    ModbusClient,
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionResponse,
    ModbusTcpTransport,
    SerialRtuTransport,
    TcpRtuTransport,
    Transport,
)

_LOGGER = logging.getLogger(__name__)

type PentasunConfigEntry = ConfigEntry[PentasunCoordinator]

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


def create_transport(data: Mapping[str, Any]) -> Transport:
    """Create the transport described by config entry data."""
    conn = data[CONF_CONNECTION_TYPE]
    serial_settings = {
        "baudrate": data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
        "parity": data.get(CONF_PARITY, DEFAULT_PARITY),
        "stopbits": data.get(CONF_STOPBITS, DEFAULT_STOPBITS),
    }
    if conn == CONN_SERIAL:
        return SerialRtuTransport(data[CONF_DEVICE], **serial_settings)
    if conn == CONN_RFC2217:
        return SerialRtuTransport(
            f"rfc2217://{data[CONF_HOST]}:{data[CONF_PORT]}", **serial_settings
        )
    if conn == CONN_RTU_OVER_TCP:
        return TcpRtuTransport(data[CONF_HOST], data[CONF_PORT])
    if conn == CONN_MODBUS_TCP:
        return ModbusTcpTransport(data[CONF_HOST], data[CONF_PORT])
    raise ValueError(f"Unknown connection type {conn}")


def create_client(data: Mapping[str, Any], options: Mapping[str, Any]) -> ModbusClient:
    """Create a Modbus client from config entry data and options."""
    return ModbusClient(
        create_transport(data),
        timeout=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        message_delay=options.get(CONF_MESSAGE_DELAY, DEFAULT_MESSAGE_DELAY) / 1000,
    )


async def async_read_thermostat(client: ModbusClient, address: int) -> list[int]:
    """Read the register block, falling back for firmware without register 9."""
    try:
        return await client.read_holding_registers(address, 0, REGISTER_COUNT)
    except ModbusExceptionResponse as err:
        if err.code != 2:  # 2 = illegal data address
            raise
        return await client.read_holding_registers(address, 0, REGISTER_COUNT - 1)


class PentasunCoordinator(DataUpdateCoordinator[dict[int, ThermostatState]]):
    """Polls every thermostat on the bus.

    ``data`` maps a Modbus address to its state; addresses that did not answer
    during the last poll are absent, which makes only that device unavailable.
    """

    config_entry: PentasunConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PentasunConfigEntry) -> None:
        """Initialize the coordinator."""
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
        self.client = create_client(entry.data, entry.options)
        self.addresses: list[int] = sorted(entry.options[CONF_ADDRESSES])
        self._auto_sync_clock: bool = entry.options.get(
            CONF_AUTO_SYNC_CLOCK, DEFAULT_AUTO_SYNC_CLOCK
        )
        self._last_clock_sync: dict[int, datetime] = {}

    async def async_shutdown(self) -> None:
        """Close the bus connection."""
        await super().async_shutdown()
        await self.client.close()

    async def _async_update_data(self) -> dict[int, ThermostatState]:
        data: dict[int, ThermostatState] = {}
        last_error: ModbusError | None = None
        for address in self.addresses:
            try:
                regs = await async_read_thermostat(self.client, address)
            except ModbusConnectionError as err:
                # The whole bus is unreachable; don't wait for every address.
                raise UpdateFailed(f"Cannot reach the RS485 bus: {err}") from err
            except ModbusError as err:
                last_error = err
                if self.data is None or address in self.data:
                    _LOGGER.warning("Thermostat %s did not respond: %s", address, err)
                continue
            if self.data is not None and address not in self.data:
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
        except ModbusError as err:
            _LOGGER.warning("Could not set clock of thermostat %s: %s", address, err)
            return state

    async def _async_write_clock(
        self, address: int, state: ThermostatState, now: datetime
    ) -> ThermostatState:
        weekday = now.isoweekday()
        await self.client.write_register(address, REG_WEEKDAY, weekday)
        await self.client.write_register(address, REG_HOUR, now.hour)
        await self.client.write_register(address, REG_MINUTE, now.minute)
        return replace(state, weekday=weekday, hour=now.hour, minute=now.minute)

    async def async_sync_clock(self, address: int) -> None:
        """Set the thermostat clock to Home Assistant's local time."""
        state = self._require_state(address)
        try:
            new_state = await self._async_write_clock(address, state, dt_util.now())
        except ModbusError as err:
            raise HomeAssistantError(
                f"Failed to set clock of thermostat {address}: {err}"
            ) from err
        self._last_clock_sync[address] = dt_util.now()
        self._publish(address, new_state)

    async def async_write(self, address: int, register: int, value: int, **changes: Any) -> None:
        """Write one register and update the cached state with ``changes``."""
        state = self._require_state(address)
        try:
            await self.client.write_register(address, register, value)
        except ModbusError as err:
            raise HomeAssistantError(
                f"Failed to write to thermostat {address}: {err}"
            ) from err
        self._publish(address, replace(state, **changes))

    def _require_state(self, address: int) -> ThermostatState:
        if self.data is None or (state := self.data.get(address)) is None:
            raise HomeAssistantError(f"Thermostat {address} is not available")
        return state

    def _publish(self, address: int, state: ThermostatState) -> None:
        self.async_set_updated_data({**self.data, address: state})


def _minute_of_week(now: datetime) -> int:
    return ((now.isoweekday() - 1) * 24 + now.hour) * 60 + now.minute
