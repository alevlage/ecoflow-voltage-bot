"""EcoFlow app-login (MQTT) reader — the unofficial route the EcoFlow app uses.

Logs in with your EcoFlow account, connects to EcoFlow's MQTT broker and
listens to a device's live data, which includes values the app doesn't show
(such as AC input voltage).

Usage:
    python ecoflow_mqtt.py SN            # print AC input voltage
    python ecoflow_mqtt.py SN --dump     # print every message for 20 s (to find keys)

Config (.env next to this script):
    ECOFLOW_EMAIL, ECOFLOW_PASSWORD
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import struct
import sys
import threading
import time
import uuid

import certifi
import httpx
import paho.mqtt.client as mqtt

from ecoflow_api import EcoFlowError, load_dotenv

log = logging.getLogger(__name__)

API = "https://api.ecoflow.com"

# AC input voltage keys in JSON messages (River 2 family reports millivolts).
JSON_AC_IN_VOLTAGE_KEYS = ["inv.acInVol", "plugInInfoAcInVol"]


def login(email: str, password: str) -> tuple[str, str]:
    """Return (token, user_id)."""
    resp = httpx.post(
        f"{API}/auth/login",
        json={
            "email": email,
            "password": base64.b64encode(password.encode()).decode(),
            "scene": "IOT_APP",
            "userType": "ECOFLOW",
        },
        headers={"lang": "en_US"},
        timeout=15,
    )
    body = resp.json()
    if str(body.get("code")) != "0":
        raise EcoFlowError(f"Login failed {body.get('code')}: {body.get('message')}")
    return body["data"]["token"], str(body["data"]["user"]["userId"])


def mqtt_credentials(token: str, user_id: str) -> dict:
    resp = httpx.get(
        f"{API}/iot-auth/app/certification",
        params={"userId": user_id},
        headers={"Authorization": f"Bearer {token}", "lang": "en_US"},
        timeout=15,
    )
    body = resp.json()
    if str(body.get("code")) != "0":
        raise EcoFlowError(f"MQTT credentials failed {body.get('code')}: {body.get('message')}")
    return body["data"]


# --- Minimal schema-less protobuf decoding (River 3 sends protobuf, not JSON) ---

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def decode_protobuf(buf: bytes) -> list[tuple[int, int, object]]:
    """Return [(field_number, wire_type, value)]. Raises on malformed input."""
    fields = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wire}")
        if i > len(buf):
            raise ValueError("truncated")
        fields.append((field, wire, value))
    return fields


def protobuf_to_dict(buf: bytes, depth: int = 0) -> dict:
    """Best-effort readable view: {field: value or [values]}."""
    out: dict = {}
    for field, wire, value in decode_protobuf(buf):
        if wire == 5:
            value = round(struct.unpack("<f", value)[0], 3)
        elif wire == 1:
            value = round(struct.unpack("<d", value)[0], 3)
        elif wire == 2:
            try:
                value = protobuf_to_dict(value, depth + 1) if depth < 4 and value else value.decode()
            except (ValueError, IndexError, UnicodeDecodeError):
                value = value.hex()
        out.setdefault(field, []).append(value)
    return {k: v[0] if len(v) == 1 else v for k, v in out.items()}


def decode_river3(payload: bytes) -> list[dict]:
    """Decode a River 3 style HeaderMessage into [{cmd_func, cmd_id, data}]."""
    messages = []
    for field, wire, header in decode_protobuf(payload):
        if field != 1 or wire != 2:
            continue
        h = {f: v for f, w, v in decode_protobuf(header)}
        pdata = h.get(1, b"")
        if h.get(6) == 1:  # enc_type 1: XOR with low byte of seq
            key = h.get(14, 0) & 0xFF
            pdata = bytes(b ^ key for b in pdata)
        try:
            data = protobuf_to_dict(pdata) if pdata else {}
        except (ValueError, IndexError):
            data = {"raw": pdata.hex()}
        messages.append({"cmd_func": h.get(8), "cmd_id": h.get(9), "data": data})
    return messages


class EcoFlowMqtt:
    def __init__(self, email: str, password: str):
        token, self.user_id = login(email, password)
        creds = mqtt_credentials(token, self.user_id)
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ANDROID_{uuid.uuid4().hex[:16].upper()}_{self.user_id}",
        )
        self.client.username_pw_set(creds["certificateAccount"], creds["certificatePassword"])
        self.client.tls_set(ca_certs=certifi.where(), cert_reqs=ssl.CERT_REQUIRED)
        self.host, self.port = creds["url"], int(creds["port"])
        self.on_payload = lambda topic, payload: None
        self.client.on_message = lambda c, u, msg: self.on_payload(msg.topic, msg.payload)

    def listen(self, sn: str) -> None:
        connected = threading.Event()

        def on_connect(client, userdata, flags, rc, props):
            # Subscribe on every connect: paho reconnects by itself after a
            # network drop, but the broker forgets subscriptions.
            client.subscribe(f"/app/device/property/{sn}")
            client.subscribe(f"/app/{self.user_id}/{sn}/thing/property/get_reply")
            connected.set()

        self.client.on_connect = on_connect
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        if not connected.wait(15):
            self.stop()
            raise EcoFlowError("Could not connect to EcoFlow MQTT broker")
        # Ask the device for a full snapshot (works for JSON devices like River 2).
        self.request_snapshot(sn)

    def request_snapshot(self, sn: str) -> None:
        self.client.publish(
            f"/app/{self.user_id}/{sn}/thing/property/get",
            json.dumps({
                "from": "Android", "id": str(int(time.time() * 1000)), "version": "1.0",
                "moduleType": 0, "operateType": "latestQuotas", "params": {},
            }),
        )

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def parse_payload(payload: bytes) -> dict | list:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return decode_river3(payload)


def find_ac_input(parsed) -> tuple[float, float | None] | None:
    """Return (volts, ac_input_watts) from a JSON message, or None.

    The voltage alone isn't reliable: the River 2 keeps reporting a frozen
    ~218 V on a dead socket. Input watts aren't either: at ~180 V the River 2
    doesn't charge, so inv.inputWatts stays 0 while pd.wattsInSum shows a few W.
    LiveReader combines both with whether the voltage is changing.
    """
    if isinstance(parsed, dict):
        params = parsed.get("params") or parsed.get("data", {}).get("quotaMap") or {}
        for key in JSON_AC_IN_VOLTAGE_KEYS:
            if key in params:
                value = float(params[key])
                watts = None
                if "inv.inputWatts" in params or "pd.wattsInSum" in params:
                    # Total input minus solar, so solar alone doesn't count as AC.
                    total_ac = float(params.get("pd.wattsInSum", 0)) - float(params.get("mppt.inWatts", 0))
                    watts = max(float(params.get("inv.inputWatts", 0)), total_ac, 0.0)
                return (value / 1000 if value > 1000 else value, watts)
    return None


def read_ac_input(sn: str, timeout: float = 30) -> tuple[float, float | None]:
    """Connect, wait for a message carrying AC input voltage, return (volts, input_watts)."""
    load_dotenv()
    ef = EcoFlowMqtt(os.environ["ECOFLOW_EMAIL"], os.environ["ECOFLOW_PASSWORD"])
    found: list[tuple[float, float | None]] = []
    done = threading.Event()

    def on_payload(topic, payload):
        reading = find_ac_input(parse_payload(payload))
        if reading is not None:
            found.append(reading)
            done.set()

    ef.on_payload = on_payload
    try:
        ef.listen(sn)
        if not done.wait(timeout):
            raise EcoFlowError(f"No AC input voltage received within {timeout:.0f} s")
    finally:
        ef.stop()
    return found[0]


class LiveReader:
    """Keeps one connection open and holds the latest AC input reading.

    The River 2 pushes a new voltage every ~3 s; that's as fresh as it gets.
    A snapshot is also requested every `interval` seconds, but the cloud
    answers with its copy of an earlier push, which can arrive after a newer
    push. So snapshots only fill in while the device's own pushes are missing;
    otherwise the voltage would flick back to the previous value.
    """

    # A live grid voltage changes every few seconds; a frozen one means the
    # device is showing a leftover value from a dead socket. With the power
    # on, it stayed exactly the same for at most 15 s in the log.
    FROZEN_AFTER = 20.0
    # Sometimes the device reports ~0 V instead of freezing the last voltage.
    NO_POWER_BELOW = 50.0
    # With no data for this long, log in again and reconnect: the connection
    # or its login may have expired. Also the retry interval while it fails.
    RECONNECT_AFTER = 120.0
    # Snapshots are ignored while the device pushed a voltage this recently.
    PUSH_FRESH = 10.0

    def __init__(self, sn: str, interval: float = 1.0):
        load_dotenv()
        self.sn = sn
        self.interval = interval
        self.volts: float | None = None
        self.watts: float | None = None
        self.updated_at: float = 0.0
        self.volts_changed_at: float = 0.0
        self.started_at: float = 0.0
        self._stop = threading.Event()
        self._connected_at: float = 0.0
        self.pushed_at: float = 0.0  # last voltage pushed by the device itself
        self.ef: EcoFlowMqtt | None = None

    def _connect(self) -> None:
        if self.ef:
            try:
                self.ef.stop()
            except Exception:
                pass
            self.ef = None
        self._connected_at = time.time()
        ef = EcoFlowMqtt(os.environ["ECOFLOW_EMAIL"], os.environ["ECOFLOW_PASSWORD"])
        ef.on_payload = self._on_payload
        ef.listen(self.sn)
        self.ef = ef

    def _on_payload(self, topic, payload) -> None:
        reading = find_ac_input(parse_payload(payload))
        if reading is not None:
            volts, watts = reading
            now = time.time()
            if topic.startswith("/app/device/property/"):
                self.pushed_at = now
            elif now - self.pushed_at < self.PUSH_FRESH:
                self.updated_at = now  # still connected; the push has the newer values
                return
            if self.volts is not None and volts != self.volts:
                self.volts_changed_at = now
            self.volts = volts
            if watts is not None:
                self.watts = watts
            self.updated_at = now

    def electricity_on(self) -> bool | None:
        """True/False, or None while it's too early to tell."""
        if self.volts is not None and self.volts < self.NO_POWER_BELOW:
            return False
        if self.watts:
            return True
        if time.time() - self.volts_changed_at < self.FROZEN_AFTER:
            return True
        if time.time() - self.started_at < self.FROZEN_AFTER:
            return None  # haven't watched long enough to call the voltage frozen
        return False

    def _poll(self) -> None:
        while not self._stop.wait(self.interval):
            now = time.time()
            silent_for = now - max(self.updated_at, self._connected_at)
            if self.ef is None or silent_for > self.RECONNECT_AFTER:
                log.warning("No data from EcoFlow for %.0f s, reconnecting", silent_for)
                try:
                    self._connect()
                    log.info("Reconnected to EcoFlow")
                except Exception as e:
                    log.warning("Reconnect failed (%s), retrying in %.0f s", e, self.RECONNECT_AFTER)
                continue
            try:
                self.ef.request_snapshot(self.sn)
            except Exception:
                pass  # paho reconnects by itself; the watchdog above covers the rest

    def start(self) -> None:
        """Connect and keep polling. Never raises: if the first connect fails,
        the poll thread keeps retrying."""
        self.started_at = time.time()
        try:
            self._connect()
        except Exception as e:
            log.warning("Could not connect to EcoFlow (%s), will retry", e)
        threading.Thread(target=self._poll, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.ef:
            self.ef.stop()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python ecoflow_mqtt.py SN [--dump]")
    sn = sys.argv[1]
    if "--dump" not in sys.argv:
        volts, watts = read_ac_input(sn)
        print(f"AC input voltage: {volts:.1f} V, AC input power: {watts} W")
        return

    load_dotenv()
    ef = EcoFlowMqtt(os.environ["ECOFLOW_EMAIL"], os.environ["ECOFLOW_PASSWORD"])
    ef.on_payload = lambda topic, payload: print(
        f"--- {topic}\n{json.dumps(parse_payload(payload), indent=1, default=str, ensure_ascii=False)}",
        flush=True,
    )
    ef.listen(sn)
    time.sleep(20)
    ef.stop()


if __name__ == "__main__":
    main()
