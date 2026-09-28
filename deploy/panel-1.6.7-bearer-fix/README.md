# Amnezia Web Panel 1.6.7 — Bearer fix for `POST /api/users/add`

## Проблема
`api_add_user()` в панели 1.6.7 авторизует только через session-cookie
(`get_current_user`), поэтому shop с `Authorization: Bearer <token>`
получал `403 Forbidden` на создание пользователей. Ломалось всё создание
VPN-пользователей через API: триалы, покупки, регистрации в боте.

## Что сделано на сервере (2026-09-28)
1. Создан API-токен панели (`awp_...`, имя `vpn-shop`) — записан в
   `shop_settings.panel_token`. Панель хранит только хэш; сам токен
   показать больше нельзя, при утере — выпустить новый.
2. В `/opt/Amnezia-Web-Panel/docker-compose.yml` образ заменён на сборку
   `amnezia-panel:shop-1.6.7` из `Dockerfile.shop-patched` (+ этот патч).
3. Отключён systemd-юнит `amnezia-panel.service` (конфликтовал по порту
   5000 с docker-контейнером). Возврат: `systemctl enable --now amnezia-panel`.

## После каждого обновления панели
Патч нужно накатить заново (файлы лежат рядом):
```bash
cd /opt/Amnezia-Web-Panel
cp /путь/к/этому/каталогу/* .
docker compose build && docker compose up -d
```
Сборка упадёт с assert, если upstream изменил `api_add_user`, —
тогда обновить якорь в `patch_api_add_bearer.py` вручную.

## Проверка
Дубликат username обязан вернуть 400 (а не 403):
```bash
TOK=$(docker exec vpnshop-shop-1 python -c "import database; print(database.get_setting('panel_token'))")
curl -X POST -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"x","role":"user"}' \
  http://localhost:5000/api/users/add
# → {"error":"...уже существует"} + HTTP 400
```
