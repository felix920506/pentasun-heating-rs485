"""Tests for the protocol layer."""

from __future__ import annotations

import pytest

from custom_components.pentasun_heating.modbus import (
    ModbusClient,
    ModbusConnectionError,
    ModbusExceptionResponse,
    ModbusTcpTransport,
    ModbusTimeoutError,
    SerialRtuTransport,
    TcpRtuTransport,
    build_rtu_frame,
    crc16,
)

from .simulator import ThermostatBus


def test_crc() -> None:
    """Check against a well known frame: read 1 register at 0 from unit 1."""
    assert build_rtu_frame(1, bytes.fromhex("0300000001")).hex() == "010300000001840a"
    assert crc16(b"") == 0xFFFF


def _clients(bus: ThermostatBus, mode: str) -> ModbusClient:
    if mode == "rtu":
        transport = TcpRtuTransport("127.0.0.1", bus.port)
    elif mode == "serial":
        # Exercises the pyserial code path used for local ports and rfc2217://
        transport = SerialRtuTransport(f"socket://127.0.0.1:{bus.port}")
    else:
        transport = ModbusTcpTransport("127.0.0.1", bus.port)
    return ModbusClient(transport, timeout=0.3, message_delay=0)


@pytest.mark.parametrize("mode", ["rtu", "serial", "tcp"])
async def test_read_write(mode: str) -> None:
    """Read, write, exceptions and a silent unit on every transport."""
    bus = ThermostatBus()
    await bus.start("tcp" if mode == "tcp" else "rtu")
    regs = bus.add(1)
    bus.add(7, [0] * 8)
    client = _clients(bus, mode)
    try:
        assert await client.read_holding_registers(1, 0, 9) == regs
        await client.write_register(1, 2, 255)
        assert regs[2] == 255
        assert await client.read_holding_registers(7, 0, 8) == [0] * 8

        with pytest.raises(ModbusExceptionResponse) as exc:
            await client.read_holding_registers(7, 0, 9)
        assert exc.value.code == 2

        with pytest.raises(ModbusTimeoutError):
            await client.read_holding_registers(3, 0, 9)
        # One retry by default
        assert [u for u, _ in bus.requests].count(3) == 2

        # The bus still works after a timeout
        assert await client.read_holding_registers(1, 7, 2) == [215, 1]
    finally:
        await client.close()
        await bus.stop()


async def test_connection_refused() -> None:
    """A closed port raises a connection error."""
    bus = ThermostatBus()
    port = await bus.start("rtu")
    await bus.stop()
    client = ModbusClient(TcpRtuTransport("127.0.0.1", port), timeout=0.2)
    with pytest.raises(ModbusConnectionError):
        await client.read_holding_registers(1, 0, 9)


async def test_bad_serial_port() -> None:
    """A missing serial device raises a connection error."""
    client = ModbusClient(SerialRtuTransport("/dev/does-not-exist"), timeout=0.2)
    with pytest.raises(ModbusConnectionError):
        await client.read_holding_registers(1, 0, 9)
