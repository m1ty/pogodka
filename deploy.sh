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

echo "== зависимости =="
# requirements.txt тоже едет в контейнер: иначе новая/обновлённая библиотека
# не встанет в venv и сервис упадёт на импорте после рестарта
local_sum=$(md5sum requirements.txt | cut -d' ' -f1)
remote_sum=$(pct exec "$CT" -- md5sum "$BASE/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)
if [ "$local_sum" != "$remote_sum" ]; then
    pct push "$CT" requirements.txt "$BASE/requirements.txt"
    pct exec "$CT" -- "$BASE/venv/bin/pip" install -q -r "$BASE/requirements.txt"
    echo "  список обновлён, зависимости доустановлены"
else
    echo "  без изменений"
fi

echo "== restart =="
pct exec "$CT" -- systemctl restart pogodka

# сервис поднимается за пару секунд — ждём ответа до ~15 с, а не одну попытку
code=FAIL
for _ in $(seq 1 15); do
    sleep 1
    code=$(pct exec "$CT" -- python3 -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status)" 2>/dev/null) \
        && break || code=FAIL
done
pct exec "$CT" -- systemctl is-active pogodka
echo "  API отвечает: $code"
[ "$code" = "200" ] || { echo "ОШИБКА: healthz не ответил" >&2; exit 1; }
echo "OK."
