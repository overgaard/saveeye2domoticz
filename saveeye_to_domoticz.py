#!/usr/bin/env python3
# Copyright (c) 2026 Jörgen Overgaard <jorgen.overgaard@telia.com>. Licensed under the MIT License (see LICENSE).
"""Bridge: SaveEye MQTT telemetry -> Domoticz MQTT input (domoticz/in).

What this script does
---------------------
1. Connects to your MQTT broker and subscribes to the SaveEye topic.
2. Every time SaveEye publishes a JSON message, it walks through the
   DEVICES table below.
3. For each entry it picks the value(s) out of the SaveEye JSON, converts
   them to the text format Domoticz expects, and publishes
   {"idx": ..., "nvalue": 0, "svalue": "..."} to the domoticz/in topic.

To add a new Domoticz device you only need to add ONE entry to DEVICES
(see the "HOW TO ADD A DEVICE" section below). Nothing else has to change.

Requires: pip install paho-mqtt   (works with both v1.x and v2.x)

Settings: edit the defaults in the SETTINGS block below, or override any of
them with an environment variable of the same name (MQTT_HOST, MQTT_PORT,
MQTT_USER, MQTT_PASS, SAVEEYE_TOPIC, SAVEEYE_SERIAL, DOMOTICZ_TOPIC,
COUNTER_OFFSET_WH). LOG_LEVEL (default INFO) is env-only; use DEBUG to see
every message sent to Domoticz.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

import paho.mqtt.client as mqtt

log = logging.getLogger("saveeye2domoticz")

# ---------------------------------------------------------------------------
# SETTINGS - edit the default values here (the text after the comma).
# An environment variable with the same name, if set, overrides the default.
# ---------------------------------------------------------------------------
def setting(name, default):
    """Return environment variable `name` if set and non-empty, else `default`."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


MQTT_HOST = setting("MQTT_HOST", "localhost")         # Broker address
MQTT_PORT = int(setting("MQTT_PORT", 1883))           # Broker port
MQTT_USER = setting("MQTT_USER", None)                # None = no login
MQTT_PASS = setting("MQTT_PASS", None)
SAVEEYE_TOPIC = setting("SAVEEYE_TOPIC", "saveeye/telemetry")
SAVEEYE_SERIAL = setting("SAVEEYE_SERIAL", None)      # e.g. "A397B7S9"; None = accept any
DOMOTICZ_TOPIC = setting("DOMOTICZ_TOPIC", "domoticz/in")

# Counter offset: added to the SaveEye lifetime total (in Wh) before it is
# sent to the Domoticz counter. Set it to (old Domoticz counter - SaveEye
# total) if the two do not match, so the energy history continues without a
# jump. Use 0 if the Domoticz counter already equals the meter's total.
COUNTER_OFFSET_WH = int(setting("COUNTER_OFFSET_WH", 0))


# ---------------------------------------------------------------------------
# Device mapping
# ---------------------------------------------------------------------------
@dataclass
class Device:
    """One Domoticz device fed from the SaveEye JSON."""

    name: str                  # Only used in log messages.
    idx: int                   # Domoticz device idx.
    paths: Sequence[Sequence[str]]  # Where to find the value(s) in the JSON.
    #   Each path is a tuple of keys, e.g. ("rmsVoltage", "L1") means
    #   data["rmsVoltage"]["L1"]. Several paths = several values (used for
    #   3-phase devices that need "L1;L2;L3" in one svalue).
    fmt: Callable[[list], str] = lambda values: str(values[0])
    #   Turns the list of found values into the Domoticz svalue string.
    interval: int = 10         # Minimum seconds between updates.
    never_decrease: bool = False
    #   True for cumulative counters: a value lower than the last one sent
    #   is skipped (protects the graphs from glitches).

    # Runtime state (not configuration):
    _last_sent: float = field(default=None, repr=False)  # None = never sent
    _last_value: float = field(default=None, repr=False)


