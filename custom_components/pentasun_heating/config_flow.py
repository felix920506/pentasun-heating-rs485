"""Config flow for the Pentasun floor heating integration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
import logging
from typing import Any

from modbus_connection import ModbusConnectionError, ModbusError, ModbusUnit
import voluptuous as vol

from homeassistant.components import usb
from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_DEVICE,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    BAUDRATES,
    CONF_ADDRESSES,
    CONF_AUTO_SYNC_CLOCK,
    CONF_BAUDRATE,
    CONF_CONNECTION_TYPE,
    CONF_MAX_TEMP,
    CONF_MESSAGE_DELAY,
    CONF_MIN_TEMP,
    CONF_NAMES,
    CONF_PARITY,
    CONF_STOPBITS,
    CONF_TIMEOUT,
    CONN_MODBUS_TCP,
    CONN_RFC2217,
    CONN_RTU_OVER_TCP,
    CONN_SERIAL,
    CONNECTION_TYPES,
    DEFAULT_AUTO_SYNC_CLOCK,
    DEFAULT_BAUDRATE,
    DEFAULT_MAX_TEMP,
    DEFAULT_MESSAGE_DELAY,
    DEFAULT_MIN_TEMP,
    DEFAULT_MODBUS_TCP_PORT,
    DEFAULT_PARITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_RANGE,
    DEFAULT_STOPBITS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MAX_ADDRESS,
    REG_POWER,
    MIN_ADDRESS,
    SETPOINT_MAX,
    SETPOINT_MIN,
)
from .bus import (
    ScanResult,
    async_get_bus_usage,
    async_read_thermostat,
    async_scan_bus,
    async_write_verified,
    build_params,
    default_timeout,
    looks_like_thermostat,
    require_timeout,
)
from .coordinator import PentasunConfigEntry

_LOGGER = logging.getLogger(__name__)

CONF_SKIP_CHECK = "skip_check"
CONF_SCAN_RANGE = "scan_range"
CONF_SCAN_COUNT = "scan_count"
CONF_SCAN_TIMEOUT = "scan_timeout"
CONF_TOGGLE_AGAIN = "toggle_again"

DEFAULT_OPTIONS: dict[str, Any] = {
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_TIMEOUT: DEFAULT_TIMEOUT,
    CONF_MESSAGE_DELAY: DEFAULT_MESSAGE_DELAY,
    CONF_MIN_TEMP: DEFAULT_MIN_TEMP,
    CONF_MAX_TEMP: DEFAULT_MAX_TEMP,
    CONF_AUTO_SYNC_CLOCK: DEFAULT_AUTO_SYNC_CLOCK,
}


class InvalidAddresses(ValueError):
    """The address list could not be parsed."""


def parse_addresses(text: str) -> list[int]:
    """Parse "1, 2, 5-8" into a sorted list of unique Modbus addresses."""
    addresses: set[int] = set()
    for part in text.replace(";", ",").replace(" ", ",").split(","):
        if not (part := part.strip()):
            continue
        try:
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
            else:
                start = end = int(part)
        except ValueError as err:
            raise InvalidAddresses(part) from err
        if not MIN_ADDRESS <= start <= end <= MAX_ADDRESS:
            raise InvalidAddresses(part)
        addresses.update(range(start, end + 1))
    if not addresses:
        raise InvalidAddresses(text)
    return sorted(addresses)


def format_addresses(addresses: list[int]) -> str:
    """Format a list of addresses for display in a text field."""
    return ", ".join(str(address) for address in sorted(addresses))


async def _async_list_serial_ports(hass: HomeAssistant) -> list[selector.SelectOptionDict]:
    """List serial ports, preferring the stable /dev/serial/by-id paths."""
    options: list[selector.SelectOptionDict] = []
    for port in await usb.async_scan_serial_ports(hass):
        path = await hass.async_add_executor_job(usb.get_serial_by_id, port.device)
        label = usb.human_readable_device_name(
            port.device,
            port.serial_number,
            port.manufacturer,
            port.description,
            getattr(port, "vid", None),
            getattr(port, "pid", None),
        )
        options.append(selector.SelectOptionDict(value=path, label=label))
    return options


def _serial_settings_schema(defaults: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Required(
            CONF_BAUDRATE, default=str(defaults.get(CONF_BAUDRATE, DEFAULT_BAUDRATE))
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[str(rate) for rate in BAUDRATES],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(
            CONF_PARITY, default=defaults.get(CONF_PARITY, DEFAULT_PARITY)
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["N", "E", "O"],
                translation_key="parity",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(
            CONF_STOPBITS, default=str(defaults.get(CONF_STOPBITS, DEFAULT_STOPBITS))
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["1", "2"], mode=selector.SelectSelectorMode.DROPDOWN
            )
        ),
    }


def _network_schema(
    defaults: Mapping[str, Any], default_port: int | None = None
) -> dict[vol.Marker, Any]:
    port = defaults.get(CONF_PORT, default_port)
    return {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)): str,
        vol.Required(
            CONF_PORT, default=port if port is not None else vol.UNDEFINED
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }


def _normalize(conn: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Convert selector output to the types stored in the config entry."""
    data: dict[str, Any] = {CONF_CONNECTION_TYPE: conn}
    if conn == CONN_SERIAL:
        data[CONF_DEVICE] = user_input[CONF_DEVICE].strip()
    else:
        data[CONF_HOST] = user_input[CONF_HOST].strip()
        data[CONF_PORT] = int(user_input[CONF_PORT])
    if conn in (CONN_SERIAL, CONN_RFC2217):
        data[CONF_BAUDRATE] = int(user_input[CONF_BAUDRATE])
        data[CONF_PARITY] = user_input[CONF_PARITY]
        data[CONF_STOPBITS] = int(user_input[CONF_STOPBITS])
    return data


