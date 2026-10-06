"""Tests for the config flow and entities."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from modbus_connection import ModbusSerialParams
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.modbus import async_get_unit
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_DEVICE,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.pentasun_heating.bus import SOCKET_BAUDRATE
from custom_components.pentasun_heating.const import (
    CONF_ADDRESSES,
    CONF_AUTO_SYNC_CLOCK,
    CONF_BAUDRATE,
    CONF_CONNECTION_TYPE,
    CONF_MAX_TEMP,
    CONF_MESSAGE_DELAY,
    CONF_MIN_TEMP,
    CONF_PARITY,
    CONF_STOPBITS,
    CONN_MODBUS_TCP,
    CONN_RTU_OVER_TCP,
    CONN_SERIAL,
    DOMAIN,
)

from .simulator import ThermostatBus

CLIMATE_1 = "climate.thermostat_1"
CLIMATE_2 = "climate.thermostat_2"

try:
    from homeassistant.components.modbus.connection import async_get_connection_info
except ImportError:  # Home Assistant 2026.9
    async_get_connection_info = None

needs_connection_info = pytest.mark.skipif(
    async_get_connection_info is None,
    reason="Home Assistant < 2026.10 doesn't report which units share a link",
)


def _data(bus: ThermostatBus) -> dict:
    return {CONF_CONNECTION_TYPE: CONN_RTU_OVER_TCP, CONF_HOST: "127.0.0.1", CONF_PORT: bus.port}


def _entry(bus: ThermostatBus, addresses: list[int], **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Pentasun",
        unique_id=f"127.0.0.1:{bus.port}",
        data=_data(bus),
        options={
            CONF_ADDRESSES: addresses,
            CONF_SCAN_INTERVAL: 30,
            CONF_MESSAGE_DELAY: 0,
            CONF_MIN_TEMP: 5.0,
            CONF_MAX_TEMP: 35.0,
            CONF_AUTO_SYNC_CLOCK: False,
            **options,
        },
    )


def _other_integration(hass: HomeAssistant, bus: ThermostatBus, unit_id: int, **link):
    """Simulate an unrelated integration holding a unit on the same RTU-over-TCP link."""
    entry = MockConfigEntry(domain="other_meter", title="Energy meter")
    entry.add_to_hass(hass)
    params = ModbusSerialParams(
        device=f"socket://127.0.0.1:{bus.port}", **({"baudrate": SOCKET_BAUDRATE} | link)
    )
    return entry, async_get_unit(hass, entry, params, unit_id)


async def _poll(hass: HomeAssistant, after: timedelta) -> None:
    """Trigger a poll as if ``after`` had passed (wall clock included)."""
    future = dt_util.utcnow() + after
    with patch("homeassistant.util.dt.utcnow", return_value=future):
        async_fire_time_changed(hass, future)
        await hass.async_block_till_done(wait_background_tasks=True)


async def _start_flow(hass: HomeAssistant, conn: str, user_input: dict) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": conn}
    )
    assert result["step_id"] == conn
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_config_flow(hass: HomeAssistant, mbap_bus: ThermostatBus) -> None:
    """Walk through the Modbus TCP flow, including a missing thermostat."""
    mbap_bus.add(1)
    result = await _start_flow(
        hass, CONN_MODBUS_TCP, {CONF_HOST: "127.0.0.1", CONF_PORT: mbap_bus.port}
    )
    assert result["step_id"] == "thermostats"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1, 0"}
    )
    assert result["errors"] == {CONF_ADDRESSES: "invalid_addresses"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1-2"}
    )
    assert result["errors"] == {"base": "no_response"}
    assert result["description_placeholders"]["addresses"] == "2"

    mbap_bus.add(2)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1-2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CONNECTION_TYPE: CONN_MODBUS_TCP, CONF_HOST: "127.0.0.1", CONF_PORT: mbap_bus.port,
    }
    assert result["options"][CONF_ADDRESSES] == [1, 2]
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE_2) is not None

    # Same bus again is rejected
    result = await _start_flow(
        hass, CONN_MODBUS_TCP, {CONF_HOST: "127.0.0.1", CONF_PORT: mbap_bus.port}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_serial_flow(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """The serial path takes any pyserial-style URL as a custom port."""
    rtu_bus.add(1)
    result = await _start_flow(
        hass,
        CONN_SERIAL,
        {
            CONF_DEVICE: f"socket://127.0.0.1:{rtu_bus.port}",
            CONF_BAUDRATE: "115200",
            CONF_PARITY: "N",
            CONF_STOPBITS: "1",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BAUDRATE] == 115200
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE_1).state == HVACMode.HEAT


async def test_cannot_connect(hass: HomeAssistant) -> None:
    """A closed port is reported on the thermostats step."""
    bus = ThermostatBus()
    port = await bus.start("rtu")
    await bus.stop()
    result = await _start_flow(hass, CONN_RTU_OVER_TCP, {CONF_HOST: "127.0.0.1", CONF_PORT: port})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_flow_rejects_other_device(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """A different kind of device answering on the address is not added."""
    rtu_bus.add(1)
    rtu_bus.add(3, [2300, 2310, 2290, 50, 61, 0, 0, 0, 0])  # e.g. a power meter
    result = await _start_flow(
        hass, CONN_RTU_OVER_TCP, {CONF_HOST: "127.0.0.1", CONF_PORT: rtu_bus.port}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1, 3"}
    )
    assert result["errors"] == {"base": "not_thermostat"}
    assert result["description_placeholders"]["addresses"] == "3"


async def test_flow_link_conflict(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """Another integration holding the link with other settings is reported."""
    rtu_bus.add(1)
    _other_integration(hass, rtu_bus, 9, baudrate=9600)
    result = await _start_flow(
        hass, CONN_RTU_OVER_TCP, {CONF_HOST: "127.0.0.1", CONF_PORT: rtu_bus.port}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1"}
    )
    assert result["errors"] == {"base": "link_conflict"}


async def test_setup_link_conflict(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """Setup fails with a clear error instead of fighting over the link."""
    rtu_bus.add(1)
    _other_integration(hass, rtu_bus, 9, baudrate=9600)
    entry = _entry(rtu_bus, [1])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


@needs_connection_info
async def test_flow_address_in_use(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """An address another integration uses on the same bus is refused."""
    rtu_bus.add(1)
    rtu_bus.add(9)
    _other_integration(hass, rtu_bus, 9)
    result = await _start_flow(
        hass, CONN_RTU_OVER_TCP, {CONF_HOST: "127.0.0.1", CONF_PORT: rtu_bus.port}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1, 9", "skip_check": True}
    )
    assert result["errors"] == {"base": "address_in_use"}
    assert result["description_placeholders"]["addresses"] == "9"
    assert "Energy meter" in result["description_placeholders"]["users"]


async def test_shares_bus_with_other_integration(
    hass: HomeAssistant, rtu_bus: ThermostatBus
) -> None:
    """Our thermostats and another integration's device use one link side by side."""
    rtu_bus.add(1)
    meter = rtu_bus.add(9, [1234, 5678])
    other_entry, other_unit = _other_integration(hass, rtu_bus, 9)
    assert await other_unit.read_holding_registers(0, 2) == meter

    entry = _entry(rtu_bus, [1])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE_1).state == HVACMode.HEAT
    # One TCP connection carries both integrations' traffic.
    assert rtu_bus.connections == 1

    # Unloading our entry leaves the shared link up for the other integration.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert other_unit.connected
    assert await other_unit.read_holding_registers(0, 2) == meter
    assert rtu_bus.connections == 1


