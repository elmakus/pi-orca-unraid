#!/bin/sh
set -eu
if [ "$#" -ne 1 ] && { [ "$#" -ne 3 ] || [ "$2" != --diagnostic-file ]; }; then
    echo 'Usage: scripts/smoke.sh IMAGE [--diagnostic-file FILE]' >&2
    exit 2
fi
exec python3 "$(dirname "$0")/runtime-smoke.py" "$1"
