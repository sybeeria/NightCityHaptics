#!/bin/sh
# Night City Haptics native companion. No Python required.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
unset LD_LIBRARY_PATH LD_PRELOAD
exec "$here/night-city-haptics" "$@"
