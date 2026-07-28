#!/usr/bin/env bash
# Первичная настройка pogodka в LXC-контейнере на Proxmox (запуск на pve).
# Предполагается свежий Debian 12 CT. Запуск: ./setup.sh [CTID]  (по умолчанию 131)
set -euo pipefail

CT="${1:-131}"
BASE=/opt/pogodka

cd "$(dirname "$0")"

echo "== пакеты =="
pct exec "$CT" -- apt-get update
pct exec "$CT" -- apt-get install -y --no-install-recommends \
    python3-venv fonts-dejavu-core

echo "== файлы приложения =="
pct exec "$CT" -- mkdir -p "$BASE/app"
for f in app/*.py; do
    pct push "$CT" "$f" "$BASE/${f}"
    echo "  $f"
done
pct push "$CT" requirements.txt "$BASE/requirements.txt"

echo "== venv + зависимости =="
pct exec "$CT" -- python3 -m venv "$BASE/venv"
pct exec "$CT" -- "$BASE/venv/bin/pip" install --no-cache-dir -r "$BASE/requirements.txt"

echo "== systemd =="
pct push "$CT" pogodka.service /etc/systemd/system/pogodka.service
pct exec "$CT" -- systemctl daemon-reload
pct exec "$CT" -- systemctl enable --now pogodka
sleep 2
pct exec "$CT" -- systemctl is-active pogodka

echo "OK. Кадр: http://<ip-контейнера>:8000/api/frame.png"
