"""Fixtures for the Pentasun tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
import functools
from unittest.mock import patch

from modbus_connection.tmodbus import ModbusConnection
import pytest

from .simulator import ThermostatBus


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Enable custom integrations, and sockets for the simulated bus on localhost."""
    return


@pytest.fixture
async def rtu_bus() -> AsyncGenerator[ThermostatBus]:
    """A bus reachable as raw RTU over TCP."""
    bus = ThermostatBus()
    await bus.start("rtu")
    yield bus
    await bus.stop()


@pytest.fixture
async def mbap_bus() -> AsyncGenerator[ThermostatBus]:
    """A bus behind a Modbus TCP gateway."""
    bus = ThermostatBus()
    await bus.start("tcp")
    yield bus
    await bus.stop()


@pytest.fixture(autouse=True)
def fast_shared_connections() -> Generator[None]:
    """Shorten timeouts so silent units fail fast despite the retries."""
    with (
        patch(
            "homeassistant.components.modbus.connection.ModbusConnection",
            functools.partial(ModbusConnection, timeout=0.05),
        ),
        patch("custom_components.pentasun_heating.config_flow.DEFAULT_TIMEOUT", 0.05),
        patch("custom_components.pentasun_heating.bus.REQUEST_BURST_PAUSES", (0.01, 0.02)),
        patch.dict(
            "custom_components.pentasun_heating.config_flow.DEFAULT_OPTIONS",
            {"timeout": 0.05},
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def no_serial_ports() -> Generator[None]:
    """Don't scan the host's serial ports."""
    with patch(
        "custom_components.pentasun_heating.config_flow.usb.async_scan_serial_ports",
        return_value=[],
    ):
        yield
