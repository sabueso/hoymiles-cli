#!/bin/bash
basedir="$(cd "$(dirname "$0")" && pwd)"

${basedir}/../hoymiles_cli.py --env-file "$basedir/../.env" --set-mode 1 --reserve-soc 25
