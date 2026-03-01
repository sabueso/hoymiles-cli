#!/usr/bin/env python3
"""
Minimal CLI for Hoymiles Cloud (neapi.hoymiles.com) based on the Home Assistant
"hoymiles_cloud" custom component logic, plus extra helpers to read ESS/battery
telemetry and (optionally) SET ESS modes using the same payload as the Hoymiles web UI.

Common usage
  # List stations
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' --list-stations --pretty

  # Real-time station data (PV / grid / load + (if present) ESS reflux_station_data)
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' --station-id 9258040 --realtime --pretty

  # Battery/ESS from realtime (works even if settings endpoints are restricted)
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' --station-id 9258040 --battery-from-realtime --pretty

  # Battery/ESS settings (mode/reserve_soc) via pvm-ctl (may return No Permission on some accounts)
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' --station-id 9258040 --battery --pretty

Set ESS/Battery mode (observed from Hoymiles web UI)
---------------------------------------------------
This uses:
  POST https://neapi.hoymiles.com/pvm-ctl/api/0/dev/setting/write
  payload: {"action":1013,"data":{"sid":<SID>,"data":{"mode":<MODE>,"data":{...}}}}

Modes (commonly seen):
  1 = Self-Consumption (auto-consumo)
  8 = Time of Use (rangos horarios)
  3 = Backup
  2 = Economy
  4 = Off-Grid
  7 = Peak Shaving

Examples:

  # Set Self-Consumption (mode=1) with reserve SOC 25%
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' --station-id 9258040 --set-mode 1 --reserve-soc 25 --pretty

  # Set Time-of-Use (mode=8) with a schedule (inline JSON)
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' --station-id 9258040 --set-mode 8 --reserve-soc 25 \
        --tou-time-json '[{"cs_time":"01:45","ce_time":"06:00","c_power":40,"dcs_time":"08:00","dce_time":"23:59","dc_power":100,"charge_soc":95,"dis_charge_soc":25}]' --pretty

  # Same Time-of-Use schedule, but loaded from a file schedule.json
  # schedule.json must contain ONLY the list (time=[...]) like:
  # [
  #   {"cs_time":"01:45","ce_time":"06:00","c_power":40,"dcs_time":"08:00","dce_time":"23:59","dc_power":100,"charge_soc":95,"dis_charge_soc":25}
  # ]
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' --station-id 9258040     --set-mode 8 --reserve-soc 25 --tou-time-file schedule.json --pretty

Set Max. Discharging Power
--------------------------
Uses the exact same flow the Hoymiles web UI uses (verified from HAR capture):
  1. POST /pvm-ctl/api/0/dev/config/fetch        – trigger device config upload
  2. Poll /pvm-ctl/api/0/dev/config/fetch_status  – wait for live config (code=0, rate=100)
  3. Patch ONLY item id=15828 content=str(pct), change stays null (HAR-verified)
  4. POST /pvm-ctl/api/0/dev/config/put           – write patched config back
  5. Poll /pvm-ctl/api/0/dev/config/put_status    – wait for device confirmation

The value is a PERCENTAGE (20–100, minimum 20%), not watts.

  # Set Max Discharging Power to 50%
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' \\
        --set-max-discharging-power 50 \\
        --dev-sn 208324250511 \\
        --dtu-sn 430123526317 \\
        --pretty

  # Override poll timeout if device is slow (default 60 s, 2 s interval):
  ./hoymiles_cli_setmode.py -u you@example.com -p 'secret' \\
        --set-max-discharging-power 100 \\
        --dev-sn 208324250511 --dtu-sn 430123526317 \\
        --poll-timeout 90 --poll-interval 3 --pretty

Notes
- The API expects the password as an MD5 hex digest (no "Bearer" prefix for Authorization).
- Output is JSON to stdout.
- Changing modes can affect charging from grid and reserve behavior. Use with care.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, Optional

import requests

API_BASE_URL = "https://neapi.hoymiles.com"
API_AUTH_URL = f"{API_BASE_URL}/iam/pub/0/auth/login"
API_STATIONS_URL = f"{API_BASE_URL}/pvm/api/0/station/select_by_page"
API_REAL_TIME_DATA_URL = f"{API_BASE_URL}/pvm-data/api/0/station/data/count_station_real_data"
API_MICROINVERTERS_URL = f"{API_BASE_URL}/pvm/api/0/dev/micro/select_by_station"
API_MICRO_DETAIL_URL = f"{API_BASE_URL}/pvm/api/0/dev/micro/find"

API_BATTERY_SETTINGS_STATUS_URL = f"{API_BASE_URL}/pvm-ctl/api/0/dev/setting/status"
API_BATTERY_SETTINGS_READ_URL = f"{API_BASE_URL}/pvm-ctl/api/0/dev/setting/read"
API_BATTERY_SETTINGS_WRITE_URL = f"{API_BASE_URL}/pvm-ctl/api/0/dev/setting/write"
API_DEV_CONFIG_FETCH_URL        = f"{API_BASE_URL}/pvm-ctl/api/0/dev/config/fetch"
API_DEV_CONFIG_FETCH_STATUS_URL = f"{API_BASE_URL}/pvm-ctl/api/0/dev/config/fetch_status"
API_DEV_CONFIG_PUT_URL          = f"{API_BASE_URL}/pvm-ctl/api/0/dev/config/put"
API_DEV_CONFIG_PUT_STATUS_URL   = f"{API_BASE_URL}/pvm-ctl/api/0/dev/config/put_status"

BATTERY_MODES = {
    1: "Self-Consumption",
    2: "Economy",
    3: "Backup",
    4: "Off-Grid",
    7: "Peak Shaving",
    8: "Time of Use",
}


class HoymilesClientError(RuntimeError):
    pass


class HoymilesClient:
    def __init__(self, username: str, password: str, timeout: int = 25, verify_tls: bool = True) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.session = requests.Session()
        self.token: Optional[str] = None

    def _post(self, url: str, json_body: Dict[str, Any], *, auth: bool = True) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth:
            if not self.token:
                raise HoymilesClientError("Not authenticated (token missing).")
            headers["Authorization"] = self.token  # IMPORTANT: no 'Bearer ' prefix

        try:
            r = self.session.post(
                url,
                headers=headers,
                json=json_body,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except requests.RequestException as e:
            raise HoymilesClientError(f"HTTP request failed: {e}") from e

        # Some endpoints sometimes respond with text; be defensive.
        try:
            data = r.json()
        except ValueError:
            raise HoymilesClientError(f"Non-JSON response ({r.status_code}): {r.text[:500]}")

        if not isinstance(data, dict):
            raise HoymilesClientError(f"Unexpected response type: {type(data)}")

        # Hoymiles uses status="0" for success
        if data.get("status") == "0" and data.get("message") == "success":
            return data
        
        raise HoymilesClientError(f"API error: status={data.get('status')} message={data.get('message')} body={data}")

    def _post_raw(self, url: str, json_body: Dict[str, Any], *, auth: bool = True) -> Dict[str, Any]:
        """POST and return parsed JSON without enforcing Hoymiles status/message semantics.
        Useful for endpoints that may return 'No Permission' (e.g., battery settings on accounts without ESS)."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth:
            if not self.token:
                raise HoymilesClientError("Not authenticated (token missing).")
            headers["Authorization"] = self.token

        try:
            r = self.session.post(
                url,
                headers=headers,
                json=json_body,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except requests.RequestException as e:
            raise HoymilesClientError(f"HTTP request failed: {e}") from e

        try:
            data = r.json()
        except ValueError:
            raise HoymilesClientError(f"Non-JSON response ({r.status_code}): {r.text[:500]}")

        if not isinstance(data, dict):
            raise HoymilesClientError(f"Unexpected response type: {type(data)}")
        return data

    def authenticate(self) -> str:
        md5_password = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        payload = {
            "user_name": self.username,
            "password": md5_password,
        }
        try:
            r = self.session.post(
                API_AUTH_URL,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except requests.RequestException as e:
            raise HoymilesClientError(f"Auth request failed: {e}") from e

        try:
            data = r.json()
        except ValueError:
            raise HoymilesClientError(f"Auth returned non-JSON ({r.status_code}): {r.text[:500]}")

        if data.get("status") == "0" and data.get("message") == "success":
            token = (data.get("data") or {}).get("token")
            if not token:
                raise HoymilesClientError(f"Auth succeeded but token missing: {data}")
            self.token = token
            return token

        raise HoymilesClientError(f"Authentication failed: status={data.get('status')} message={data.get('message')}")

    def stations(self, page_size: int = 10, page_num: int = 1) -> Dict[str, Any]:
        resp = self._post(API_STATIONS_URL, {"page_size": page_size, "page_num": page_num}, auth=True)
        return resp.get("data") or {}

    def list_stations(self) -> Dict[str, str]:
        data = self.stations(page_size=50, page_num=1)
        stations = {}
        for st in (data.get("list") or []):
            sid = str(st.get("id"))
            name = st.get("name")
            if sid and name:
                stations[sid] = name
        return stations

    def realtime(self, station_id: str) -> Dict[str, Any]:
        resp = self._post(API_REAL_TIME_DATA_URL, {"sid": int(station_id)}, auth=True)
        return resp.get("data") or {}

    def micro_list(self, station_id: str) -> Dict[str, Any]:
        payload = {"sid": int(station_id), "page_size": 1000, "page_num": 1, "show_warn": 0}
        resp = self._post(API_MICROINVERTERS_URL, payload, auth=True)
        return resp.get("data") or {}

    def micro_details(self, station_id: str, micro_id: str) -> Dict[str, Any]:
        resp = self._post(API_MICRO_DETAIL_URL, {"id": int(micro_id), "sid": int(station_id)}, auth=True)
        return resp.get("data") or {}
   
    def write_setting(self, action: int, sid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Write settings using pvm-ctl dev/setting/write with the payload observed in the web UI."""
        payload = {
            "action": int(action),
            "data": {
                "sid": int(sid),
                "data": data,
            },
        }
        return self._post(API_BATTERY_SETTINGS_WRITE_URL, payload, auth=True)

    def set_battery_mode(self, station_id: str, mode: int, reserve_soc: int = 25, tou_time: Optional[list] = None) -> Dict[str, Any]:
        """Set ESS/battery mode using web UI payload pattern (action=1013).

        For mode 8 (Time of Use), pass tou_time as a list of schedule dicts like the UI payload.
        """
        mode_i = int(mode)
        data: Dict[str, Any] = {"mode": mode_i, "data": {"reserve_soc": int(reserve_soc)}}
        if mode_i == 8:
            data["data"]["time"] = tou_time or []
        return self.write_setting(1013, station_id, data)

    # ------------------------------------------------------------------ #
    #  ESS Advanced Config  –  fetch → modify → put → confirm           #
    # ------------------------------------------------------------------ #

    # Fixed group structure for /config/put (verified from HAR).
    # fetch_status returns a flat list; PUT expects these two groups.
    _PUT_GROUPS = [
        {"id": 138, "name": "ESS Advanced Config", "item_ids": [
            12663, 12665, 16364, 18109, 16365, 18110, 30819, 12667,
            13173, 13182, 12664, 12668, 15828, 15827, 12671, 12672,
            12673, 14390, 14391, 12670, 14392, 12674, 12675, 12658,
            12659, 14459, 12660, 14604, 14605, 14606, 14607, 14608,
            14609, 14610, 14611, 14455, 15372, 15373, 15374, 15375, 17816,
        ]},
        {"id": 136, "name": "ESS Safety Config", "item_ids": [
            12683, 21478, 21479, 21480, 21482, 21483, 21484, 21485,
            21488, 21489, 21490, 21491, 21492, 21493, 21494, 21495,
            21499, 21500, 21501, 21502, 21503, 21504, 21505, 21506,
            21509, 21510, 21511, 21512, 21513, 21514, 21515, 21516,
            21519, 21520, 21521, 21524, 21525, 21526, 21527, 21553,
            21554, 21555, 21556, 21557, 21558, 21559, 21565, 21566,
            27304, 21567, 27305, 21585, 21586, 21587, 21588, 21589,
            21613, 21614, 21615, 21616, 36298, 36291, 36292, 36293,
        ]},
    ]

    def get_dev_config(
        self,
        dev_sn: str,
        dtu_sn: str,
        cfg_type: int = 0,
        poll_interval: float = 2.0,
        poll_timeout: float = 60.0,
    ) -> Dict[int, Any]:
        """Fetch the current ESS config from the device.

        fetch_status returns a FLAT list of all ~253 config items (not grouped).
        This method returns a dict keyed by item id for O(1) lookup:
            { 15828: {"id": 15828, "name": "Max. Discharging Power",
                      "content": 25.0, "change": None, ...}, ... }
        """
        fetch_resp = self._post(
            API_DEV_CONFIG_FETCH_URL,
            {"cfg_type": cfg_type, "dev_sn": dev_sn, "dtu_sn": dtu_sn},
        )
        job_id = str(fetch_resp.get("data") or "")
        if not job_id:
            raise HoymilesClientError(
                f"config/fetch did not return a job id: {fetch_resp}"
            )

        deadline = time.monotonic() + poll_timeout
        while True:
            status_resp = self._post_raw(
                API_DEV_CONFIG_FETCH_STATUS_URL, {"id": job_id}
            )
            d = status_resp.get("data") or {}
            code = d.get("code")
            rate = d.get("rate")
            flat_list = d.get("data")

            if code == 0 and rate == 100:
                if not isinstance(flat_list, list) or not flat_list:
                    raise HoymilesClientError(
                        f"fetch_status code=0 rate=100 but data.data is not a "
                        f"non-empty list (got {type(flat_list).__name__}). "
                        f"Response: {str(status_resp)[:500]}"
                    )
                return {item["id"]: item for item in flat_list if "id" in item}

            if time.monotonic() > deadline:
                raise HoymilesClientError(
                    f"Timed out waiting for fetch_status after {poll_timeout}s "
                    f"(job_id={job_id}, last code={code}, rate={rate})"
                )
            time.sleep(poll_interval)

    def _build_put_payload(
        self,
        item_index: Dict[int, Any],
        dev_sn: str,
        dtu_sn: str,
        rid: int,
    ) -> Dict[str, Any]:
        """Build the grouped PUT payload from the flat item index.

        For each item in the fixed group structure, copy content and change
        from the live device values. Items missing from the fetch response
        are sent with content=null, change=null (safe fallback).
        """
        groups = []
        for group_def in self._PUT_GROUPS:
            items = []
            for item_id in group_def["item_ids"]:
                live = item_index.get(item_id, {})
                item: Dict[str, Any] = {
                    "id": item_id,
                    "type": live.get("type"),
                    "name": live.get("name", ""),
                    "content": live.get("content"),
                    "change": live.get("change"),
                    "stc_id": live.get("stc_id"),
                }
                if "sub_id" in live:
                    item["sub_id"] = live["sub_id"]
                if "mark" in live:
                    item["mark"] = live["mark"]
                items.append(item)
            groups.append({"id": group_def["id"], "name": group_def["name"], "list": items})
        return {"data": groups, "dev_sn": dev_sn, "dtu_sn": dtu_sn, "rid": int(rid)}

    def _wait_put_status(
        self,
        job_id: str,
        poll_interval: float = 2.0,
        poll_timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Poll /config/put_status until device confirms write (code=0, rate=100)."""
        deadline = time.monotonic() + poll_timeout
        while True:
            resp = self._post_raw(API_DEV_CONFIG_PUT_STATUS_URL, {"id": job_id})
            d = resp.get("data") or {}
            if d.get("code") == 0 and d.get("rate") == 100:
                return d
            if time.monotonic() > deadline:
                raise HoymilesClientError(
                    f"Timed out waiting for put_status after {poll_timeout}s "
                    f"(job_id={job_id}, last code={d.get('code')}, rate={d.get('rate')})"
                )
            time.sleep(poll_interval)

    def set_max_discharging_power(
        self,
        dev_sn: str,
        dtu_sn: str,
        max_discharging_power: int,
        rid: int = 54,
        poll_interval: float = 2.0,
        poll_timeout: float = 60.0,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Set Max. Discharging Power (%) using fetch → patch → put → confirm.

        Flow (verified from full HAR capture):
          1. POST /config/fetch        – trigger device to upload its config
          2. Poll /config/fetch_status – wait for FLAT item list (code=0, rate=100)
          3. Build grouped PUT payload  – populate content/change from live values
          4. Patch only id=15828        – set content=str(pct), change stays null
          5. POST /config/put           – write patched config back
          6. Poll /config/put_status    – wait for device confirmation (code=0, rate=100)

        NOTE: fetch returns a flat list of ~253 items; PUT expects a grouped
        structure. _build_put_payload handles that translation using live values,
        so nothing is ever hardcoded from the script.

        Args:
            dev_sn:                 Device serial number (e.g. '208324250511')
            dtu_sn:                 DTU serial number    (e.g. '430123526317')
            max_discharging_power:  Percentage 20-100 (NOT watts; minimum 20%)
            rid:                    Request ID (default 54, matches web UI)
            poll_interval:          Seconds between status polls (default 2)
            poll_timeout:           Max seconds to wait per polling phase (default 60)
            dry_run:                If True, skip PUT and return the payload that
                                    would have been sent for manual verification.
        """
        pct = int(max_discharging_power)
        if not (20 <= pct <= 100):
            raise HoymilesClientError(
                f"max_discharging_power must be a percentage between 20 and 100, got {pct}"
            )

        # Steps 1+2: fetch flat item index from device
        item_index = self.get_dev_config(
            dev_sn, dtu_sn,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
        )

        # Step 3: build grouped PUT payload using live values
        put_payload = self._build_put_payload(item_index, dev_sn, dtu_sn, rid)

        # Step 4: patch only id=15828 — content=str(pct), change stays null per HAR
        TARGET_ITEM_ID = 15828
        old_value = (item_index.get(TARGET_ITEM_ID) or {}).get("content")
        patched = False
        for group in put_payload["data"]:
            for item in group["list"]:
                if item["id"] == TARGET_ITEM_ID:
                    item["content"] = str(pct)
                    item["change"] = None
                    patched = True

        if not patched:
            raise HoymilesClientError(
                f"Item id={TARGET_ITEM_ID} (Max. Discharging Power) not found "
                f"in PUT group template. This should never happen."
            )

        # Step 5: dry-run — show what would be sent without calling PUT
        if dry_run:
            changed_items = [
                {"group": g["name"], "id": i["id"], "name": i["name"],
                 "content": i["content"], "change": i["change"]}
                for g in put_payload["data"]
                for i in g["list"]
                if i.get("change") is not None
            ]
            return {
                "dry_run": True,
                "old_value_pct": int(float(old_value)) if old_value is not None else None,
                "new_value_pct": pct,
                "items_with_non_null_change": changed_items,
                "full_put_payload": put_payload,
            }

        # Steps 5+6: write and wait for device confirmation
        put_resp = self._post(API_DEV_CONFIG_PUT_URL, put_payload)
        put_job_id = str(put_resp.get("data") or "")
        if not put_job_id:
            raise HoymilesClientError(f"config/put did not return a job id: {put_resp}")
        confirm = self._wait_put_status(put_job_id, poll_interval=poll_interval, poll_timeout=poll_timeout)
        return {
            "success": True,
            "old_value_pct": int(float(old_value)) if old_value is not None else None,
            "new_value_pct": pct,
            "put_job_id": put_job_id,
            "device_confirm": confirm,
        }

    def battery_settings_status(self, station_id: str) -> Dict[str, Any]:
        # NOTE: HA integration uses the STATUS endpoint because it returns the current mode + per-mode data.
        # Some accounts/devices return status=3 "No Permission" when no ESS/battery is present or exposed.
        resp = self._post_raw(API_BATTERY_SETTINGS_STATUS_URL, {"id": str(station_id)}, auth=True)
        return resp

    def battery_settings(self, station_id: str) -> Dict[str, Any]:
        resp = self.battery_settings_status(station_id)

        # Graceful handling for accounts without battery/permission
        if resp.get("status") == "3" and str(resp.get("message", "")).lower().startswith("no permission"):
            return {
                "supported": False,
                "reason": "No Permission (likely no battery/ESS on this station, or the account lacks permission)",
                "raw": resp,
            }

        # enforce success for other cases
        if not (resp.get("status") == "0" and resp.get("message") == "success"):
            return {
                "supported": False,
                "reason": f"API error: status={resp.get('status')} message={resp.get('message')}",
                "raw": resp,
            }

        data = resp.get("data") or {}

        # Best-effort normalization:
        # Some accounts return: {"data": {"data": {"mode": 1, "data": {...}}}}
        # We'll try to flatten to:
        # {"mode": <int>, "mode_name": "...", "reserve_soc": <int|None>, "raw": <original>}
        mode = None
        reserve_soc = None
        raw = data

        try:
            inner = (data.get("data") or {})
            if isinstance(inner, dict):
                mode = inner.get("mode")
                mode_data = inner.get("data") or {}
                if isinstance(mode, int) and isinstance(mode_data, dict):
                    # mode-specific keys are usually k_1, k_2, ...
                    k = f"k_{mode}"
                    if k in mode_data and isinstance(mode_data[k], dict):
                        reserve_soc = mode_data[k].get("reserve_soc")
        except Exception:
            pass

        return {
            "mode": mode,
            "mode_name": BATTERY_MODES.get(mode, "unknown") if isinstance(mode, int) else None,
            "reserve_soc": reserve_soc,
            "raw": raw,
        }

    def battery_from_realtime(self, station_id: str) -> Dict[str, Any]:
        """Best-effort battery info from realtime endpoint (works even if settings endpoints are forbidden)."""
        rt = self.realtime(station_id) or {}
        rsd = rt.get("reflux_station_data") or {}
        return {
            "bms_soc": rsd.get("bms_soc"),
            "bms_power": rsd.get("bms_power"),
            "pv_power": rsd.get("pv_power"),
            "grid_power": rsd.get("grid_power"),
            "load_power": rsd.get("load_power"),
            "work_mode": rsd.get("work_mode"),  # numeric; mapping is vendor-specific
            "raw_reflux_station_data": rsd,
        }

    def all_micro_details(self, station_id: str) -> Dict[str, Any]:
        details = {}
        ml = self.micro_list(station_id)
        for item in (ml.get("list") or []):
            mid = item.get("id")
            if mid is None:
                continue
            mid_s = str(mid)
            try:
                details[mid_s] = self.micro_details(station_id, mid_s)
            except HoymilesClientError as e:
                details[mid_s] = {"error": str(e)}
        return details

def _pick_station_id(client: HoymilesClient, station_id: Optional[str]) -> str:
    if station_id:
        return station_id
    stations = client.list_stations()
    if not stations:
        raise HoymilesClientError("No stations found for this account.")
    # pick first
    return next(iter(stations.keys()))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Hoymiles Cloud CLI (based on HA hoymiles_cloud integration).",
        epilog=(
            "Credentials can also be set in a .env file (or environment variables):\n"
            "  HOYMILES_USERNAME=you@example.com\n"
            "  HOYMILES_PASSWORD=secret\n"
            "  HOYMILES_DEV_SN=208324250511\n"
            "  HOYMILES_DTU_SN=430123526317\n"
            "  HOYMILES_STATION_ID=9258040\n"
            "CLI arguments always take priority over .env values."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-u", "--username", default=None, help="Hoymiles account username/email (or set HOYMILES_USERNAME in .env)")
    p.add_argument("-p", "--password", default=None, help="Hoymiles account password (plain; will be MD5 hashed) (or set HOYMILES_PASSWORD in .env)")
    p.add_argument("--station-id", default=None, help="Station ID (sid). If omitted, uses HOYMILES_STATION_ID from .env or the first station found.")
    p.add_argument("--micro-id", help="Microinverter ID for --micro-detail")
    p.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconds (default 25)")
    p.add_argument("--insecure", action="store_true", help="Disable TLS verification (not recommended)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    p.add_argument("--list-stations", action="store_true", help="List station IDs and names")
    p.add_argument("--realtime", action="store_true", help="Get station real-time data")
    p.add_argument("--micro-list", action="store_true", help="List microinverters for the station (basic list)")
    p.add_argument("--micro-detail", action="store_true", help="Get microinverter detail (requires --micro-id)")
    p.add_argument("--micro-details", action="store_true", help="Get details for ALL microinverters in the station")
    p.add_argument("--battery", action="store_true", help="Get battery mode/settings via pvm-ctl settings endpoints (may be restricted)")
    p.add_argument("--battery-from-realtime", action="store_true", help="Get battery SOC/power/work_mode from realtime data (fallback)")
    p.add_argument("--set-mode", type=int, help="Set battery/ESS mode (e.g. 1=Self-Consumption, 8=Time of Use). Uses pvm-ctl setting/write action=1013.")
    p.add_argument("--reserve-soc", type=int, default=25, help="Reserve SOC %% to keep in battery (default 25). Used for --set-mode.")
    p.add_argument("--tou-time-json", help="For --set-mode 8: JSON string with the time schedule list (time=[...])")
    p.add_argument("--tou-time-file", help="For --set-mode 8: Path to a JSON file containing the time schedule list")
    p.add_argument("--set-max-discharging-power", type=int, metavar="PCT",
                   help="Set Max. Discharging Power %% (20-100, NOT watts). Requires --dev-sn and --dtu-sn.")
    p.add_argument("--get-max-discharging-power", action="store_true",
                   help="Read current Max. Discharging Power %% from device. Requires --dev-sn and --dtu-sn.")
    p.add_argument("--get-dev-config", action="store_true",
                   help="Read and print the full live ESS Advanced Config from device. Requires --dev-sn and --dtu-sn.")
    p.add_argument("--debug-config", action="store_true",
                   help="Dump the raw fetch_status response to diagnose config structure issues.")
    p.add_argument("--dry-run", action="store_true",
                   help="For --set-max-discharging-power: fetch live config, build the patched payload, "
                        "print it, but DO NOT call /config/put. Use this to verify nothing unexpected changes.")
    p.add_argument("--dev-sn", default=None, help="Device serial number (or set HOYMILES_DEV_SN in .env)")
    p.add_argument("--dtu-sn", default=None, help="DTU serial number (or set HOYMILES_DTU_SN in .env)")
    p.add_argument("--rid", type=int, default=54, help="Request ID for config/put calls (default 54)")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="Seconds between fetch_status polls when reading device config (default 2)")
    p.add_argument("--poll-timeout", type=float, default=60.0,
                   help="Max seconds to wait for device config response (default 60)")
    p.add_argument("--all", action="store_true", help="Return stations + realtime + all micro details")
    p.add_argument("--env-file", default=None, metavar="PATH",
                   help="Path to a .env file to load (default: .env in current directory)")

    args = p.parse_args()

    # Load .env file: explicit --env-file takes priority over default .env
    try:
        from dotenv import load_dotenv
        env_path = args.env_file or ".env"
        load_dotenv(dotenv_path=env_path, override=False)
    except ImportError:
        if args.env_file:
            p.error("--env-file requires python-dotenv: pip install python-dotenv")

    # Resolve credentials and device identifiers: CLI > .env / environment variables
    args.username   = args.username   or os.environ.get("HOYMILES_USERNAME")
    args.password   = args.password   or os.environ.get("HOYMILES_PASSWORD")
    args.station_id = args.station_id or os.environ.get("HOYMILES_STATION_ID")
    args.dev_sn     = args.dev_sn     or os.environ.get("HOYMILES_DEV_SN")
    args.dtu_sn     = args.dtu_sn     or os.environ.get("HOYMILES_DTU_SN")

    if not args.username:
        p.error("Username is required. Pass -u / --username or set HOYMILES_USERNAME in .env")
    if not args.password:
        p.error("Password is required. Pass -p / --password or set HOYMILES_PASSWORD in .env")
    client = HoymilesClient(
        username=args.username,
        password=args.password,
        timeout=args.timeout,
        verify_tls=not args.insecure,
    )

    client.authenticate()

    out: Dict[str, Any] = {}

    if args.list_stations:
        out["stations"] = client.list_stations()

    # If any station-dependent options are requested, resolve station id
    station_needed = args.realtime or args.micro_list or args.micro_detail or args.micro_details or args.battery or args.battery_from_realtime or (args.set_mode is not None) or args.all
    sid = None
    if station_needed:
        sid = _pick_station_id(client, args.station_id)
        out["station_id"] = sid


    if args.set_mode is not None:
        tou_time = None
        if args.tou_time_json:
            tou_time = json.loads(args.tou_time_json)
        elif args.tou_time_file:
            with open(args.tou_time_file, "r", encoding="utf-8") as f:
                tou_time = json.load(f)
        out["set_mode_result"] = client.set_battery_mode(sid, args.set_mode, reserve_soc=args.reserve_soc, tou_time=tou_time)

    if args.set_max_discharging_power is not None:
        if not args.dev_sn or not args.dtu_sn:
            raise HoymilesClientError("--set-max-discharging-power requires --dev-sn and --dtu-sn")
        out["set_max_discharging_power_result"] = client.set_max_discharging_power(
            dev_sn=args.dev_sn,
            dtu_sn=args.dtu_sn,
            max_discharging_power=args.set_max_discharging_power,
            rid=args.rid,
            poll_interval=args.poll_interval,
            poll_timeout=args.poll_timeout,
            dry_run=args.dry_run,
        )

    if args.get_max_discharging_power or args.get_dev_config or args.debug_config:
        if not args.dev_sn or not args.dtu_sn:
            raise HoymilesClientError("--get-max-discharging-power / --get-dev-config require --dev-sn and --dtu-sn")

        if args.debug_config:
            # Trigger fetch and dump the raw fetch_status response without any parsing
            fetch_resp = client._post(
                API_DEV_CONFIG_FETCH_URL,
                {"cfg_type": 0, "dev_sn": args.dev_sn, "dtu_sn": args.dtu_sn},
            )
            job_id = str(fetch_resp.get("data") or "")
            if not job_id:
                raise HoymilesClientError(f"config/fetch did not return a job id: {fetch_resp}")
            import time as _time
            deadline = _time.monotonic() + args.poll_timeout
            while True:
                status_resp = client._post_raw(API_DEV_CONFIG_FETCH_STATUS_URL, {"id": job_id})
                d = status_resp.get("data") or {}
                if d.get("code") == 0 and d.get("rate") == 100:
                    out["debug_raw_fetch_status"] = status_resp
                    break
                if _time.monotonic() > deadline:
                    out["debug_raw_fetch_status_timeout"] = status_resp
                    break
                _time.sleep(args.poll_interval)
        else:
            # get_dev_config returns a flat dict keyed by item id
            item_index = client.get_dev_config(
                dev_sn=args.dev_sn,
                dtu_sn=args.dtu_sn,
                poll_interval=args.poll_interval,
                poll_timeout=args.poll_timeout,
            )
            if args.get_dev_config:
                # Convert back to sorted list for human-readable output
                out["dev_config"] = sorted(item_index.values(), key=lambda x: x.get("id", 0))
            if args.get_max_discharging_power:
                item = item_index.get(15828)
                out["max_discharging_power_pct"] = item.get("content") if item else None

    if args.realtime or args.all:
        out["realtime"] = client.realtime(sid)

    if args.micro_list or args.all:
        out["micro_list"] = client.micro_list(sid)

    if args.micro_detail:
        if not args.micro_id:
            raise HoymilesClientError("--micro-detail requires --micro-id")
        out["micro_detail"] = client.micro_details(sid, args.micro_id)

    if args.micro_details or args.all:
        out["micro_details"] = client.all_micro_details(sid)
    if args.battery or args.all:
        out["battery"] = client.battery_settings(sid)

    if args.battery_from_realtime or args.all:
        out["battery_realtime"] = client.battery_from_realtime(sid)


    # Default behavior if no flags: return basic set (stations + realtime + all micro details for first station)
    if not any([args.list_stations, args.realtime, args.micro_list, args.micro_detail,
                args.micro_details, args.all, args.set_mode is not None,
                args.set_max_discharging_power is not None, args.get_max_discharging_power,
                args.get_dev_config, args.debug_config]):
        sid = _pick_station_id(client, args.station_id)
        out = {
            "stations": client.list_stations(),
            "station_id": sid,
            "realtime": client.realtime(sid),
            "micro_details": client.all_micro_details(sid),
            "battery": client.battery_settings(sid),
            "battery_realtime": client.battery_from_realtime(sid),
        }

    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HoymilesClientError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        raise SystemExit(2)