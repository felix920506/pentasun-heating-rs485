"""Fixtures for the Pentasun tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

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