def _unique_id(data: Mapping[str, Any]) -> str:
    if data[CONF_CONNECTION_TYPE] == CONN_SERIAL:
        return data[CONF_DEVICE]
    return f"{data[CONF_HOST]}:{data[CONF_PORT]}"


def _title(data: Mapping[str, Any]) -> str:
    return f"Pentasun ({_unique_id(data)})"


PROBE_PLACEHOLDERS = ("addresses", "users", "error")


async def _async_probe(
    hass: HomeAssistant,
    data: Mapping[str, Any],
    addresses: list[int],
    exclude_entry_id: str | None = None,
    *,
    read: bool = True,
) -> tuple[str | None, dict[str, str]]:
    """Check the addresses on the bus; return an error key and placeholders.

    Requests go over the modbus integration's shared connection, so probing
    a bus other integrations already use doesn't disturb them. The addresses
    are checked to be unused by other integrations first, so the timeout
    requested for a probed unit can't clobber someone else's and is withdrawn
    right after.
    """
    placeholders = dict.fromkeys(PROBE_PLACEHOLDERS, "")
    params = build_params(data)

    # Another integration already talks to a device at one of these addresses.
    usage = async_get_bus_usage(hass, params, exclude_entry_id)
    users: dict[int, str] = {}
    for entry_id, units in usage.entry_units.items():
        other = hass.config_entries.async_get_entry(entry_id)
        name = f"{other.title} ({other.domain})" if other else entry_id
        users.update((unit, name) for unit in units)
    for hub, units in usage.yaml_hubs.items():
        users.update((unit, f"Modbus YAML hub {hub}") for unit in units or ())
    if taken := [address for address in addresses if address in users]:
        placeholders["addresses"] = format_addresses(taken)
        placeholders["users"] = ", ".join(sorted({users[a] for a in taken}))
        return "address_in_use", placeholders
    if not read:
        return None, placeholders

    missing: list[int] = []
    foreign: list[int] = []
    for address in addresses:
        try:
            async with async_get_temporary_unit(hass, params, address) as unit:
                undo_timeout = require_timeout(unit, default_timeout(data))
                try:
                    regs = await async_read_thermostat(unit)
                except ModbusError as err:
                    _LOGGER.debug("Thermostat %s: %s", address, err)
                    if not unit.connected:
                        return "cannot_connect", placeholders
                    missing.append(address)
                    continue
                finally:
                    undo_timeout()
        except HomeAssistantError as err:
            # The port or host is in use with different serial settings.
            placeholders["error"] = str(err)
            return "link_conflict", placeholders
        if not looks_like_thermostat(regs):
            _LOGGER.debug("Address %s does not look like a thermostat: %s", address, regs)
            foreign.append(address)

    if missing:
        placeholders["addresses"] = format_addresses(missing)
        return "no_response", placeholders
    if foreign:
        placeholders["addresses"] = format_addresses(foreign)
        return "not_thermostat", placeholders
    return None, placeholders


