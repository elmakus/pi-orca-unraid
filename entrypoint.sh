#!/bin/sh
set -eu
exec /usr/local/bin/orca-ide serve --port "${ORCA_PORT:-6769}" --pairing-address "${ORCA_PAIRING_ADDRESS:?Set ORCA_PAIRING_ADDRESS to the LAN address}" "$@"
