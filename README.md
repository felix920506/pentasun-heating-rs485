# Pentasun Floor Heating (RS485 / Modbus RTU) for Home Assistant

Custom integration for the Pentasun **PTB** floor heating thermostats, which speak
Modbus RTU over a shared RS485 bus (protocol "溫控器通用介面協定 V1.0 / JKW-MODBUS").
Several thermostats can share one bus; each one has its own Modbus address.

Requires **Home Assistant 2026.9 or newer**. All bus traffic goes through Home
Assistant's built-in `modbus` integration, so the thermostats can share a bus
with other Modbus devices (see below).

## Supported connections

| Option in the setup dialog | Hardware | Notes |
| --- | --- | --- |
| USB / RS485 adapter | RS485 dongle on the Home Assistant host | Pick the `/dev/serial/by-id/...` path if offered |
| Serial device server (RFC 2217) | USR-TCP232, Moxa NPort, ser2net etc. in RFC 2217 / "Telnet COM port" mode | Home Assistant sets baud rate/parity on the server |
| Serial device server (raw TCP socket) | Elfin EW11, USR, ser2net `raw` etc. in TCP server mode | Set 9600 8N1 on the server's serial side; RTU frames pass through unchanged |
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

### Finding the thermostats on a bus

After choosing the connection, setup offers to **scan the bus** (Home Assistant
2026.10 or newer) or to enter the addresses yourself as a list, e.g. `1, 2, 5-8`.
To add thermostats later, use *Configure → Scan for new thermostats* on the
integration entry.

The scan asks every address in the chosen range (default 1–32) once per round, for
6 rounds, with a short 0.25 s timeout. The thermostats ignore about half of all
requests, so this finds a thermostat about 98 % of the time; the result is shown
before anything is saved, so you can add one the scan missed. Scanning 1–32 takes
about a minute; on a real bus with one thermostat it found it in each of 3 runs
(48 s each).

If you enter **how many thermostats** there are (or, when scanning from
*Configure*, how many are new), the scan stops as soon as it has found them all.
Devices beyond the last thermostat found aren't asked, so the list of other
devices may be incomplete. If fewer have answered after the 6 rounds, the scan
keeps asking the silent addresses for up to another minute. On a bus with 8
thermostats this found the 8th in that extra time in 2 of 3 scans. If some still
don't answer, the results warn you and list any addresses with garbled replies.
Garbled replies usually mean two thermostats share an address. Otherwise check the
wiring (A/B, bias, termination, loose terminals), the power, and that every
thermostat's address is within the scanned range.

Scanning is safe on a bus shared with other devices: addresses other integrations
use are never polled, devices that answer but don't look like a PTB thermostat
(e.g. an energy meter) are listed but not added, and the requests go over the
shared connection, so other integrations keep working during the scan. Home
Assistant 2026.9 can't shorten the 10 s Modbus timeout, which would make a scan
take tens of minutes, so there you enter the addresses manually.

## Entities (per thermostat)

| Entity | Register | Description |
| --- | --- | --- |
| Climate | 40001, 40002, 40003, 40008, 40009 | On/off (`heat`/`off`), target temperature (5–50 °C in 0.5 °C steps), room temperature, heating/idle action, preset = operating mode (`manual`, `timer`) |
| Temperature sensor | 40008 | Room temperature, for history and statistics |
| Heating binary sensor | 40009 | On while the thermostat calls for heat |
| Child lock switch | 40004 | Locks the keypad |
| Sync clock button | 40005-40007 | Sets the thermostat clock to Home Assistant's local time |
| Thermostat clock sensor | 40005-40007 | Diagnostic, disabled by default |

## Options

*Configure* on the integration entry lets you change the thermostat addresses, polling
interval (default 30 s), response timeout (default 1 s), delay between requests, min/max target
temperature and **automatic clock sync** (re-sets the clock whenever it drifts by more
than 2 minutes; the timer mode depends on it). *Reconfigure* changes how
the bus is connected without losing entities.

## Sharing the bus with other Modbus devices

The thermostats can sit on the same RS485 bus as unrelated Modbus devices (energy
meters, heat pumps, …) handled by other integrations:

* **One connection per bus.** The integration never opens its own connection. It asks
  Home Assistant's `modbus` integration for one, which shares a single connection per
  port or host between all integrations, so requests never collide on the wire.
  Every integration on a bus must use the same serial settings. If they differ,
  setup fails with an error that says so.
* **Addresses must be unique on the bus.** Setup refuses an address that another
  integration already uses on the same bus (Home Assistant 2026.10+). It also checks
  that the device answering looks like a PTB thermostat, so a meter that happens to
  use the address isn't added by mistake.
