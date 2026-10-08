"""Access to the RS485 bus through Home Assistant's shared Modbus connections.

The thermostats are often wired on the same bus as unrelated Modbus devices
handled by other integrations. Everything here goes through the ``modbus``
integration's shared connections, so requests from all integrations are
serialized on one link instead of colliding on the wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import logging
from typing import Any

from modbus_connection import (
    IllegalDataAddressError,
    ModbusProtocolError,
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTimeoutError,
    ModbusUnit,
)

from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback

from .const import (
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
    DEFAULT_BAUDRATE,
    DEFAULT_MESSAGE_DELAY,
    DEFAULT_PARITY,
    DEFAULT_STOPBITS,
    DEFAULT_TIMEOUT,
    REG_HOUR,
    REG_LOCK,
    REG_MINUTE,
    REG_MODE,
    REG_POWER,
    REG_WEEKDAY,
    REGISTER_COUNT,
    REQUEST_ATTEMPTS_PER_BURST,
    REQUEST_BURST_PAUSES,
)

_LOGGER = logging.getLogger(__name__)

type ModbusParams = ModbusSerialParams | ModbusTcpParams

# The line speed Home Assistant's modbus integration gives an RTU link carried
# over a plain socket. The serial settings mean nothing on a socket, but they
# must match for two integrations to share the connection to one server.
SOCKET_BAUDRATE = 115200


def _url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://[{host}]:{port}" if ":" in host else f"{scheme}://{host}:{port}"


def build_params(data: Mapping[str, Any]) -> ModbusParams:
    """Return the link parameters for config entry data."""
    conn = data[CONF_CONNECTION_TYPE]
    if conn == CONN_MODBUS_TCP:
        return ModbusTcpParams(host=data[CONF_HOST], port=data[CONF_PORT])
    if conn == CONN_RTU_OVER_TCP:
        return ModbusSerialParams(
            device=_url("socket", data[CONF_HOST], data[CONF_PORT]),
            baudrate=SOCKET_BAUDRATE,
        )
    if conn == CONN_SERIAL:
        device = data[CONF_DEVICE]
    elif conn == CONN_RFC2217:
        device = _url("rfc2217", data[CONF_HOST], data[CONF_PORT])
    else:
        raise ValueError(f"Unknown connection type {conn}")
    return ModbusSerialParams(
        device=device,
        baudrate=data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
        parity=data.get(CONF_PARITY, DEFAULT_PARITY),
        stopbits=data.get(CONF_STOPBITS, DEFAULT_STOPBITS),
    )


def configure_unit(unit: ModbusUnit, options: Mapping[str, Any]) -> Callable[[], None]:
    """Apply the timing options to one of our units; returns an undo callback.

    Message spacing only paces requests to this unit, so it never slows down
    other devices on the bus. The timeout is a minimum for the whole link:
    another integration that asks for a longer one still gets it.
    """
    unit.set_message_spacing(
        options.get(CONF_MESSAGE_DELAY, DEFAULT_MESSAGE_DELAY) / 1000
    )
    undo_timeout = require_timeout(unit, options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))

    def undo() -> None:
        unit.set_message_spacing(0)
        undo_timeout()

    return undo


def require_timeout(unit: ModbusUnit, timeout: float) -> Callable[[], None]:
    """Ask the link for a response timeout; returns an undo callback.

    ``require_timeout()`` arrived in modbus-connection 4.12 (Home Assistant
    2026.10); before that every link uses the library's 10 second timeout.
    """
    if not hasattr(unit, "require_timeout"):
        return lambda: None
    unit.require_timeout(timeout)
    return lambda: unit.require_timeout(None)


async def _async_request[T](unit: ModbusUnit, request: Callable[[], Awaitable[T]]) -> T:
    """Send a request, repeating it when the answer is lost or garbled.

    Tries come in bursts separated by pauses, so a thermostat that is silent
    for a few seconds gets asked again once it listens. The bus is free for
    other devices during the pauses.
    """
    bursts = len(REQUEST_BURST_PAUSES) + 1
    for burst, pause in enumerate((0.0, *REQUEST_BURST_PAUSES), start=1):
        if pause:
            _LOGGER.debug("No answer, trying again in %s s (burst %s/%s)", pause, burst, bursts)
            await asyncio.sleep(pause)
        for attempt in range(1, REQUEST_ATTEMPTS_PER_BURST + 1):
            try:
                return await request()
            except (ModbusTimeoutError, ModbusProtocolError) as err:
                # A link that is down won't come back by asking again.
                last_try = burst == bursts and attempt == REQUEST_ATTEMPTS_PER_BURST
                if last_try or not unit.connected:
                    raise
                _LOGGER.debug("Attempt %s of burst %s failed: %s", attempt, burst, err)
    raise AssertionError("unreachable")


async def async_read_thermostat(unit: ModbusUnit) -> list[int]:
    """Read the register block, falling back for firmware without register 9."""
    try:
        return await _async_request(
            unit, lambda: unit.read_holding_registers(0, REGISTER_COUNT)
        )
    except IllegalDataAddressError:
        return await _async_request(
            unit, lambda: unit.read_holding_registers(0, REGISTER_COUNT - 1)
        )


async def async_write_register(unit: ModbusUnit, register: int, value: int) -> None:
    """Write one register (function 0x06). Repeating it is harmless."""
    await _async_request(unit, lambda: unit.write_register(register, value & 0xFFFF))


def looks_like_thermostat(regs: list[int]) -> bool:
    """Return True if the registers are plausible for a PTB thermostat.

    Guards against adding some other device that happens to answer on the
    entered address of a shared bus.
    """
    return (
        regs[REG_POWER] in (0, 1)
        and regs[REG_MODE] in (0, 1, 2)
        and regs[REG_LOCK] in (0, 1)
        and regs[REG_MINUTE] < 60
        and regs[REG_HOUR] < 24
        and regs[REG_WEEKDAY] <= 7
    )


@dataclass
class BusUsage:
    """What else in Home Assistant uses the same link."""

    entry_units: dict[str, list[int]] = field(default_factory=dict)
    """Unit ids held by other config entries, keyed by entry id."""
    yaml_hubs: dict[str, list[int] | None] = field(default_factory=dict)
    """Modbus YAML hubs on the same port or host, with their unit ids if known."""


@callback
def async_get_bus_usage(
    hass: HomeAssistant, params: ModbusParams, exclude_entry_id: str | None = None
) -> BusUsage:
    """Find other users of the link ``params`` describes (best effort)."""
    usage = BusUsage()
    endpoint = params.endpoint
    try:
        # Added in Home Assistant 2026.10.
        from homeassistant.components.modbus.connection import (  # noqa: PLC0415
            async_get_connection_info,
        )
    except ImportError:
        pass
    else:
        for info in async_get_connection_info(hass):
            if tuple(info.endpoint) != endpoint:
                continue
            for entry_id, units in info.units.items():
                if entry_id != exclude_entry_id:
                    usage.entry_units[entry_id] = list(units)

    # Hubs configured in YAML open a link of their own instead of sharing one.
    for name, hub in (hass.data.get("modbus") or {}).items():
        if _hub_endpoint(hub) == endpoint:
            units = getattr(hub, "units", None)
            usage.yaml_hubs[name] = list(units) if units is not None else None
    return usage


def _hub_endpoint(hub: Any) -> tuple | None:
    if (endpoint := getattr(hub, "endpoint", None)) is not None:
        return tuple(endpoint)
    # Home Assistant 2026.9 hubs don't expose an endpoint yet.
    params = getattr(hub, "_pb_params", None)
    if not isinstance(params, dict):
        return None
    if "host" not in params:
        return ("serial", params.get("port"))
    host, port = str(params["host"]).lower(), params.get("port")
    if str(getattr(params.get("framer"), "value", params.get("framer"))) == "rtu":
        return ("serial", _url("socket", host, port))
    return ("tcp", host, port)
