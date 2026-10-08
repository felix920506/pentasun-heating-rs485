"""A fake RS485 bus of PTB thermostats (and other devices), served over TCP."""

from __future__ import annotations

import asyncio
import struct



def crc16(data: bytes) -> int:
    """Return the Modbus CRC-16 of ``data``."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_rtu_frame(unit: int, pdu: bytes) -> bytes:
    """Wrap a PDU into an RTU frame."""
    body = bytes([unit]) + pdu
    return body + struct.pack("<H", crc16(body))

# Values the real PTB thermostat keeps for each writable register (40001-40007).
# Anything else is acknowledged but ignored, as the hardware does.
ACCEPTED = {
    0: range(2),  # power
    1: range(2),  # mode: 2 (schedule) is documented but not accepted
    2: range(50, 501),  # set point 5.0-50.0 °C
    3: range(2),  # lock
    4: range(60),  # minute
    5: range(24),  # hour
    6: range(8),  # weekday, 0 = unset
}


class ThermostatBus:
    """Thermostats keyed by Modbus address, reachable via RTU-over-TCP or Modbus TCP."""

    def __init__(self) -> None:
        """Initialize the bus."""
        self.units: dict[int, list[int]] = {}
        self.requests: list[tuple[int, bytes]] = []
        self.server: asyncio.Server | None = None
        self.port = 0
        self.connections = 0  # currently open client connections
        self.drop_next = 0  # ignore this many upcoming requests, like the real thermostat
        self.lose_writes = 0  # acknowledge but don't apply this many valid writes

    def add(self, unit: int, regs: list[int] | None = None) -> list[int]:
        """Add a thermostat: on, manual, 22.0 °C target, 21.5 °C room, heating."""
        self.units[unit] = regs if regs is not None else [1, 0, 220, 0, 30, 12, 3, 215, 1]
        return self.units[unit]

    def handle(self, unit: int, pdu: bytes) -> bytes | None:
        """Process a request PDU; None means the unit stays silent."""
        self.requests.append((unit, pdu))
        if self.drop_next:
            self.drop_next -= 1
            return None
        regs = self.units.get(unit)
        if regs is None:
            return None
        function, address, value = struct.unpack(">BHH", pdu)
        if function == 3:
            if address + value > len(regs):
                return bytes([0x83, 2])
            data = regs[address : address + value]
            return bytes([3, 2 * value]) + struct.pack(f">{value}H", *data)
        if function == 6:
            if address not in ACCEPTED:
                return bytes([0x86, 2])
            if self.lose_writes:
                self.lose_writes -= 1
            elif value in ACCEPTED[address]:
                regs[address] = value
            return pdu
        return bytes([function | 0x80, 1])

    async def start(self, mode: str) -> int:
        """Start serving; mode is "rtu" or "tcp". Returns the port."""
        handler = self._serve_rtu if mode == "rtu" else self._serve_mbap
        self.server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Stop serving."""
        if self.server:
            self.server.close()
            self.server.close_clients()
            await self.server.wait_closed()

    async def _serve_rtu(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            while True:
                frame = await reader.readexactly(8)  # fc 3 and 6 requests are 8 bytes
                assert crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]
                if (resp := self.handle(frame[0], frame[1:6])) is not None:
                    writer.write(build_rtu_frame(frame[0], resp))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
        finally:
            self.connections -= 1

    async def _serve_mbap(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            while True:
                tid, _, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                pdu = await reader.readexactly(length - 1)
                if (resp := self.handle(unit, pdu)) is not None:
                    writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
        finally:
            self.connections -= 1
