"""Minimal asyncio Modbus master for the PTB thermostats.

The thermostats only implement function codes 0x03 (read holding registers)
and 0x06 (write single register), so instead of depending on pymodbus (whose
API changes between the versions pinned by Home Assistant core) this module
implements just what is needed, over four kinds of links:

* ``SerialRtuTransport``   - local RS485 adapter, or an ``rfc2217://`` URL
                             (both handled by pyserial's ``serial_for_url``)
* ``TcpRtuTransport``      - serial device server in raw TCP socket mode
                             (Modbus RTU frames tunnelled over TCP)
* ``ModbusTcpTransport``   - serial device server in Modbus gateway mode
                             (Modbus TCP on the network side, RTU on the bus)

This module intentionally does not import Home Assistant.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
import contextlib
import logging
import struct
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

FC_READ_HOLDING_REGISTERS = 0x03
FC_WRITE_SINGLE_REGISTER = 0x06


class ModbusError(Exception):
    """Base error for Modbus communication problems."""


class ModbusConnectionError(ModbusError):
    """The link to the bus could not be opened or was lost."""


class ModbusTimeoutError(ModbusError):
    """The slave did not answer in time."""


class ModbusFrameError(ModbusError):
    """A malformed or unexpected response was received."""


class ModbusExceptionResponse(ModbusError):
    """The slave answered with a Modbus exception."""

    def __init__(self, function: int, code: int) -> None:
        """Initialize the exception."""
        super().__init__(f"Modbus exception {code} for function {function:#04x}")
        self.function = function
        self.code = code


def crc16(data: bytes) -> int:
    """Return the Modbus CRC-16 of ``data``."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_rtu_frame(unit: int, pdu: bytes) -> bytes:
    """Wrap a PDU into an RTU frame (address + PDU + CRC, low byte first)."""
    body = bytes([unit]) + pdu
    return body + struct.pack("<H", crc16(body))


def rtu_remaining_length(head: bytes) -> int:
    """Return how many bytes follow the first three bytes of an RTU response."""
    function = head[1]
    if function & 0x80:
        return 2  # exception code already read, CRC follows
    if function == FC_READ_HOLDING_REGISTERS:
        return head[2] + 2  # byte count, data, CRC
    if function == FC_WRITE_SINGLE_REGISTER:
        return 5  # rest of address, value, CRC
    raise ModbusFrameError(f"Unsupported function code in response: {function:#04x}")


def parse_rtu_frame(unit: int, frame: bytes) -> bytes:
    """Validate an RTU response frame and return its PDU."""
    if len(frame) < 4:
        raise ModbusFrameError(f"Response too short: {frame.hex()}")
    body, crc = frame[:-2], struct.unpack("<H", frame[-2:])[0]
    if crc16(body) != crc:
        raise ModbusFrameError(f"CRC mismatch in response: {frame.hex()}")
    if body[0] != unit:
        raise ModbusFrameError(
            f"Response from unit {body[0]} while waiting for unit {unit}"
        )
    return body[1:]


class Transport(ABC):
    """A link capable of exchanging one Modbus request/response."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Return True when the link is open."""

    @abstractmethod
    async def connect(self) -> None:
        """Open the link."""

    @abstractmethod
    async def close(self) -> None:
        """Close the link."""

    @abstractmethod
    async def exchange(self, unit: int, pdu: bytes, timeout: float) -> bytes:
        """Send ``pdu`` to ``unit`` and return the response PDU."""

    def reset_input(self) -> None:  # noqa: B027
        """Discard any stale input after a failed exchange (optional)."""


