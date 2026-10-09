#!/bin/sh
# Night City Haptics: starts the companion and opens its control page.
# Needs python3 (preinstalled on SteamOS and most Linux systems). Nothing else.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/nch.py" "$@"
