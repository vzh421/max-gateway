#!/bin/sh
set -eu
# Перед запуском сервиса — миграции. Остальные команды (login, kill, …) — как есть.
if [ "${1:-run}" = "run" ]; then
    python -m gateway db upgrade
fi
exec python -m gateway "$@"
