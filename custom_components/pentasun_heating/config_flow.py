"""Config flow for the Pentasun floor heating integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
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
    DEFAULT_STOPBITS,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MAX_ADDRESS,
    MIN_ADDRESS,
)
from .coordinator import PentasunConfigEntry, async_read_thermostat, create_client
from .modbus import ModbusConnectionError, ModbusError

_LOGGER = logging.getLogger(__name__)

CONF_SKIP_CHECK = "skip_check"

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


def _list_serial_ports() -> list[selector.SelectOptionDict]:
    """List serial ports, preferring the stable /dev/serial/by-id paths."""
    from serial.tools import list_ports  # noqa: PLC0415

    by_id: dict[str, str] = {}
    by_id_dir = "/dev/serial/by-id"
    if os.path.isdir(by_id_dir):
        for name in os.listdir(by_id_dir):
            link = os.path.join(by_id_dir, name)
            by_id[os.path.realpath(link)] = link

    options: list[selector.SelectOptionDict] = []
    for port in sorted(list_ports.comports(), key=lambda p: p.device):
        path = by_id.get(os.path.realpath(port.device), port.device)
        details = ", ".join(
            part
            for part in (port.description, port.manufacturer, port.serial_number)
            if part and part != "n/a"
        )
        label = f"{port.device} - {details}" if details else port.device
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


async def _async_probe(
    data: Mapping[str, Any], options: Mapping[str, Any], addresses: list[int]
) -> tuple[str | None, list[int]]:
    """Try to read every address.

    Returns an error key (or None) and the list of addresses that did not answer.
    """
    client = create_client(data, options)
    missing: list[int] = []
    try:
        for address in addresses:
            try:
                await async_read_thermostat(client, address)
            except ModbusConnectionError as err:
                _LOGGER.debug("Connection failed: %s", err)
                return "cannot_connect", addresses
            except ModbusError as err:
                _LOGGER.debug("Thermostat %s did not respond: %s", address, err)
                missing.append(address)
    finally:
        await client.close()
    return ("no_response" if missing else None), missing


class PentasunConfigFlow(ConfigFlow, domain=DOMAIN):
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
            ports = await self.hass.async_add_executor_job(_list_serial_ports)
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
        if user_input is not None:
            self._data = _normalize(conn, user_input)
            await self.async_set_unique_id(_unique_id(self._data))
            if self.source != SOURCE_RECONFIGURE:
                self._abort_if_unique_id_configured()
                return await self.async_step_thermostats()

            entry = self._get_reconfigure_entry()
            if self.unique_id != entry.unique_id:
                self._abort_if_unique_id_configured()
            addresses = entry.options[CONF_ADDRESSES]
            error, missing = await _async_probe(self._data, entry.options, addresses)
            # Accept the new connection as long as any thermostat answers.
            if error == "cannot_connect" or len(missing) == len(addresses):
                errors["base"] = "cannot_connect" if error == "cannot_connect" else "no_devices"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=self.unique_id,
                    title=_title(self._data),
                    data=self._data,
                )

        schema = await self._async_connection_schema(conn)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id=conn, data_schema=schema, errors=errors)

    async def async_step_thermostats(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the Modbus addresses of the thermostats on the bus."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"missing": ""}
        if user_input is not None:
            try:
                addresses = parse_addresses(user_input[CONF_ADDRESSES])
            except InvalidAddresses:
                errors[CONF_ADDRESSES] = "invalid_addresses"
            else:
                error = None
                if not user_input.get(CONF_SKIP_CHECK):
                    error, missing = await _async_probe(
                        self._data, DEFAULT_OPTIONS, addresses
                    )
                    placeholders["missing"] = format_addresses(missing)
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
                vol.Required(CONF_ADDRESSES, default="1"): str,
                vol.Optional(CONF_SKIP_CHECK, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="thermostats",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders=placeholders,
        )


class PentasunOptionsFlow(OptionsFlowWithReload):
    """Change thermostat addresses and polling settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            try:
                addresses = parse_addresses(user_input[CONF_ADDRESSES])
            except InvalidAddresses:
                errors[CONF_ADDRESSES] = "invalid_addresses"
            else:
                if user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
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
                vol.Required(CONF_TIMEOUT): number(0.2, 10, 0.1, "s"),
                vol.Required(CONF_MESSAGE_DELAY): number(0, 1000, 10, "ms"),
                vol.Required(CONF_MIN_TEMP): number(0, 40, 0.5, "°C"),
                vol.Required(CONF_MAX_TEMP): number(5, 60, 0.5, "°C"),
                vol.Required(CONF_AUTO_SYNC_CLOCK): bool,
            }
        )
        suggested = {
            **DEFAULT_OPTIONS,
            **options,
            CONF_ADDRESSES: format_addresses(options.get(CONF_ADDRESSES, [])),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or suggested
            ),
            errors=errors,
        )
