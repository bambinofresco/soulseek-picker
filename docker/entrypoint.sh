#!/bin/sh
# Drop to PUID:PGID (Unraid default 99:100, nobody:users) so files land with the right owner.
set -e

umask "${UMASK:-002}"
mkdir -p "${CONFIG_DIR:-/config}"

if [ "$(id -u)" = "0" ]; then
    chown "${PUID}:${PGID}" "${CONFIG_DIR:-/config}"
    exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups env HOME="${CONFIG_DIR:-/config}" "$@"
fi

exec "$@"