* **No bus hogging.** Each poll is a single 9-register read per thermostat. A
  thermostat that stops answering becomes unavailable after two missed polls and is
  then only retried every 5 minutes, because every unanswered request holds up the
  whole bus until it times out.
* **Short stalls only.** A lost request holds up the whole shared bus until it times
  out, so the integration asks for a 1 s response timeout (the thermostats answer
  within about 40 ms) instead of Home Assistant's 10 s default. That timeout is a
  minimum for the shared connection: an integration that asks for a longer one still
  gets it. It needs Home Assistant 2026.10+ (2026.9 always uses 10 s). The *delay
  between requests* only paces requests to the thermostats. Both are withdrawn when
  the integration unloads.
* **Modbus YAML hubs** (`modbus:` in `configuration.yaml`) open a separate
  connection of their own. If one points at the same port or host, a repair issue
  warns you, because the two connections' requests can collide on the bus.

**RTU over TCP baud rate:** for raw TCP links, Home Assistant labels the connection
115200 baud so that every integration using the same serial server shares it. That
label isn't sent anywhere. The real RS485 speed is whatever the serial server is
configured for (9600 for these thermostats), so you don't need to change any
device.

## Troubleshooting the RS485 bus

* **No thermostat found:** check the address on the thermostat itself (option **C**, see
  above). It may not be 1.
* **Thermostats answer only some of the time:** this is normal for these thermostats.
  They ignore roughly 30–70 % of requests, with clean replies to the rest. This was
  measured on a single thermostat both on an unbiased bus and on a properly biased one
  behind a serial device server, and it doesn't depend on timing or request size.
  Misses are mostly independent, with occasional silent stretches of up to ~8 s.
  The integration sends each request in up to 3 bursts of 4 tries (1 s timeout),
  pausing 2 s and then 5 s between bursts so the tries are spread over time; the bus
  stays free for other devices during the pauses. In a 150-read test on hardware
  every read succeeded: most at once (median 0.05 s), the slowest after 11 tries
  (17 s). A thermostat that really is gone costs at most 12 s of bus time, and is
  then only retried every 5 minutes.
* **Bus wiring:** RS485 still needs fail-safe bias for reliable communication. If A–B
  measures about 0 V with the bus idle, enable the bias (and 120 Ω termination)
  jumpers on your USB adapter or serial server. Otherwise add about 680 Ω from A to
  +5 V and from B to GND at one point on the bus; idle should then read roughly
  0.2–1 V.

## Protocol notes

Function codes 0x03 (read holding registers) and 0x06 (write single register),
registers 40001–40009 (offsets 0–8), temperatures as value × 10. Verified on a PTB
thermostat by watching its display while writing each register:

| Register | Meaning | Confirmed behaviour |
| --- | --- | --- |
| 40001 | Power | 0 = off (display dark), 1 = on |
| 40002 | Mode | 0 = manual, 1 = timer (display shows the timer state, e.g. "ON"). **2 (programming mode in the document) and higher are ignored** by the tested firmware |
| 40003 | Set point × 10 | Shown as "Set". The document calls it "internal sensor temperature" and shows only the low byte, but it is the set point and uses the **full 16-bit register**: 5.0–50.0 °C are accepted. Values are rounded down to 0.5 °C (22.3 → 22.0) |
| 40004 | Keypad lock | 0/1, shows a lock icon |
| 40005–40007 | Minute, hour, weekday | Weekday 1 = Monday … 7 = Sunday; 0 is accepted and shows no day |
| 40008 | Room temperature × 10 | Read only |
| 40009 | Heating | 1 while calling for heat (flame icon); follows a set point change after a few seconds |

* **Out-of-range writes are acknowledged but ignored.** The thermostat echoes the write
  as if it succeeded and keeps its old value. The integration therefore reads every
  write back, repeats it if it didn't stick (rarely, a valid write is lost), and
  reports an error if the thermostat refuses it.
* **Hysteresis:** heating switches on when the room is 1.0 °C or more below the set
  point and off when the room reaches the set point.
* The document lists the CRC as "high, low"; standard Modbus byte order (low byte
  first) is what the thermostats use.
* Thermostats whose firmware lacks register 40009 are handled (heating state unknown).
* Modbus communication uses [modbus-connection](https://home-assistant-libs.github.io/modbus-connection/)
  through Home Assistant's `modbus` integration, so the integration has no Python
  requirements of its own and can't conflict with the library versions Home Assistant
  ships.

## Development

```sh
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest --timeout 30
```

The tests run against a simulated bus (`tests/simulator.py`) served over raw
RTU-over-TCP and Modbus TCP, including another integration's device sharing the bus.
The serial code path is exercised through a `socket://` device.

## Debug logging

```yaml
logger:
  logs:
    custom_components.pentasun_heating: debug
    modbus_connection: debug
    tmodbus: debug
```
