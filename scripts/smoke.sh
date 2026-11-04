#!/bin/sh
set -eu
if [ "$#" -ne 1 ]; then
    echo 'Usage: scripts/smoke.sh IMAGE' >&2
    exit 2
fi
exec python3 "$(dirname "$0")/runtime-smoke.py" "$1"
