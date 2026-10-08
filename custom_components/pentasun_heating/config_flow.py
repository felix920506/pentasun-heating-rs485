"""Config flow for the Pentasun floor heating integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from modbus_connection import ModbusConnectionError, ModbusError
import voluptuous as vol

from homeassistant.components import usb
from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
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
    MIN_ADDRESS,
    SETPOINT_MAX,
    SETPOINT_MIN,
)
from .bus import (
    ScanResult,
    async_get_bus_usage,
    async_read_thermostat,
    async_scan_bus,
    build_params,
    looks_like_thermostat,
    require_timeout,
    scan_supported,
)
from .coordinator import PentasunConfigEntry

_LOGGER = logging.getLogger(__name__)

CONF_SKIP_CHECK = "skip_check"
CONF_SCAN_RANGE = "scan_range"
CONF_SCAN_COUNT = "scan_count"

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
                undo_timeout = require_timeout(unit, DEFAULT_TIMEOUT)
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


class _ScanSteps:
    """Bus scan steps shared by the config flow and the options flow."""

    hass: Any
    _scan_task: asyncio.Task[ScanResult] | None = None
    _scan_range: list[int]
    _scan_count: int | None = None
    _scan_error: str | None = None
    _scan_error_detail: str = ""
    _scan_result: ScanResult | None = None

    def _scan_target(self) -> tuple[Mapping[str, Any], str | None, list[int]]:
        """Return the connection data, own entry id and already known addresses."""
        raise NotImplementedError

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
        }


class PentasunConfigFlow(_ScanSteps, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a thermostat bus."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}

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
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=self.unique_id,
                    title=_title(self._data),
                    data=self._data,
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
        if not scan_supported():
            return await self.async_step_thermostats()
        return self.async_show_menu(
            step_id="add_thermostats", menu_options=["scan", "thermostats"]
        )

    def _scan_target(self) -> tuple[Mapping[str, Any], str | None, list[int]]:
        return self._data, None, []

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
            "scan_result", user_input, format_addresses(found), self._scan_placeholders()
        )

    async def _async_addresses_step(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        default: str,
        extra_placeholders: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
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
                    return self.async_create_entry(
                        title=_title(self._data),
                        data=self._data,
                        options={**DEFAULT_OPTIONS, CONF_ADDRESSES: addresses},
                    )

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


class PentasunOptionsFlow(_ScanSteps, OptionsFlowWithReload):
    """Change thermostat addresses and polling settings, or scan for more."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between the settings and a bus scan."""
        if not scan_supported():
            return await self.async_step_settings()
        return self.async_show_menu(step_id="init", menu_options=["settings", "scan"])

    def _scan_target(self) -> tuple[Mapping[str, Any], str | None, list[int]]:
        entry = self.config_entry
        return entry.data, entry.entry_id, list(entry.options[CONF_ADDRESSES])

    async def async_step_scan_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add the thermostats the scan found to the existing ones."""
        entry = self.config_entry
        errors: dict[str, str] = {}
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
                    return self.async_create_entry(
                        data={**entry.options, CONF_ADDRESSES: addresses}
                    )
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