class SerialRtuTransport(Transport):
    """RTU over a pyserial port: local device path or ``rfc2217://`` URL.

    pyserial's rfc2217 implementation has no file descriptor asyncio could
    watch, so all port I/O runs synchronously in the default executor.
    """

    def __init__(
        self,
        url: str,
        *,
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: int = 1,
        bytesize: int = 8,
    ) -> None:
        """Initialize the transport."""
        self._url = url
        self._settings: dict[str, Any] = {
            "baudrate": baudrate,
            "parity": parity,
            "stopbits": stopbits,
            "bytesize": bytesize,
        }
        self._serial: Any = None
        # 3.5 character times, the minimum silent interval between RTU frames.
        self._frame_gap = max(3.5 * 11 / baudrate, 0.00175)

    @property
    def connected(self) -> bool:
        """Return True when the port is open."""
        return self._serial is not None and self._serial.is_open

    async def connect(self) -> None:
        """Open the port."""
        loop = asyncio.get_running_loop()
        try:
            self._serial = await loop.run_in_executor(None, self._open)
        except Exception as err:  # serial.SerialException, OSError, ValueError
            raise ModbusConnectionError(
                f"Unable to open {self._url}: {err}"
            ) from err

    def _open(self) -> Any:
        import serial  # noqa: PLC0415 - avoid importing pyserial at module load

        return serial.serial_for_url(
            self._url, timeout=0.05, write_timeout=2, **self._settings
        )

    async def close(self) -> None:
        """Close the port."""
        if (ser := self._serial) is None:
            return
        self._serial = None
        await asyncio.get_running_loop().run_in_executor(None, ser.close)

    async def exchange(self, unit: int, pdu: bytes, timeout: float) -> bytes:
        """Send a request and read the response."""
        frame = build_rtu_frame(unit, pdu)
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None, self._sync_exchange, frame, timeout
            )
        except ModbusError:
            raise
        except Exception as err:  # serial.SerialException, OSError
            await self.close()
            raise ModbusConnectionError(f"Serial I/O error: {err}") from err
        return parse_rtu_frame(unit, response)

    def _sync_exchange(self, frame: bytes, timeout: float) -> bytes:
        ser = self._serial
        if ser is None:
            raise ModbusConnectionError("Port is not open")
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()
        deadline = time.monotonic() + timeout
        head = self._read_exact(ser, 3, deadline)
        return head + self._read_exact(ser, rtu_remaining_length(head), deadline)

    def _read_exact(self, ser: Any, size: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < size:
            if time.monotonic() > deadline:
                if buf:
                    raise ModbusFrameError(f"Incomplete response: {bytes(buf).hex()}")
                raise ModbusTimeoutError("No response")
            buf += ser.read(size - len(buf))
        return bytes(buf)

    @property
    def frame_gap(self) -> float:
        """Return the minimum silent interval between frames, in seconds."""
        return self._frame_gap


class _StreamTransport(Transport):
    """Common code for TCP based transports."""

    def __init__(self, host: str, port: int, connect_timeout: float = 5.0) -> None:
        """Initialize the transport."""
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._dirty = False

    @property
    def connected(self) -> bool:
        """Return True when the socket is open."""
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open the TCP connection."""
        try:
            async with asyncio.timeout(self._connect_timeout):
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port
                )
        except (OSError, TimeoutError) as err:
            raise ModbusConnectionError(
                f"Unable to connect to {self._host}:{self._port}: {err}"
            ) from err
        self._dirty = False

    async def close(self) -> None:
        """Close the TCP connection."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    def reset_input(self) -> None:
        """Remember to discard late bytes before the next request."""
        self._dirty = True

    async def _discard_stale(self) -> None:
        assert self._reader is not None
        while True:
            try:
                async with asyncio.timeout(0.02):
                    data = await self._reader.read(1024)
            except TimeoutError:
                break
            if not data:
                raise ModbusConnectionError("Connection closed by peer")
            _LOGGER.debug("Discarded stale bytes: %s", data.hex())
        self._dirty = False

    async def _read(self, size: int) -> bytes:
        assert self._reader is not None
        try:
            return await self._reader.readexactly(size)
        except asyncio.IncompleteReadError as err:
            await self.close()
            raise ModbusConnectionError("Connection closed by peer") from err

    async def exchange(self, unit: int, pdu: bytes, timeout: float) -> bytes:
        """Send a request and read the response."""
        if self._writer is None:
            raise ModbusConnectionError("Not connected")
        try:
            if self._dirty:
                await self._discard_stale()
            self._writer.write(self._frame(unit, pdu))
            await self._writer.drain()
            async with asyncio.timeout(timeout):
                return await self._read_response(unit)
        except TimeoutError as err:
            raise ModbusTimeoutError("No response") from err
        except OSError as err:
            await self.close()
            raise ModbusConnectionError(f"Socket error: {err}") from err

    @abstractmethod
    def _frame(self, unit: int, pdu: bytes) -> bytes:
        """Build the bytes to send for a request."""

    @abstractmethod
    async def _read_response(self, unit: int) -> bytes:
        """Read one response and return its PDU."""


class TcpRtuTransport(_StreamTransport):
    """Raw RTU frames over a TCP socket (serial server in "TCP server" mode)."""

    def _frame(self, unit: int, pdu: bytes) -> bytes:
        return build_rtu_frame(unit, pdu)

    async def _read_response(self, unit: int) -> bytes:
        head = await self._read(3)
        try:
            remaining = rtu_remaining_length(head)
        except ModbusFrameError:
            self._dirty = True
            raise
        return parse_rtu_frame(unit, head + await self._read(remaining))


class ModbusTcpTransport(_StreamTransport):
    """Modbus TCP (MBAP) to a gateway that converts to RTU on the bus side."""

    def __init__(self, host: str, port: int = 502, connect_timeout: float = 5.0) -> None:
        """Initialize the transport."""
        super().__init__(host, port, connect_timeout)
        self._transaction_id = 0

    def _frame(self, unit: int, pdu: bytes) -> bytes:
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        return struct.pack(">HHHB", self._transaction_id, 0, len(pdu) + 1, unit) + pdu

    async def _read_response(self, unit: int) -> bytes:
        while True:
            tid, proto, length, resp_unit = struct.unpack(">HHHB", await self._read(7))
            if proto != 0 or not 2 <= length <= 256:
                await self.close()
                raise ModbusFrameError(f"Invalid MBAP header (proto={proto}, len={length})")
            pdu = await self._read(length - 1)
            if tid != self._transaction_id:
                # Late answer to an earlier request that timed out; skip it.
                _LOGGER.debug("Skipping response with stale transaction id %s", tid)
                continue
            if resp_unit != unit:
                raise ModbusFrameError(
                    f"Response from unit {resp_unit} while waiting for unit {unit}"
                )
            return pdu


class ModbusClient:
    """Serializes requests on one bus and implements function codes 3 and 6."""

    def __init__(
        self,
        transport: Transport,
        *,
        timeout: float = 1.0,
        retries: int = 1,
        message_delay: float = 0.05,
    ) -> None:
        """Initialize the client.

        ``message_delay`` is the pause kept between the end of one transaction
        and the start of the next; inexpensive thermostats often need it.
        """
        self._transport = transport
        self._timeout = timeout
        self._retries = retries
        self._delay = max(message_delay, getattr(transport, "frame_gap", 0.0))
        self._lock = asyncio.Lock()
        self._last_done = 0.0

    @property
    def transport(self) -> Transport:
        """Return the underlying transport."""
        return self._transport

    async def close(self) -> None:
        """Close the underlying link."""
        async with self._lock:
            await self._transport.close()

    async def read_holding_registers(
        self, unit: int, address: int, count: int
    ) -> list[int]:
        """Read ``count`` holding registers starting at ``address``."""
        pdu = struct.pack(">BHH", FC_READ_HOLDING_REGISTERS, address, count)

        def check(resp: bytes) -> list[int]:
            if len(resp) != 2 + 2 * count or resp[1] != 2 * count:
                raise ModbusFrameError(f"Unexpected read response: {resp.hex()}")
            return list(struct.unpack(f">{count}H", resp[2:]))

        return await self._request(unit, pdu, check)

    async def write_register(self, unit: int, address: int, value: int) -> None:
        """Write a single holding register."""
        pdu = struct.pack(">BHH", FC_WRITE_SINGLE_REGISTER, address, value & 0xFFFF)

        def check(resp: bytes) -> None:
            if resp != pdu:
                raise ModbusFrameError(f"Unexpected write echo: {resp.hex()}")

        await self._request(unit, pdu, check)

    async def _request[T](
        self, unit: int, pdu: bytes, check: Callable[[bytes], T]
    ) -> T:
        async with self._lock:
            attempt = 0
            while True:
                try:
                    return await self._attempt(unit, pdu, check)
                except (ModbusTimeoutError, ModbusFrameError) as err:
                    self._transport.reset_input()
                    if attempt >= self._retries:
                        raise
                    attempt += 1
                    _LOGGER.debug(
                        "Unit %s: %s, retrying (%s/%s)",
                        unit, err, attempt, self._retries,
                    )

    async def _attempt(
        self, unit: int, pdu: bytes, check: Callable[[bytes], Any]
    ) -> Any:
        if not self._transport.connected:
            await self._transport.connect()
        wait = self._last_done + self._delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await self._transport.exchange(unit, pdu, self._timeout)
        finally:
            self._last_done = time.monotonic()
        if not resp:
            raise ModbusFrameError("Empty response")
        if resp[0] == pdu[0] | 0x80:
            raise ModbusExceptionResponse(pdu[0], resp[1] if len(resp) > 1 else 0)
        if resp[0] != pdu[0]:
            raise ModbusFrameError(f"Unexpected function in response: {resp.hex()}")
        return check(resp)
