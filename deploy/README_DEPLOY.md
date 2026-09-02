# 🚀 Деплой VpnShop + Amnezia Web Panel на VPS

Данный гайд — пошаговый план (Вариант А). Вы выполняете команды сами в своей
SSH-сессии, вставляя блоки по очереди. Всё готово: скрипт `install.sh`,
docker-compose, systemd-юнит, nginx-конфиг.

---

## 0. Подготовка

Везде ниже `YOUR_VPS_IP` замените на IP вашего сервера, а `vpn.example.com` — на ваш домен.

> ⚠️ **Безопасность:** смените root-пароль и настройте вход по SSH-ключам:
> ```bash
> passwd                       # сменить пароль root
> ssh-copy-id root@YOUR_VPS_IP # затем вход без пароля
> ```

---

## 1. Конфигурация на VPS (один раз)

Зайдите на сервер и подготовьте папку:

```bash
ssh root@YOUR_VPS_IP
apt-get update -y && apt-get install -y git
mkdir -p /opt
```

Создайте **DNS A-запись** вашего домена на IP сервера `YOUR_VPS_IP`.
Без неё HTTPS (Let's Encrypt) не выпустится.

---

## 2. Получите репозиторий

Склонируйте проект с GitHub прямо на сервер:

```bash
cd /opt
git clone https://github.com/xxldrago/vpn-shop.git
cd vpn-shop
```
ls -la /opt/vpn-shop/
```

---

## 3. Запустите установщик

```bash
cd /opt/vpn-shop
sudo bash deploy/install.sh vpn.example.com
```
(замените `vpn.example.com` на свой домен).

Скрипт выполнит всё автоматически:
- обновит систему, поставит **docker**, **nginx**, **certbot**, **python3-venv**
- установит **Amnezia Web Panel** (systemd-сервис на `127.0.0.1:5000`)
- соберёт и запустит **VpnShop** (веб + бот) в Docker (SQLite в volume)
- настроит **nginx + HTTPS** для магазина и вебхука Platega

---

## 4. Настройте секреты

Скрипт создал файл конфигурации. Отредактируйте его:

```bash
nano /opt/vpn-shop/deploy/.env
```

Заполните как минимум:
| Параметр | Что вписать |
|---|---|
| `DOMAIN` -> `SHOP_PUBLIC_URL` | `https://ваш-домен.ru` |
| `PANEL_TOKEN` | Токен из панели (см. шаг 6) |
| `PLATEGA_MERCHANT_ID` | ID мерчанта Platega |
| `PLATEGA_SECRET` | API-ключ Platega |
| `SHOP_SECRET_KEY` | Длинная случайная строка |
| `TELEGRAM_BOT_TOKEN` | Токен бота (или оставьте пусто, впишете в админке) |

После редактирования перезапустите контейнеры:

```bash
cd /opt/vpn-shop
sudo docker compose -f deploy/docker-compose.yml up -d --build
```

---

## 5. Проверьте, что всё работает

```bash
# Статус контейнеров
sudo docker compose -f /opt/vpn-shop/deploy/docker-compose.yml ps

# Логи магазина
sudo docker compose -f /opt/vpn-shop/deploy/docker-compose.yml logs -f shop

# Магазин доступен?
curl -s https://ваш-домен.ru/ | head -20
# Панель?
curl -s http://127.0.0.1:5000/ | head
```

---

## 6. Настройте Amnezia Web Panel

1. Откройте **http://YOUR_VPS_IP:5000** (или через ваш домен, если пробросили).
2. Войдите: `admin` / `admin` → **сразу смените пароль** (Users).
3. **Settings → API Tokens** → создайте токен (префикс `awp_...`).
   — Показывается **один раз**, скопируйте его в `deploy/.env` → `PANEL_TOKEN`.
4. **Servers** → добавьте VPN-сервер (host, root, пароль/ключ).
   Панель подключится по SSH и определит протоколы.

> Откройте порт 5000 в фаерволе/дашборде VPS для `YOUR_VPS_IP`, либо
> пробросьте панель через nginx на отдельный поддомен (см. ниже).

---

## 7. Настройте магазин

1. Откройте **https://ваш-домен.ru/admin** → войдите `admin/admin` → смените пароль.
2. **Настройки** — пропишите (если не в `.env`):
   - URL панели: `http://127.0.0.1:5000`
   - токен панели
   - публичный URL магазина
   - ключи Platega
   - токен Telegram-бота (или оставьте — возьмётся из `.env`)
3. Создайте **тарифы** и **промокоды**.

---

## 8. Telegram-бот

Если токен указан в `.env` — бот уже крутится (контейнер `bot`).
Проверка:
```bash
sudo docker compose -f /opt/vpn-shop/deploy/docker-compose.yml logs bot
```
При ошибке `Unauthorized` — токен неверный. Бот включается/выключается перезапуском контейнера.

---

## 9. Platega

В личном кабинете Platega укажите **Webhook / Callback URL**:
```
https://ваш-домен.ru/webhook/callback
```
Проверить не пробил ли вебхук: `sudo docker compose … logs shop` (ищите `webhook`).

---

## Управление сервисами

```bash
# Магазин/бот
sudo docker compose -f /opt/vpn-shop/deploy/docker-compose.yml ps|logs shop|logs bot|restart

# Панель
sudo systemctl status amnezia-panel
sudo systemctl restart amnezia-panel

# nginx
sudo nginx -t && sudo systemctl reload nginx

# HTTPS обновление (авто, но можно вручную)
sudo certbot renew
```

---

## (Опционально) Проброс панели через домен

Чтобы не ходить на панель по `IP:5000`, добавьте nginx-конфиг:

```nginx
# /etc/nginx/sites-available/panel
server {
    server_name panel.ваш-домен.ru;
    location / { proxy_pass http://127.0.0.1:5000; }
}
```
```bash
ln -s /etc/nginx/sites-available/panel /etc/nginx/sites-enabled/
sudo certbot --nginx -d panel.ваш-домен.ru
```

---

## Обновление с GitHub (синхронизация кода)

Когда вы пушите изменения в репозиторий с локальной машины, на сервере
достаточно подтянуть их и пересобрать контейнеры:

```bash
cd /opt/vpn-shop
sudo git pull
sudo docker compose -f deploy/docker-compose.yml up -d --build
```

> Обратите внимание: `deploy/.env` с секретами **не попадает** в git
> (он в `.gitignore`), поэтому при обновлениях файл не затирается.
> Данные SQLite лежат в docker-volume и тоже сохраняются между сборками.

---

## Порядок на будущее (тестирование)

После разворачивания — протестируем по очереди: покупка, промокоды, тестовая
подписка, рефералка, баланс, тикеты, QR/конфиги, Telegram-бот, уведомления.
