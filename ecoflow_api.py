"""Minimal EcoFlow Developer (IoT Open) API client.

Reads the AC input voltage of a device such as a River 3 Max.

Usage:
    python ecoflow_api.py            # print AC input voltage
    python ecoflow_api.py --dump     # print every quota key (to find the right one)
    python ecoflow_api.py --devices  # list devices bound to your account

Config comes from environment variables (or a .env file next to this script):
    ECOFLOW_ACCESS_KEY, ECOFLOW_SECRET_KEY, ECOFLOW_SN,
    ECOFLOW_HOST (default https://api-e.ecoflow.com; US accounts: https://api-a.ecoflow.com)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import sys
import time
from pathlib import Path

import httpx

# Candidate keys for AC input voltage, most likely first. River 3 uses
# plugInInfoAcInVol (volts); older models used inv.acInVol (millivolts).
AC_IN_VOLTAGE_KEYS = [
    "plugInInfoAcInVol",
    "inv.acInVol",
    "acInVol",
]


def load_dotenv(path: Path = Path(__file__).with_name(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class EcoFlowError(Exception):
    pass


class EcoFlowClient:
    def __init__(self, access_key: str, secret_key: str, host: str = "https://api-e.ecoflow.com"):
        self.access_key = access_key
        self.secret_key = secret_key
        self.host = host.rstrip("/")
        self.http = httpx.Client(timeout=15)

    def _headers(self, params: dict) -> dict:
        nonce = str(random.randint(100000, 999999))
        timestamp = str(int(time.time() * 1000))
        query = "&".join(f"{k}={params[k]}" for k in sorted(params))
        to_sign = f"{query}&" if query else ""
        to_sign += f"accessKey={self.access_key}&nonce={nonce}&timestamp={timestamp}"
        sign = hmac.new(self.secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        return {"accessKey": self.access_key, "nonce": nonce, "timestamp": timestamp, "sign": sign}

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        params = params or {}
        resp = self.http.get(self.host + path, params=params, headers=self._headers(params))
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("code")) != "0":
            raise EcoFlowError(f"EcoFlow API error {body.get('code')}: {body.get('message')}")
        return body.get("data")

    def devices(self) -> list:
        return self._get("/iot-open/sign/device/list")

    def quota_all(self, sn: str) -> dict:
        return self._get("/iot-open/sign/device/quota/all", {"sn": sn})

    def ac_input_voltage(self, sn: str) -> float:
        """Return AC input voltage in volts. Raises EcoFlowError if not found."""
        data = self.quota_all(sn)
        if not data:
            raise EcoFlowError("API returned no data for this device (it may not be supported by the Developer API)")
        for key in AC_IN_VOLTAGE_KEYS:
            if key in data:
                value = float(data[key])
                # Older models report millivolts.
                return value / 1000 if value > 1000 else value
        # Fall back to any key that looks like AC input voltage.
        for key, value in data.items():
            if "acinvol" in key.lower().replace("_", "").replace(".", ""):
                value = float(value)
                return value / 1000 if value > 1000 else value
        raise EcoFlowError("No AC input voltage key in quota data; run with --dump to inspect keys")


def client_from_env() -> tuple[EcoFlowClient, str]:
    load_dotenv()
    try:
        client = EcoFlowClient(
            os.environ["ECOFLOW_ACCESS_KEY"],
            os.environ["ECOFLOW_SECRET_KEY"],
            os.environ.get("ECOFLOW_HOST", "https://api-e.ecoflow.com"),
        )
        return client, os.environ.get("ECOFLOW_SN", "")
    except KeyError as e:
        sys.exit(f"Missing environment variable {e}")


def main() -> None:
    client, sn = client_from_env()
    if "--devices" in sys.argv:
        print(json.dumps(client.devices(), indent=2, ensure_ascii=False))
        return
    if not sn:
        sys.exit("Set ECOFLOW_SN (run with --devices to find it)")
    if "--dump" in sys.argv:
        print(json.dumps(client.quota_all(sn), indent=2, sort_keys=True, ensure_ascii=False))
        return
    print(f"AC input voltage: {client.ac_input_voltage(sn):.1f} V")


if __name__ == "__main__":
    main()
