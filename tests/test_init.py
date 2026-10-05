"""Tests for the config flow and entities."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

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
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from custom_components.pentasun_heating.const import (
    CONF_ADDRESSES,
    CONF_AUTO_SYNC_CLOCK,
    CONF_CONNECTION_TYPE,
    CONF_MAX_TEMP,
    CONF_MESSAGE_DELAY,
    CONF_MIN_TEMP,
    CONF_TIMEOUT,
    CONN_MODBUS_TCP,
    CONN_RTU_OVER_TCP,
    DOMAIN,
)

from .simulator import ThermostatBus

CLIMATE_1 = "climate.thermostat_1"
CLIMATE_2 = "climate.thermostat_2"


def _entry(bus: ThermostatBus, addresses: list[int], **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Pentasun",
        unique_id=f"127.0.0.1:{bus.port}",
        data={CONF_CONNECTION_TYPE: CONN_RTU_OVER_TCP, CONF_HOST: "127.0.0.1", CONF_PORT: bus.port},
        options={
            CONF_ADDRESSES: addresses,
            CONF_SCAN_INTERVAL: 30,
            CONF_TIMEOUT: 0.2,
            CONF_MESSAGE_DELAY: 0,
            CONF_MIN_TEMP: 5.0,
            CONF_MAX_TEMP: 35.0,
            CONF_AUTO_SYNC_CLOCK: False,
            **options,
        },
    )


async def test_config_flow(hass: HomeAssistant, mbap_bus: ThermostatBus) -> None:
    """Walk through the Modbus TCP flow, including a missing thermostat."""
    mbap_bus.add(1)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": CONN_MODBUS_TCP}
    )
    assert result["step_id"] == CONN_MODBUS_TCP
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: mbap_bus.port}
    )
    assert result["step_id"] == "thermostats"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1, 0"}
    )
    assert result["errors"] == {CONF_ADDRESSES: "invalid_addresses"}

    with patch("custom_components.pentasun_heating.config_flow.DEFAULT_OPTIONS",
               {CONF_TIMEOUT: 0.2, CONF_MESSAGE_DELAY: 0}):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESSES: "1-2"}
        )
    assert result["errors"] == {"base": "no_response"}
    assert result["description_placeholders"]["missing"] == "2"

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
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": CONN_MODBUS_TCP}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: mbap_bus.port}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cannot_connect(hass: HomeAssistant) -> None:
    """A closed port is reported on the thermostats step."""
    bus = ThermostatBus()
    port = await bus.start("rtu")
    await bus.stop()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": CONN_RTU_OVER_TCP}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: port}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESSES: "1"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


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

    # A thermostat that stops answering becomes unavailable on its own
    del rtu_bus.units[2]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(CLIMATE_2).state == STATE_UNAVAILABLE
    assert hass.states.get(CLIMATE_1).state == HVACMode.HEAT

    # External change picked up on the next poll
    regs[0] = 0
    rtu_bus.units[2] = regs2
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=62))
    await hass.async_block_till_done(wait_background_tasks=True)
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
            CONF_TIMEOUT: 0.5,
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
