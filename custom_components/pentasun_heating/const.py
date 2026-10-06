"""Constants for the Pentasun floor heating integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "pentasun_heating"
MANUFACTURER: Final = "Pentasun"
MODEL: Final = "PTB"

# Connection settings (config entry data)
CONF_CONNECTION_TYPE: Final = "connection_type"
CONF_BAUDRATE: Final = "baudrate"
CONF_PARITY: Final = "parity"
CONF_STOPBITS: Final = "stopbits"

CONN_SERIAL: Final = "serial"
CONN_RFC2217: Final = "rfc2217"
CONN_RTU_OVER_TCP: Final = "rtu_over_tcp"
CONN_MODBUS_TCP: Final = "modbus_tcp"
CONNECTION_TYPES: Final = [CONN_SERIAL, CONN_RFC2217, CONN_RTU_OVER_TCP, CONN_MODBUS_TCP]

DEFAULT_BAUDRATE: Final = 9600
DEFAULT_PARITY: Final = "N"
DEFAULT_STOPBITS: Final = 1
DEFAULT_MODBUS_TCP_PORT: Final = 502
BAUDRATES: Final = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]

# Tunables (config entry options)
CONF_ADDRESSES: Final = "addresses"
CONF_TIMEOUT: Final = "timeout"
CONF_MESSAGE_DELAY: Final = "message_delay"
CONF_MIN_TEMP: Final = "min_temp"
CONF_MAX_TEMP: Final = "max_temp"
CONF_AUTO_SYNC_CLOCK: Final = "auto_sync_clock"

DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_MESSAGE_DELAY: Final = 50  # milliseconds
DEFAULT_MIN_TEMP: Final = 5.0
DEFAULT_MAX_TEMP: Final = 35.0
DEFAULT_AUTO_SYNC_CLOCK: Final = False

MIN_ADDRESS: Final = 1
MAX_ADDRESS: Final = 255

# Holding registers (protocol document numbers them 40001..40009)
REG_POWER: Final = 0  # 0 = off, 1 = on
REG_MODE: Final = 1  # 0 = manual, 1 = timer, 2 = programmed schedule
REG_SETPOINT: Final = 2  # target temperature * 10
REG_LOCK: Final = 3  # 0 = unlocked, 1 = keypad locked
REG_MINUTE: Final = 4  # 0-59
REG_HOUR: Final = 5  # 0-23
REG_WEEKDAY: Final = 6  # 1 = Monday ... 7 = Sunday
REG_ROOM_TEMP: Final = 7  # room temperature * 10, read only
REG_HEATING: Final = 8  # 0 = idle, 1 = heating, read only
REGISTER_COUNT: Final = 9

MODE_MANUAL: Final = "manual"
MODE_TIMER: Final = "timer"
MODE_SCHEDULE: Final = "schedule"
MODES: Final = [MODE_MANUAL, MODE_TIMER, MODE_SCHEDULE]  # index = register value

# Re-sync the thermostat clock when it drifts by more than this many minutes
CLOCK_DRIFT_TOLERANCE: Final = 2

# A thermostat is shown unavailable after this many missed polls in a row
MAX_MISSED_POLLS: Final = 2
# ...and then only polled this often, so it doesn't stall the shared bus
UNAVAILABLE_RETRY_INTERVAL: Final = timedelta(minutes=5)