# ===========================================================================
# HOW TO ADD A DEVICE
# ===========================================================================
# Copy one of the entries below, change idx and paths, and adjust fmt.
#   * name      any label, shows up in logs
#   * idx       the Domoticz device idx
#   * paths     key path(s) into the SaveEye JSON
#   * fmt       function turning the found values into the svalue text
#   * interval  minimum seconds between updates for this device
#
# Example - a 3-phase Current device (Domoticz wants "A1;A2;A3" in amps,
# SaveEye sends milliamps). Uncomment and put in your real idx:
#
#   Device(
#       name="Current L1-L3",
#       idx=579,
#       paths=[("rmsCurrent", "L1"), ("rmsCurrent", "L2"), ("rmsCurrent", "L3")],
#       fmt=lambda v: ";".join(f"{x / 1000:.1f}" for x in v),
#   ),
#
# Example - a single-phase Current device (one device per phase):
#
#   Device(
#       name="Current L1",
#       idx=579,
#       paths=[("rmsCurrent", "L1")],
#       fmt=lambda v: f"{v[0] / 1000:.1f}",
#   ),
# ===========================================================================
DEVICES = [
    # Current power in W -> Usage (Electric) device.
    Device(
        name="Power",
        idx=572,
        paths=[("activeActualConsumption", "total")],
        fmt=lambda v: str(int(v[0])),
        interval=10,
    ),
    # Lifetime energy in Wh -> RFXMeter counter device.
    Device(
        name="Energy counter",
        idx=575,
        paths=[("activeTotalConsumption", "total")],
        fmt=lambda v: str(int(v[0]) + COUNTER_OFFSET_WH),
        interval=60,
        never_decrease=True,
    ),
    # Voltage per phase (V) -> General/Voltage devices.
    Device(
        name="Voltage L1",
        idx=576,
        paths=[("rmsVoltage", "L1")],
        interval=60,
    ),
    Device(
        name="Voltage L2",
        idx=577,
        paths=[("rmsVoltage", "L2")],
        interval=60,
    ),
    Device(
        name="Voltage L3",
        idx=578,
        paths=[("rmsVoltage", "L3")],
        interval=60,
    ),
]


# ---------------------------------------------------------------------------
# Core logic (normally no need to edit below this line)
# ---------------------------------------------------------------------------
def dig(data, path):
    """Follow a key path into nested dicts. Returns None if any key is missing."""
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def send(client, idx, svalue):
    """Publish one update to Domoticz in its MQTT input format."""
    payload = json.dumps({"idx": idx, "nvalue": 0, "svalue": svalue})
    client.publish(DOMOTICZ_TOPIC, payload)
    log.debug("-> %s %s", DOMOTICZ_TOPIC, payload)


def process(client, data):
    """Handle one SaveEye message: update every device that is due."""
    # Ignore messages from a different SaveEye unit, if a serial is set.
    if SAVEEYE_SERIAL and data.get("saveeyeDeviceSerialNumber") != SAVEEYE_SERIAL:
        return

    now = time.monotonic()
    for dev in DEVICES:
        # Throttle: skip if this device was updated too recently.
        if dev._last_sent is not None and now - dev._last_sent < dev.interval:
            continue

        # Collect the value(s); skip the device if any are missing.
        values = [dig(data, path) for path in dev.paths]
        if any(v is None for v in values):
            log.debug("%s: value missing in message, skipped", dev.name)
            continue

        svalue = dev.fmt(values)

        # Counter sanity check: never send a lower value than before.
        if dev.never_decrease:
            number = float(svalue)
            if dev._last_value is not None and number < dev._last_value:
                log.warning("%s went backwards (%s -> %s), skipped",
                            dev.name, dev._last_value, number)
                continue
            dev._last_value = number

        send(client, dev.idx, svalue)
        dev._last_sent = now


def on_connect(client, userdata, flags, reason_code, properties=None):
    """Called on every (re)connect; subscribing here survives reconnects.

    Works with paho-mqtt 1.x (reason_code is an int, 0 = OK) and 2.x
    (reason_code is a ReasonCode object).
    """
    failed = reason_code.is_failure if hasattr(reason_code, "is_failure") \
        else reason_code != 0
    if failed:
        log.error("MQTT connect failed: %s", reason_code)
        return
    log.info("Connected to %s:%s, subscribing to %s",
             MQTT_HOST, MQTT_PORT, SAVEEYE_TOPIC)
    client.subscribe(SAVEEYE_TOPIC)


def on_message(client, userdata, msg):
    """Called for every SaveEye message. Errors are logged, never fatal."""
    try:
        process(client, json.loads(msg.payload))
    except Exception:
        log.exception("Failed to process message: %r", msg.payload[:200])


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # paho-mqtt 2.x requires a callback API version; 1.x does not have one.
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    else:
        client = mqtt.Client()
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    # loop_forever handles automatic reconnects if the broker goes away.
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