@asynccontextmanager
async def _async_flow_unit(
    hass: HomeAssistant, data: Mapping[str, Any], address: int
) -> AsyncIterator[ModbusUnit]:
    """Hold a unit for a flow step, with our response timeout."""
    async with async_get_temporary_unit(hass, build_params(data), address) as unit:
        undo_timeout = require_timeout(unit, default_timeout(data))
        try:
            yield unit
        finally:
            undo_timeout()


class _IdentifySteps:
    """Steps that switch each thermostat on or off in turn so it can be named.

    The thermostats only know their bus address, so this is how to find out
    which room each one is in. Every thermostat's power is restored before
    moving on to the next, and when the flow is closed half way.
    """

    hass: Any
    _identify_queue: list[int]
    _identify_total: int = 0
    _identify_names: dict[str, str]
    _identify_original: bool | None = None
    """The power state of the thermostat being identified, before toggling."""
    _identify_toggled: bool = False

    def _identify_data(self) -> Mapping[str, Any]:
        """Return the connection data."""
        raise NotImplementedError

    def _identify_addresses(self) -> list[int]:
        """Return the addresses to identify."""
        raise NotImplementedError

    async def _async_finish_naming(self, names: dict[str, str]) -> ConfigFlowResult:
        """Save the names (possibly none) and finish the flow."""
        raise NotImplementedError

    async def async_step_name_thermostats(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to identify and name the thermostats."""
        return self.async_show_menu(
            step_id="name_thermostats", menu_options=["identify", "finish"]
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish without naming."""
        return await self._async_finish_naming({})

    async def async_step_identify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Toggle one thermostat at a time and ask for its name."""
        errors: dict[str, str] = {}
        if user_input is None:
            self._identify_queue = list(self._identify_addresses())
            self._identify_total = len(self._identify_queue)
            self._identify_names = {}
            if not await self._async_identify_toggle():
                errors["base"] = "identify_failed"
        elif user_input.get(CONF_TOGGLE_AGAIN):
            if not await self._async_identify_toggle():
                errors["base"] = "identify_failed"
        else:
            address = self._identify_queue.pop(0)
            await self._async_identify_restore(address)
            if name := user_input.get(CONF_NAME, "").strip():
                self._identify_names[str(address)] = name
            if not self._identify_queue:
                return await self._async_finish_naming(self._identify_names)
            if not await self._async_identify_toggle():
                errors["base"] = "identify_failed"

        address = self._identify_queue[0]
        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): str,
                vol.Optional(CONF_TOGGLE_AGAIN, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="identify",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "address": str(address),
                "index": str(self._identify_total - len(self._identify_queue) + 1),
                "total": str(self._identify_total),
            },
        )

    async def _async_identify_toggle(self) -> bool:
        """Switch the current thermostat's power; returns False if that failed."""
        address = self._identify_queue[0]
        try:
            async with _async_flow_unit(self.hass, self._identify_data(), address) as unit:
                if self._identify_original is None:
                    regs = await async_read_thermostat(unit)
                    self._identify_original = bool(regs[REG_POWER])
                power = self._identify_original if self._identify_toggled else (
                    not self._identify_original
                )
                kept, _ = await async_write_verified(unit, REG_POWER, int(power))
        except (ModbusError, HomeAssistantError) as err:
            _LOGGER.debug("Cannot toggle thermostat %s: %s", address, err)
            return False
        if kept:
            self._identify_toggled = not self._identify_toggled
        return kept

    async def _async_identify_restore(self, address: int) -> None:
        """Put the thermostat's power back as it was."""
        original, toggled = self._identify_original, self._identify_toggled
        self._identify_original, self._identify_toggled = None, False
        if original is None or not toggled:
            return
        try:
            async with _async_flow_unit(self.hass, self._identify_data(), address) as unit:
                await async_write_verified(unit, REG_POWER, int(original))
        except (ModbusError, HomeAssistantError) as err:
            _LOGGER.warning(
                "Could not switch thermostat %s back %s: %s",
                address, "on" if original else "off", err,
            )

    @callback
    def async_remove(self) -> None:
        """Restore the thermostat being identified when the flow is closed."""
        if self._identify_toggled and self._identify_queue:
            self.hass.async_create_background_task(
                self._async_identify_restore(self._identify_queue[0]),
                f"{DOMAIN} restore identified thermostat",
            )


class _ScanSteps:
    """Bus scan steps shared by the config flow and the options flow."""

    hass: Any
    _scan_task: asyncio.Task[ScanResult] | None = None
    _scan_range: list[int]
    _scan_count: int | None = None
    _scan_timeout: float | None = None
    _scan_error: str | None = None
    _scan_error_detail: str = ""
    _scan_result: ScanResult | None = None

    def _scan_target(self) -> tuple[Mapping[str, Any], str | None, list[int]]:
        """Return the connection data, own entry id and already known addresses."""
        raise NotImplementedError

    def _scan_default_timeout(self) -> float:
        """Return the suggested scan timeout for a Modbus TCP gateway."""
        return default_timeout(self._scan_target()[0])

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which addresses to scan."""
        errors: dict[str, str] = {}
        if self._scan_error:
            errors["base"], self._scan_error = self._scan_error, None
        if user_input is not None:
            try:
                self._scan_range = parse_addresses(user_input[CONF_SCAN_RANGE])
            except InvalidAddresses:
                errors[CONF_SCAN_RANGE] = "invalid_addresses"
            else:
                count = user_input.get(CONF_SCAN_COUNT)
                self._scan_count = int(count) if count else None
                self._scan_timeout = user_input.get(CONF_SCAN_TIMEOUT)
                return await self.async_step_scan_progress()
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_RANGE, default=DEFAULT_SCAN_RANGE): str,
                vol.Optional(CONF_SCAN_COUNT): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=255, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
        if self._scan_target()[0][CONF_CONNECTION_TYPE] == CONN_MODBUS_TCP:
            schema = schema.extend(
                {
                    vol.Required(
                        CONF_SCAN_TIMEOUT, default=self._scan_default_timeout()
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.2,
                            max=30,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    )
                }
            )
        return self.async_show_form(
            step_id="scan",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"error": self._scan_error_detail},
        )

    async def async_step_scan_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan the bus in the background, showing progress."""
        if self._scan_task is None:
            data, exclude_entry_id, known = self._scan_target()
            addresses = [a for a in self._scan_range if a not in known]
            self._scan_task = self.hass.async_create_task(
                async_scan_bus(
                    self.hass,
                    build_params(data),
                    addresses,
                    exclude_entry_id=exclude_entry_id,
                    expected=self._scan_count,
                    timeout=self._scan_timeout,
                    on_progress=self.async_update_progress,
                ),
                f"{DOMAIN} bus scan",
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan_progress",
                progress_action="scan",
                progress_task=self._scan_task,
                description_placeholders={
                    "range": format_addresses(self._scan_range)
                    if len(self._scan_range) < 6
                    else f"{self._scan_range[0]}-{self._scan_range[-1]}"
                },
            )
        task, self._scan_task = self._scan_task, None
        try:
            self._scan_result = task.result()
        except ModbusConnectionError as err:
            _LOGGER.debug("Scan failed: %s", err)
            self._scan_error = "cannot_connect"
            return self.async_show_progress_done(next_step_id="scan")
        except HomeAssistantError as err:
            self._scan_error, self._scan_error_detail = "link_conflict", str(err)
            return self.async_show_progress_done(next_step_id="scan")
        return self.async_show_progress_done(next_step_id="scan_result")

    def _scan_placeholders(self) -> dict[str, str]:
        result = self._scan_result or ScanResult()
        return {
            "found": format_addresses(result.thermostats) or "-",
            "other": format_addresses(result.other_devices) or "-",
            "skipped": format_addresses(result.skipped) or "-",
            "found_count": str(len(result.thermostats)),
            "expected": str(result.expected or ""),
            "garbled": format_addresses(result.garbled) or "-",
        }

    def _scan_errors(self, user_input: dict[str, Any] | None) -> dict[str, str]:
        """Warn when the scan found fewer thermostats than the user expected."""
        if user_input is None and self._scan_result and self._scan_result.missing:
            return {"base": "missing_thermostats"}
        return {}


class PentasunConfigFlow(_ScanSteps, _IdentifySteps, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a thermostat bus."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: PentasunConfigEntry) -> PentasunOptionsFlow:
        """Return the options flow."""
        return PentasunOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how the RS485 bus is connected."""
        return self.async_show_menu(step_id="user", menu_options=CONNECTION_TYPES)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change how the bus is connected."""
        return self.async_show_menu(step_id="reconfigure", menu_options=CONNECTION_TYPES)

    def _defaults(self, conn: str) -> Mapping[str, Any]:
        """Prefill the form with the current settings when reconfiguring."""
        if self.source == SOURCE_RECONFIGURE:
            data = self._get_reconfigure_entry().data
            if data[CONF_CONNECTION_TYPE] == conn:
                return data
        return {}

    async def async_step_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a local RS485 adapter."""
        return await self._async_connection_step(CONN_SERIAL, user_input)

    async def async_step_rfc2217(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a serial device server in RFC 2217 mode."""
        return await self._async_connection_step(CONN_RFC2217, user_input)

    async def async_step_rtu_over_tcp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a serial device server in raw TCP socket mode."""
        return await self._async_connection_step(CONN_RTU_OVER_TCP, user_input)

    async def async_step_modbus_tcp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a Modbus TCP to RTU gateway."""
        return await self._async_connection_step(CONN_MODBUS_TCP, user_input)

    async def _async_connection_schema(self, conn: str) -> vol.Schema:
        defaults = self._defaults(conn)
        if conn == CONN_SERIAL:
            ports = await _async_list_serial_ports(self.hass)
            default_port = ports[0]["value"] if ports else vol.UNDEFINED
            schema = {
                vol.Required(
                    CONF_DEVICE, default=defaults.get(CONF_DEVICE, default_port)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=ports,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                **_serial_settings_schema(defaults),
            }
        elif conn == CONN_RFC2217:
            schema = {**_network_schema(defaults), **_serial_settings_schema(defaults)}
        elif conn == CONN_MODBUS_TCP:
            schema = _network_schema(defaults, DEFAULT_MODBUS_TCP_PORT)
        else:
            schema = _network_schema(defaults)
        return vol.Schema(schema)

    async def _async_connection_step(
        self, conn: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = dict.fromkeys(PROBE_PLACEHOLDERS, "")
        if user_input is not None:
            self._data = _normalize(conn, user_input)
            await self.async_set_unique_id(_unique_id(self._data))
            if self.source != SOURCE_RECONFIGURE:
                self._abort_if_unique_id_configured()
                return await self.async_step_add_thermostats()

            entry = self._get_reconfigure_entry()
            if self.unique_id != entry.unique_id:
                self._abort_if_unique_id_configured()
            addresses = entry.options[CONF_ADDRESSES]
            error, placeholders = await _async_probe(
                self.hass, self._data, addresses, entry.entry_id
            )
            # Accept the new connection as long as any thermostat answers.
            if error == "no_response":
                all_missing = placeholders["addresses"] == format_addresses(addresses)
                error = "no_devices" if all_missing else None
            if not error:
                # Switching to a gateway needs a timeout longer than the gateway's.
                timeout = max(
                    entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
                    default_timeout(self._data),
                )
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=self.unique_id,
                    title=_title(self._data),
                    data=self._data,
                    options={**entry.options, CONF_TIMEOUT: timeout},
                )
            errors["base"] = error

        schema = await self._async_connection_schema(conn)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id=conn,
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_add_thermostats(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan the bus for thermostats or enter their addresses."""
        return self.async_show_menu(
            step_id="add_thermostats", menu_options=["scan", "thermostats"]
        )

    def _scan_target(self) -> tuple[Mapping[str, Any], str | None, list[int]]:
        return self._data, None, []

    def _identify_data(self) -> Mapping[str, Any]:
        return self._data

    def _identify_addresses(self) -> list[int]:
        return list(self._options[CONF_ADDRESSES])

    async def _async_finish_naming(self, names: dict[str, str]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_title(self._data),
            data=self._data,
            options={**self._options, CONF_NAMES: names},
        )

    async def async_step_thermostats(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the Modbus addresses of the thermostats on the bus."""
        return await self._async_addresses_step("thermostats", user_input, "1")

    async def async_step_scan_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the thermostats the scan found."""
        found = self._scan_result.thermostats if self._scan_result else []
        return await self._async_addresses_step(
            "scan_result",
            user_input,
            format_addresses(found),
            self._scan_placeholders(),
            self._scan_errors(user_input),
        )

    async def _async_addresses_step(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        default: str,
        extra_placeholders: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        errors = dict(errors or {})
        placeholders = dict.fromkeys(PROBE_PLACEHOLDERS, "")
        if user_input is not None:
            try:
                addresses = parse_addresses(user_input[CONF_ADDRESSES])
            except InvalidAddresses:
                errors[CONF_ADDRESSES] = "invalid_addresses"
            else:
                error, placeholders = await _async_probe(
                    self.hass,
                    self._data,
                    addresses,
                    read=not user_input.get(CONF_SKIP_CHECK),
                )
                if error:
                    errors["base"] = error
                else:
                    self._options = {
                        **DEFAULT_OPTIONS,
                        CONF_TIMEOUT: default_timeout(self._data),
                        CONF_ADDRESSES: addresses,
                    }
                    return await self.async_step_name_thermostats()

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESSES, default=default): str,
                vol.Optional(CONF_SKIP_CHECK, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders=placeholders | (extra_placeholders or {}),
        )


class PentasunOptionsFlow(_ScanSteps, _IdentifySteps, OptionsFlowWithReload):
    """Change thermostat addresses and polling settings, or scan for more."""

    _options: dict[str, Any]
    _new_addresses: list[int] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between the settings, a bus scan and naming."""
        self._options = dict(self.config_entry.options)
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "scan", "identify"]
        )

    def _identify_data(self) -> Mapping[str, Any]:
        return self.config_entry.data

    def _identify_addresses(self) -> list[int]:
        if self._new_addresses is not None:
            return self._new_addresses
        return list(self._options[CONF_ADDRESSES])

    async def _async_finish_naming(self, names: dict[str, str]) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self._options, CONF_NAMES: {**self._options.get(CONF_NAMES, {}), **names}}
        )

    def _scan_target(self) -> tuple[Mapping[str, Any], str | None, list[int]]:
        entry = self.config_entry
        return entry.data, entry.entry_id, list(entry.options[CONF_ADDRESSES])

    def _scan_default_timeout(self) -> float:
        entry = self.config_entry
        return max(
            entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT), default_timeout(entry.data)
        )

    async def async_step_scan_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add the thermostats the scan found to the existing ones."""
        entry = self.config_entry
        errors = self._scan_errors(user_input)
        placeholders = dict.fromkeys(PROBE_PLACEHOLDERS, "") | self._scan_placeholders()
        if user_input is not None:
            try:
                addresses = parse_addresses(user_input[CONF_ADDRESSES])
            except InvalidAddresses:
                errors[CONF_ADDRESSES] = "invalid_addresses"
            else:
                error, probe = await _async_probe(
                    self.hass, entry.data, addresses, entry.entry_id, read=False
                )
                placeholders |= probe
                if error:
                    errors[CONF_ADDRESSES] = error
                else:
                    known = set(entry.options[CONF_ADDRESSES])
                    self._options = {**entry.options, CONF_ADDRESSES: addresses}
                    self._new_addresses = [a for a in addresses if a not in known]
                    if not self._new_addresses:
                        return await self._async_finish_naming({})
                    return await self.async_step_name_thermostats()
        found = self._scan_result.thermostats if self._scan_result else []
        default = format_addresses(sorted({*entry.options[CONF_ADDRESSES], *found}))
        schema = vol.Schema({vol.Required(CONF_ADDRESSES, default=default): str})
        return self.async_show_form(
            step_id="scan_result",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        placeholders = dict.fromkeys(PROBE_PLACEHOLDERS, "")
        entry = self.config_entry
        options = entry.options
        if user_input is not None:
            try:
                addresses = parse_addresses(user_input[CONF_ADDRESSES])
            except InvalidAddresses:
                errors[CONF_ADDRESSES] = "invalid_addresses"
            else:
                error, placeholders = await _async_probe(
                    self.hass, entry.data, addresses, entry.entry_id, read=False
                )
                if error:
                    errors[CONF_ADDRESSES] = error
                elif user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
                    errors[CONF_MAX_TEMP] = "invalid_temp_range"
                else:
                    return self.async_create_entry(
                        data={
                            **entry.options,
                            **user_input,
                            CONF_ADDRESSES: addresses,
                            CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                            CONF_MESSAGE_DELAY: int(user_input[CONF_MESSAGE_DELAY]),
                        }
                    )

        def number(
            minimum: float, maximum: float, step: float, unit: str | None = None
        ) -> selector.NumberSelector:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=minimum,
                    max=maximum,
                    step=step,
                    unit_of_measurement=unit,
                    mode=selector.NumberSelectorMode.BOX,
                )
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESSES): str,
                vol.Required(CONF_SCAN_INTERVAL): number(5, 3600, 1, "s"),
                vol.Required(CONF_TIMEOUT): number(0.2, 30, 0.1, "s"),
                vol.Required(CONF_MESSAGE_DELAY): number(0, 1000, 10, "ms"),
                vol.Required(CONF_MIN_TEMP): number(SETPOINT_MIN, SETPOINT_MAX, 0.5, "°C"),
                vol.Required(CONF_MAX_TEMP): number(SETPOINT_MIN, SETPOINT_MAX, 0.5, "°C"),
                vol.Required(CONF_AUTO_SYNC_CLOCK): bool,
            }
        )
        suggested = {
            **DEFAULT_OPTIONS,
            **options,
            CONF_ADDRESSES: format_addresses(options.get(CONF_ADDRESSES, [])),
        }
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or suggested
            ),
            errors=errors,
            description_placeholders=placeholders,
        )