async def test_yaml_hub_on_same_bus(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """A YAML hub with its own link to our bus raises a repair issue."""
    rtu_bus.add(1)
    hass.data["modbus"] = {
        "heat_pump": SimpleNamespace(
            endpoint=("serial", f"socket://127.0.0.1:{rtu_bus.port}"), units=[5]
        )
    }
    entry = _entry(rtu_bus, [1])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"yaml_hub_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders["hubs"] == "heat_pump"


async def test_entities(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """Entities reflect and control the registers."""
    regs = rtu_bus.add(1)
    regs2 = rtu_bus.add(2, [0, 2, 200, 1, 0, 0, 1, 190, 0])
    entry = _entry(rtu_bus, [1, 2])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE_1)
    assert state.state == HVACMode.HEAT
    assert state.attributes["current_temperature"] == 21.5
    assert state.attributes[ATTR_TEMPERATURE] == 22.0
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert state.attributes[ATTR_PRESET_MODE] == "manual"
    state = hass.states.get(CLIMATE_2)
    assert state.state == HVACMode.OFF
    assert state.attributes[ATTR_PRESET_MODE] == "schedule"
    assert hass.states.get("binary_sensor.thermostat_1_heating").state == STATE_ON
    assert hass.states.get("sensor.thermostat_1_temperature").state == "21.5"
    assert hass.states.get("switch.thermostat_2_child_lock").state == STATE_ON

    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: CLIMATE_1, ATTR_TEMPERATURE: 24.3}, blocking=True,
    )
    assert regs[2] == 245
    assert hass.states.get(CLIMATE_1).attributes[ATTR_TEMPERATURE] == 24.5

    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: CLIMATE_2, "hvac_mode": HVACMode.HEAT}, blocking=True,
    )
    assert regs2[0] == 1
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: CLIMATE_2, ATTR_PRESET_MODE: "timer"}, blocking=True,
    )
    assert regs2[1] == 1
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.thermostat_2_child_lock"}, blocking=True,
    )
    assert regs2[3] == 0

    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.thermostat_1_sync_clock"}, blocking=True,
    )
    assert 1 <= regs[6] <= 7

    # One missed poll is tolerated; the second makes only that thermostat unavailable
    del rtu_bus.units[2]
    await _poll(hass, timedelta(seconds=31))
    assert hass.states.get(CLIMATE_2).state == HVACMode.HEAT
    await _poll(hass, timedelta(seconds=62))
    assert hass.states.get(CLIMATE_2).state == STATE_UNAVAILABLE
    assert hass.states.get(CLIMATE_1).state == HVACMode.HEAT

    # A silent thermostat is not asked again on every poll, to keep the bus free
    rtu_bus.units[2] = regs2
    requests = len(rtu_bus.requests)
    await _poll(hass, timedelta(seconds=93))
    assert hass.states.get(CLIMATE_2).state == STATE_UNAVAILABLE
    assert [unit for unit, _ in rtu_bus.requests[requests:]] == [1]

    # ...but it is retried after a while; external changes are picked up too
    regs[0] = 0
    await _poll(hass, timedelta(minutes=7))
    assert hass.states.get(CLIMATE_1).state == HVACMode.OFF
    assert hass.states.get(CLIMATE_2).state == HVACMode.HEAT

    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: CLIMATE_1}, blocking=True,
    )
    assert regs[0] == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_old_firmware_and_clock_sync(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """Firmware without register 40009 works; clocks get synced when enabled."""
    regs = rtu_bus.add(1, [1, 0, 220, 0, 0, 0, 0, 215])  # invalid clock (weekday 0)
    entry = _entry(rtu_bus, [1], **{CONF_AUTO_SYNC_CLOCK: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(CLIMATE_1)
    assert ATTR_HVAC_ACTION not in state.attributes
    assert hass.states.get("binary_sensor.thermostat_1_heating").state == "unknown"
    assert 1 <= regs[6] <= 7


async def test_not_ready_and_options(hass: HomeAssistant, rtu_bus: ThermostatBus) -> None:
    """Setup retries when nothing answers; options change addresses and drop devices."""
    entry = _entry(rtu_bus, [1, 2])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    rtu_bus.add(1)
    rtu_bus.add(2)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 2

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ADDRESSES: "1",
            CONF_SCAN_INTERVAL: 10,
            CONF_MESSAGE_DELAY: 0,
            CONF_MIN_TEMP: 10,
            CONF_MAX_TEMP: 30,
            CONF_AUTO_SYNC_CLOCK: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_ADDRESSES] == [1]
    assert hass.states.get(CLIMATE_1).attributes["max_temp"] == 30
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1


async def test_reconfigure(hass: HomeAssistant, rtu_bus: ThermostatBus, mbap_bus: ThermostatBus) -> None:
    """The connection can be switched, e.g. to a Modbus TCP gateway."""
    rtu_bus.add(1)
    entry = _entry(rtu_bus, [1])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": CONN_MODBUS_TCP}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: mbap_bus.port}
    )
    assert result["errors"] == {"base": "no_devices"}

    mbap_bus.add(1)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: mbap_bus.port}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_CONNECTION_TYPE] == CONN_MODBUS_TCP
    assert entry.unique_id == f"127.0.0.1:{mbap_bus.port}"
    assert hass.states.get(CLIMATE_1).state == HVACMode.HEAT
