# Pentasun Floor Heating (RS485 / Modbus RTU) for Home Assistant

Custom integration for the Pentasun **PTB** floor heating thermostats, which speak
Modbus RTU over a shared RS485 bus (protocol "溫控器通用介面協定 V1.0 / JKW-MODBUS").
Several thermostats can share one bus; each one has its own Modbus address.

## Supported connections

| Option in the setup dialog | Hardware | Notes |
| --- | --- | --- |
| USB / RS485 adapter | RS485 dongle on the Home Assistant host | Pick the `/dev/serial/by-id/...` path if offered |
| Serial device server (RFC 2217) | USR-TCP232, Moxa NPort, ser2net etc. in RFC 2217 / "Telnet COM port" mode | Home Assistant sets baud rate/parity on the server |
| Serial device server (raw TCP socket) | Elfin EW11, USR, ser2net `raw` etc. in TCP server mode | Set 9600 8N1 on the server; RTU frames pass through unchanged |
| Modbus TCP gateway | Server in "Modbus TCP to RTU" gateway mode | Usually port 502; the thermostat address is the unit ID |

Default serial settings for the thermostats: **9600 baud, 8 data bits, no parity, 1 stop bit**.
Wire A(+) and B(-) to the thermostats' A/B terminals.

## Installation

* **HACS**: add this repository as a custom repository (category *Integration*) and install.
* **Manual**: copy `custom_components/pentasun_heating` into your Home Assistant
  `config/custom_components/` folder.

Restart Home Assistant, then go to *Settings → Devices & services → Add integration →
Pentasun Floor Heating*.

### Setting thermostat addresses

Each thermostat on a bus needs a unique address (default `1`). With the thermostat
**switched off**, hold **M** and the **clock** key for 5 seconds to open the advanced
options, press **M** until option **C** is shown, and change the value with the
up/down keys. Switch the thermostat on to save it.

In the setup dialog enter the addresses as a list, e.g. `1, 2, 5-8`.

## Entities (per thermostat)

| Entity | Register | Description |
| --- | --- | --- |
| Climate | 40001, 40002, 40003, 40008, 40009 | On/off (`heat`/`off`), target temperature (0.5 °C steps), room temperature, heating/idle action, preset = operating mode (`manual`, `timer`, `schedule`) |
| Temperature sensor | 40008 | Room temperature, for history and statistics |
| Heating binary sensor | 40009 | On while the thermostat calls for heat |
| Child lock switch | 40004 | Locks the keypad |
| Sync clock button | 40005-40007 | Sets the thermostat clock to Home Assistant's local time |
| Thermostat clock sensor | 40005-40007 | Diagnostic, disabled by default |

## Options

*Configure* on the integration entry lets you change the thermostat addresses, polling
interval (default 30 s), response timeout, delay between bus messages, min/max target
temperature and **automatic clock sync** (re-sets the clock whenever it drifts by more
than 2 minutes; the timer and schedule modes depend on it). *Reconfigure* changes how
the bus is connected without losing entities.

A thermostat that stops answering becomes unavailable on its own; the others keep
working.

## Protocol notes

* Function codes 0x03 (read holding registers) and 0x06 (write single register),
  registers 40001-40009 (offsets 0-8). Temperatures are value × 10.
* The protocol document describes register 40003 as "internal sensor temperature × 10",
  but it is the writable register and 40008 is the measured room temperature, so it is
  treated as the **set point**. The document also shows only the low byte being used;
  the full 16-bit register is used here so set points above 25.5 °C work. If your
  thermostats behave differently, please open an issue.
* The document lists the CRC as "high, low"; standard Modbus byte order (low byte
  first) is used, as the document otherwise refers to standard Modbus RTU.
* Thermostats whose firmware lacks register 40009 are handled (heating state unknown).
* The integration has its own small Modbus implementation (no pymodbus dependency),
  so it doesn't conflict with the version pinned by Home Assistant's built-in Modbus
  integration.

## Development

```sh
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest --timeout 30
```

The tests run against a simulated thermostat bus (`tests/simulator.py`) served over
raw RTU-over-TCP and Modbus TCP; the serial code path is exercised through pyserial's
`socket://` URL.

## Debug logging

```yaml
logger:
  logs:
    custom_components.pentasun_heating: debug
```
