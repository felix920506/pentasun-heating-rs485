"""Access to the RS485 bus through Home Assistant's shared Modbus connections.

The thermostats are often wired on the same bus as unrelated Modbus devices
handled by other integrations. Everything here goes through the ``modbus``
integration's shared connections, so requests from all integrations are
serialized on one link instead of colliding on the wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
import logging
from typing import Any

from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusExceptionError,
    ModbusProtocolError,
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTimeoutError,
    ModbusUnit,
)

from homeassistant.components.modbus import async_get_temporary_unit
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
    DEFAULT_GATEWAY_TIMEOUT,
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
    SCAN_MISSING_TIME,
    SCAN_ROUNDS,
    SCAN_TIMEOUT,
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


def default_timeout(data: Mapping[str, Any]) -> float:
    """Return the default response timeout for a connection.

    A Modbus TCP gateway forwards one request at a time and drops requests that
    arrive while it still waits for a reply on the RS485 side, so the timeout
    must be longer than the gateway's own.
    """
    if data[CONF_CONNECTION_TYPE] == CONN_MODBUS_TCP:
        return DEFAULT_GATEWAY_TIMEOUT
    return DEFAULT_TIMEOUT


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


@dataclass
class ScanResult:
    """What a bus scan found."""

    thermostats: list[int] = field(default_factory=list)
    other_devices: list[int] = field(default_factory=list)
    """Addresses where a device answered that doesn't look like a thermostat."""
    skipped: list[int] = field(default_factory=list)
    """Addresses not scanned because another integration uses them."""
    garbled: list[int] = field(default_factory=list)
    """Addresses with corrupted replies, e.g. two devices sharing the address."""
    expected: int | None = None
    """How many thermostats the user said to look for."""

    @property
    def missing(self) -> int:
        """Return how many of the expected thermostats weren't found."""
        return max((self.expected or 0) - len(self.thermostats), 0)


async def _async_read_once(unit: ModbusUnit, timeout: float) -> list[int]:
    """One read of the register block, for scanning (no repeats).

    The request is also cut off here after ``timeout``: Home Assistant 2026.9
    can't shorten the link's 10 s timeout, and on later versions another
    integration may require a longer one. The library discards a reply that
    arrives after the request was cancelled.
    """
    try:
        async with asyncio.timeout(timeout):
            try:
                return await unit.read_holding_registers(0, REGISTER_COUNT)
            except IllegalDataAddressError:
                return await unit.read_holding_registers(0, REGISTER_COUNT - 1)
    except TimeoutError as err:
        raise ModbusTimeoutError(f"No answer within {timeout} s") from err


async def async_scan_bus(
    hass: HomeAssistant,
    params: ModbusParams,
    addresses: Iterable[int],
    *,
    exclude_entry_id: str | None = None,
    expected: int | None = None,
    timeout: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> ScanResult:
    """Look for thermostats on the bus.

    Every candidate address is asked once per round, for ``SCAN_ROUNDS``
    rounds, so the tries for one address are spread over the whole scan; an
    address drops out as soon as anything answers. Addresses other
    integrations use on this bus are never polled. Requests go over the
    shared connection, so other integrations keep working during a scan.

    With ``expected`` set, the scan stops once that many thermostats
    answered. If fewer did after the regular rounds, the silent addresses
    are asked for up to ``SCAN_MISSING_TIME`` seconds more.

    ``timeout`` replaces the short ``SCAN_TIMEOUT``; a Modbus TCP gateway
    needs one longer than its own.

    Raises ``ModbusConnectionError`` if the bus can't be reached and
    ``HomeAssistantError`` if the link is in use with other settings.
    """
    result = ScanResult(expected=expected)
    scan_timeout = timeout or SCAN_TIMEOUT
    usage = async_get_bus_usage(hass, params, exclude_entry_id)
    taken = {unit for units in usage.entry_units.values() for unit in units}
    taken |= {unit for units in usage.yaml_hubs.values() for unit in units or ()}
    remaining = [address for address in addresses if address not in taken]
    result.skipped = sorted(set(addresses) & taken)
    garbled: set[int] = set()
    loop = asyncio.get_running_loop()
    deadline: float | None = None

    def progress(scan_round: int, done: float) -> float:
        regular = min((scan_round + done) / SCAN_ROUNDS, 1)
        if not expected:
            return regular
        # The last quarter of the bar is the extra time for missing thermostats.
        if deadline is None:
            return 0.75 * regular
        return 1 - 0.25 * max(deadline - loop.time(), 0) / SCAN_MISSING_TIME

    def found_all() -> bool:
        return bool(expected) and len(result.thermostats) >= expected

    async with AsyncExitStack() as stack:
        units: dict[int, ModbusUnit] = {}
        for address in remaining:
            unit = await stack.enter_async_context(
                async_get_temporary_unit(hass, params, address)
            )
            # Withdrawn before the unit is released; nobody else uses these
            # addresses, so the requirement can't clobber another integration's.
            stack.callback(require_timeout(unit, scan_timeout))
            units[address] = unit

        scan_round = 0
        while remaining and not found_all():
            if deadline is not None and loop.time() >= deadline:
                break
            if scan_round >= SCAN_ROUNDS:
                if not expected:
                    break
                if deadline is None:
                    _LOGGER.debug(
                        "Scan: %s of %s thermostats found, still asking %s",
                        len(result.thermostats), expected, list(remaining),
                    )
                    deadline = loop.time() + SCAN_MISSING_TIME
            pending = list(remaining)
            for index, address in enumerate(pending):
                if deadline is not None and loop.time() >= deadline:
                    break
                if on_progress:
                    on_progress(progress(scan_round, index / len(pending)))
                unit = units[address]
                try:
                    regs = await _async_read_once(unit, scan_timeout)
                except ModbusProtocolError as err:
                    if not unit.connected:
                        raise ModbusConnectionError(str(err)) from err
                    # Two devices answering at once corrupt each other's reply.
                    _LOGGER.debug("Scan: garbled reply from address %s: %s", address, err)
                    garbled.add(address)
                    continue
                except ModbusTimeoutError as err:
                    if not unit.connected:
                        raise ModbusConnectionError(str(err)) from err
                    continue
                except ModbusExceptionError:
                    # Something answered, but refuses the thermostat registers.
                    regs = None
                remaining.remove(address)
                if regs is not None and looks_like_thermostat(regs):
                    result.thermostats.append(address)
                else:
                    result.other_devices.append(address)
                _LOGGER.debug("Scan: address %s answered %s", address, regs)
                if found_all():
                    _LOGGER.debug("Scan: found all %s thermostats", expected)
                    break
            scan_round += 1

    result.garbled = sorted(garbled)
    result.thermostats.sort()
    result.other_devices.sort()
    return result
