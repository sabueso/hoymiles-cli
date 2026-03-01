# hoymiles-cli

CLI to control Hoymiles inverters with ESS battery through the [neapi.hoymiles.com](https://neapi.hoymiles.com) API, based on the `hoymiles_cloud` Home Assistant custom component logic.

---

> ⚠️ **Disclaimer**
>
> This project was developed and tested on a specific hardware setup:
> - **Inverter:** Hoymiles HAS-5.0LV-EUG1
> - **Battery:** Felicity ESS LUX-X-48100LCG01
>
> It may work on other Hoymiles ESS devices or battery models, but this has not been verified.
> **Use with caution.** Sending incorrect configuration values to the inverter
> can affect battery behaviour, grid interaction, and UPS/EPS mode.
> Always use `--dry-run` before committing any change, and verify the result
> against the Hoymiles web UI afterwards.

---

## Requirements

```bash
pip install requests python-dotenv
```

## Credentials setup

Copy `examples/env.example` to `.env` and fill in your values:

```bash
cp examples/env.example .env
```

```ini
HOYMILES_USERNAME=you@example.com
HOYMILES_PASSWORD=secret
HOYMILES_DEV_SN=208324250511
HOYMILES_DTU_SN=430123526317
HOYMILES_STATION_ID=9258040
```

> **Note:** `.env` is listed in `.gitignore` and will never be committed to the repository. `examples/env.example` is committed as a template — copy it to the repo root before use.

CLI arguments always take priority over `.env` values.

---

## Usage

```
./hoymiles_cli.py [options]
```

Once `.env` is configured, most commands don't need `-u` or `-p`.

### Reading data

```bash
# Station real-time data (PV / grid / load / battery)
./hoymiles_cli.py --realtime --pretty

# List stations in the account
./hoymiles_cli.py --list-stations --pretty

# Battery SOC and power (from realtime endpoint, most compatible)
./hoymiles_cli.py --battery-from-realtime --pretty

# Battery operating mode and reserve SOC (may require ESS permissions)
./hoymiles_cli.py --battery --pretty

# Everything at once: station + realtime + microinverters
./hoymiles_cli.py --all --pretty
```

---

## Battery operating mode

Changes the ESS operating mode. Available modes:

| Value | Name             |
|-------|------------------|
| `1`   | Self-Consumption |
| `2`   | Economy          |
| `3`   | Backup           |
| `4`   | Off-Grid         |
| `7`   | Peak Shaving     |
| `8`   | Time of Use      |

```bash
# Self-Consumption with 25% reserve SOC
./hoymiles_cli.py --set-mode 1 --reserve-soc 25

# Time of Use with inline schedule
./hoymiles_cli.py --set-mode 8 --reserve-soc 25 \
  --tou-time-json '[{"cs_time":"01:00","ce_time":"06:30","c_power":20,"dcs_time":"08:00","dce_time":"23:59","dc_power":100,"charge_soc":60,"dis_charge_soc":25}]'

# Time of Use with schedule from file
./hoymiles_cli.py --set-mode 8 --reserve-soc 25 \
  --tou-time-file schedule.json
```

---

## Max. Discharging Power

Controls the maximum battery discharge power percentage **(20–100%, not watts)**.

The script follows exactly the same flow as the Hoymiles web UI (verified via HAR capture):

1. `POST /config/fetch` — requests the current config from the device
2. Poll `POST /config/fetch_status` — waits until all live values are received (`code=0, rate=100`)
3. Patches **only** `id=15828` with the new value — no other field is touched
4. `POST /config/put` — sends the config back to the device
5. Poll `POST /config/put_status` — waits for device confirmation

### Read the current value

```bash
# Just the value
./hoymiles_cli.py --get-max-discharging-power --pretty

# Full device config (~253 parameters)
./hoymiles_cli.py --get-dev-config --pretty

# Find the specific field
./hoymiles_cli.py --get-dev-config --pretty | grep -A6 '"id": 15828'
```

### Change the value (recommended flow)

```bash
# 1. Run a dry-run first to verify only id=15828 changes
./hoymiles_cli.py --set-max-discharging-power 50 --dry-run --pretty

# 2. Check the output:
#    "old_value_pct": 35,
#    "new_value_pct": 50,
#    "items_with_non_null_change": [...]   <- only enum fields from the device

# 3. Run without --dry-run
./hoymiles_cli.py --set-max-discharging-power 50 --pretty
```

Expected output after a successful change:
```json
{
  "set_max_discharging_power_result": {
    "success": true,
    "old_value_pct": 35,
    "new_value_pct": 50,
    "put_job_id": "37395541",
    "device_confirm": { "code": 0, "rate": 100 }
  }
}
```

---

## Example bash scripts

### `change_to_self.sh` — switch to Self-Consumption

```bash
#!/bin/bash
basedir="$(cd "$(dirname "$0")" && pwd)"
${basedir}/../hoymiles_cli.py --env-file "$basedir/../.env" --set-mode 1 --reserve-soc 25
```

### `change_to_time.sh` — switch to Time of Use

```bash
#!/bin/bash
basedir="$(cd "$(dirname "$0")" && pwd)"
${basedir}/../hoymiles_cli.py --env-file "$basedir/../.env" --set-mode 8 --reserve-soc 25 \
  --tou-time-json '[{"cs_time":"01:00","ce_time":"06:30","c_power":20,"dcs_time":"08:00","dce_time":"23:59","dc_power":100,"charge_soc":60,"dis_charge_soc":25}]'
```

### `change_bat_power_output.sh` — set Max. Discharging Power

```bash
#!/bin/bash
basedir="$(cd "$(dirname "$0")" && pwd)"
${basedir}/../hoymiles_cli.py --env-file "$basedir/../.env" --set-max-discharging-power 20
```

### `show_info.sh` — display current status

```bash
#!/bin/bash
basedir="$(cd "$(dirname "$0")" && pwd)"
${basedir}/../hoymiles_cli.py --env-file "$basedir/../.env" --pretty
```

---

## Options reference

| Option | Description |
|--------|-------------|
| `-u`, `--username` | Hoymiles account email (or `HOYMILES_USERNAME` in `.env`) |
| `-p`, `--password` | Plain-text password, MD5-hashed before sending (or `HOYMILES_PASSWORD`) |
| `--station-id` | Station ID (or `HOYMILES_STATION_ID`). Falls back to the first station found |
| `--dev-sn` | ESS device serial number (or `HOYMILES_DEV_SN`) |
| `--dtu-sn` | DTU serial number (or `HOYMILES_DTU_SN`) |
| `--env-file PATH` | Explicit path to a `.env` file (useful when invoking from an external bash script) |
| `--set-mode N` | Set ESS operating mode (1/2/3/4/7/8) |
| `--reserve-soc N` | Reserve SOC percentage (default 25, used with `--set-mode`) |
| `--tou-time-json` | Time of Use schedule as inline JSON (for `--set-mode 8`) |
| `--tou-time-file` | Time of Use schedule from a JSON file (for `--set-mode 8`) |
| `--set-max-discharging-power PCT` | Set Max. Discharging Power to the given percentage (20-100) |
| `--get-max-discharging-power` | Read the current Max. Discharging Power value |
| `--get-dev-config` | Read the full live ESS config from the device (~253 parameters) |
| `--dry-run` | Simulate `--set-max-discharging-power` without writing anything to the device |
| `--realtime` | Get station real-time data |
| `--battery` | Get battery mode and reserve SOC via pvm-ctl (may require permissions) |
| `--battery-from-realtime` | Get battery SOC and power from realtime endpoint (most compatible) |
| `--list-stations` | List all stations in the account |
| `--all` | Stations + realtime + all microinverters |
| `--pretty` | Pretty-print JSON output |
| `--poll-interval N` | Seconds between fetch/put_status polls (default 2) |
| `--poll-timeout N` | Maximum timeout per polling phase in seconds (default 60) |
| `--timeout N` | HTTP timeout in seconds (default 25) |
| `--insecure` | Disable TLS verification (not recommended) |

---

## Finding your `dev_sn` and `dtu_sn`

Open the Hoymiles web dashboard, navigate to the ESS settings page, open the browser developer tools (F12 -> Network) and look for the request to `/pvm-ctl/api/0/dev/config/fetch`. The request body will contain:

```json
{"cfg_type": 0, "dev_sn": "208324250511", "dtu_sn": "430123526317"}
```

---

## Notes

- The API authenticates using an MD5 hash of the password (no `Bearer` prefix).
- The session token is obtained automatically on every run.
- ESS configuration changes may take several seconds to propagate to the device.
- Output is always JSON on stdout; errors go to stderr.
