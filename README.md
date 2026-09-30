# SaveEye → Domoticz MQTT Bridge

A small Python service that reads power-meter telemetry published by a
[SaveEye](https://saveeye.se) device to a local MQTT broker, converts it to
the format [Domoticz](https://www.domoticz.com) understands, and publishes it
to Domoticz's MQTT input topic (`domoticz/in`).

It is useful if you already have Domoticz devices (with years of logging
history) fed by another meter and want to switch to SaveEye **without
creating new devices or losing history**.

```
SaveEye device ──► MQTT broker ──► [ this bridge ] ──► MQTT broker ──► Domoticz
                  saveeye/telemetry                     domoticz/in
```

> **Disclaimer:** This is an unofficial community project and is not
> affiliated with or endorsed by SaveEye or Domoticz.

## Features

- Table-driven mapping: adding a Domoticz device is a single entry in one list
- Ships with mappings for power (W), lifetime energy counter (Wh) and
  per-phase voltage (V); examples for current (A) are included in the code
- Per-device update interval, so Domoticz is not flooded with messages
- Counter safety check: a lower value than the last one sent is skipped, so
  glitches don't ruin your energy graphs
- Optional counter offset, so history continues seamlessly from an old device
- Automatic reconnect if the broker restarts
- Settings live in one block at the top of the script; environment variables
  override them
- Works with paho-mqtt 1.x and 2.x

## Requirements

| Requirement | Notes |
|---|---|
| Python | 3.8 or newer |
| [paho-mqtt](https://pypi.org/project/paho-mqtt/) | 1.6+ or 2.x (`pip install -r requirements.txt`) |
| An MQTT broker | e.g. [Mosquitto](https://mosquitto.org), reachable from where the bridge runs |
| A SaveEye device | with **Local MQTT** enabled, publishing to `saveeye/telemetry` |
| Domoticz | with an **MQTT Client Gateway** hardware whose *Publish topic / In topic* is `domoticz/in` |
| Domoticz devices | the virtual sensors you want to update (see [Domoticz setup](#domoticz-setup)) |

## Installation

```bash
git clone https://github.com/overgaard/saveeye2domoticz.git /opt/saveeye
cd /opt/saveeye
pip install -r requirements.txt      # or: sudo apt install python3-paho-mqtt
```

## Setup

### 1. SaveEye

In the SaveEye app, open the MQTT configuration for your device and enter your
broker's address, port (default `1883`) and credentials. SaveEye's own guide
covers this:
[saveeye/SaveEye-HA-Guide](https://github.com/saveeye/SaveEye-HA-Guide).

To check that data arrives, subscribe to the topic:

```bash
mosquitto_sub -h <broker> -t saveeye/telemetry -v
```

### 2. Domoticz setup

1. Add an **MQTT Client Gateway with LAN interface** under
   *Setup → Hardware* and point it at your broker. Make sure it is set to
   receive on `domoticz/in`.
2. Note the **idx** of each Domoticz device you want to update
   (*Setup → Devices*). The device type decides the payload format:

| What | Domoticz type | Payload `svalue` | SaveEye field |
|---|---|---|---|
| Current power | Usage → Electric | `"4105"` (W) | `activeActualConsumption.total` |
| Lifetime energy | RFXMeter → RFXMeter counter | `"127800866"` (Wh) | `activeTotalConsumption.total` |
| Voltage (per phase) | General → Voltage | `"231"` (V) | `rmsVoltage.L1` / `L2` / `L3` |
| Current (optional) | Current (3 Phase) | `"12.6;3.1;3.3"` (A) | `rmsCurrent.L1..L3` (÷1000, sent in mA) |

### 3. Configure the bridge

Open `saveeye_to_domoticz.py` and edit the **SETTINGS** block at the top, and
the `DEVICES` list further down (set your own idx values).

| Setting | Default | Description |
|---|---|---|
| `MQTT_HOST` | `localhost` | Broker address |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USER` / `MQTT_PASS` | none | Broker login, if required |
| `SAVEEYE_TOPIC` | `saveeye/telemetry` | Topic SaveEye publishes to |
| `SAVEEYE_SERIAL` | none | If set, messages from other SaveEye units are ignored |
| `DOMOTICZ_TOPIC` | `domoticz/in` | Domoticz MQTT input topic |
| `COUNTER_OFFSET_WH` | `0` | See [Counter offset](#counter-offset) |
| `LOG_LEVEL` | `INFO` | Environment variable only. Use `DEBUG` to see every message sent |

Every setting can also be given as an environment variable of the same name,
which overrides the value in the file:

```bash
MQTT_HOST=192.168.1.10 MQTT_USER=me MQTT_PASS=secret python3 saveeye_to_domoticz.py
```

### 4. Run it

```bash
python3 saveeye_to_domoticz.py
```

Use `LOG_LEVEL=DEBUG` to see each message published to Domoticz.

## Adding another Domoticz device

Add one entry to the `DEVICES` list. For example, a 3-phase Current device
(Domoticz wants amps, SaveEye sends milliamps):

```python
Device(
    name="Current L1-L3",
    idx=579,
    paths=[("rmsCurrent", "L1"), ("rmsCurrent", "L2"), ("rmsCurrent", "L3")],
    fmt=lambda v: ";".join(f"{x / 1000:.1f}" for x in v),
),
```

| Field | Meaning |
|---|---|
| `name` | Label used in log messages |
| `idx` | Domoticz device idx |
| `paths` | Key path(s) into the SaveEye JSON. Several paths give several values |
| `fmt` | Function that turns the found values into the Domoticz `svalue` string |
| `interval` | Minimum seconds between updates (default 10) |
| `never_decrease` | Set `True` for cumulative counters to skip decreasing values |

## Counter offset

The energy counter is sent as the raw SaveEye lifetime total (in Wh). If your
existing Domoticz counter shows a different total, a mismatch will appear as a
spike in the energy graph. Avoid that by setting

```
COUNTER_OFFSET_WH = (current Domoticz counter in Wh) − (SaveEye total in Wh)
```

Also check the device's **Counter Divider** setting in Domoticz, and stop
whatever fed the device before, so two sources don't overwrite each other.

## Run as a systemd service

Create `/etc/systemd/system/saveeye.service`:

```ini
[Unit]
Description=SaveEye to Domoticz MQTT bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=saveeye
WorkingDirectory=/opt/saveeye
ExecStart=/usr/bin/python3 /opt/saveeye/saveeye_to_domoticz.py
Restart=always
RestartSec=5
# Optional: keep credentials out of the script
# EnvironmentFile=/etc/saveeye.env

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin saveeye
sudo chown root:saveeye /opt/saveeye/saveeye_to_domoticz.py
sudo chmod 640 /opt/saveeye/saveeye_to_domoticz.py
sudo systemctl daemon-reload
sudo systemctl enable --now saveeye

systemctl status saveeye
journalctl -u saveeye -f
```

If paho-mqtt is installed in a virtualenv, point `ExecStart` at that
environment's Python instead.

## Example SaveEye message

Field names as seen on a SaveEye device (values here are made up). Fields
available depend on your meter type.

```json
{
  "saveeyeDeviceSerialNumber": "A1234567",
  "meterType": "",
  "meterSerialNumber": "Not found",
  "timestamp": "2026-09-29T19:12:33",
  "wifiRssi": -79,
  "activeActualConsumption": {"total": 4105, "L1": 2837, "L2": 665, "L3": 601},
  "activeActualProduction": {"total": 0, "L1": 0, "L2": 0, "L3": 0},
  "activeTotalConsumption": {"total": 127800866},
  "activeTotalProduction": {"total": 5},
  "reactiveActualConsumption": {"total": 0},
  "reactiveActualProduction": {"total": 1222},
  "reactiveTotalConsumption": {"total": 65029},
  "reactiveTotalProduction": {"total": 26964912},
  "rmsVoltage": {"L1": 231, "L2": 235, "L3": 235},
  "rmsCurrent": {"L1": 12600, "L2": 3100, "L3": 3300},
  "powerFactor": {"total": 100}
}
```

Units, as used by this bridge: power in W, energy in Wh, voltage in V,
current in mA.

## Troubleshooting

- **Nothing happens.** Run with `LOG_LEVEL=DEBUG` and confirm messages arrive on
  `saveeye/telemetry` with `mosquitto_sub`.
- **Domoticz devices don't update.** Check that the MQTT Client Gateway is
  enabled and listening on `domoticz/in`, and that the idx values are correct.
- **Spike in the energy graph.** The counter offset is likely wrong, see
  [Counter offset](#counter-offset).
- **`AttributeError: ... CallbackAPIVersion`** You are running an old copy of
  the script on paho-mqtt 1.x. Use the current version, which supports both.

## License

Add a license of your choice (for example MIT) as a `LICENSE` file.
