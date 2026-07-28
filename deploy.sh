#!/usr/bin/env bash
# Обновление кода pogodka в LXC-контейнере (запуск на pve).
# Запуск: ./deploy.sh [CTID]   (по умолчанию 131)
set -euo pipefail

CT="${1:-131}"
BASE=/opt/pogodka

cd "$(dirname "$0")"

if [ ! -d .git ]; then
    echo "ВНИМАНИЕ: это не git-клон — деплой продолжится, но версии не под контролем" >&2
fi

echo "== push кода в CT $CT =="
for f in app/*.py; do
    pct push "$CT" "$f" "$BASE/$f"
    echo "  $f"
done

echo "== restart =="
pct exec "$CT" -- systemctl restart pogodka
sleep 2
pct exec "$CT" -- systemctl is-active pogodka

code=$(pct exec "$CT" -- python3 -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8000/healthz').status)" 2>/dev/null || echo FAIL)
echo "  API отвечает: $code"
echo "OK."
