#!/bin/bash
basedir="$(cd "$(dirname "$0")" && pwd)"

${basedir}/../hoymiles_cli.py --env-file "$basedir/../.env" --set-mode 8 --reserve-soc 25 --tou-time-json '[{"cs_time":"01:00","ce_time":"06:30","c_power":20,"dcs_time":"08:00","dce_time":"23:59","dc_power":100,"charge_soc":60,"dis_charge_soc":25}]'
