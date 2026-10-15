#!/bin/sh
set -eu
umask 077
if [ "$(apk --print-arch)" != aarch64 ] || [ "$(uname -m)" != aarch64 ]; then
  echo "Miho requires an ARM64 image and ARM64 runtime." >&2
  exit 1
fi
mkdir -p "$DATA_DIR"
chown miho:miho "$DATA_DIR"
exec su-exec miho:miho /sbin/tini -g -- "$@"

