#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec python3 scripts/manage.py restore "${1:?Usage: restore.sh /path/to/backup-directory}"
