# 🛡️ Amnezia Panel

**Открытый исходный код** · **Docker-ready** · **FastAPI + Vanilla JS**

Веб-панель для тестового управления AmneziaWG/WireGuard-подобным VPN-контуром: локальный `awg-tunnel`, peer CRUD, QR/конфиги клиентов, traffic accounting, уведомления, WebSocket live-события и multi-server управление через SSH.

![Version](https://img.shields.io/badge/version-1.1.6-blue)
![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![Docker](https://img.shields.io/badge/docker-ready-blue)

> Статус: тестовый проект. Не выставляйте панель в интернет без HTTPS, сильного пароля, firewall/reverse proxy и постоянных секретов.

---

## Возможности

### VPN / peers

- Создание и удаление peer'ов.
- Генерация private/public key и preshared key.
- QR-код и `.conf` для импорта в AmneziaVPN/WireGuard-клиент.
- Включение/отключение peer'ов.
- Учёт трафика peer'ов и логирование handshake-событий.

### Multi-server

- Управление несколькими серверами из одной панели.
- SSH-подключение через password, key path или PEM key data.
- Проверка удалённых Docker/AmneziaWG контейнеров.
- `start` / `stop` / `restart` удалённого контейнера.
- Сводная статистика по серверам.

### Уведомления и realtime

- Telegram bot, webhook и email SMTP каналы.
- События: peer added/deleted, handshake, server up/down, traffic threshold.
- WebSocket `/ws` для live-обновлений UI.
- Audit log действий администратора.

### UI

- Single Page Application на чистом HTML/CSS/JS.
- Вкладки: клиенты, трафик, подключения, серверы, поиск, уведомления.
- Тёмная тема.
- No-cache static serving для быстрой разработки.

---

## Архитектура

```text
Browser SPA
  │ REST + WebSocket
  ▼
awg-panel container
  ├─ FastAPI / Uvicorn
  ├─ SQLite /data/panel.db
  ├─ Fernet encryption for stored SSH secrets
  ├─ Docker CLI for local awg-tunnel operations
  └─ Paramiko SSH for remote servers
       │
       ├─ local docker exec → awg-tunnel
       └─ remote SSH → docker exec on external AWG hosts

awg-tunnel container
  ├─ Alpine 3.20
  ├─ amneziawg-go userspace tunnel
  ├─ amneziawg-tools / awg
  └─ host network, UDP 51820 by default
```

---

## Структура репозитория

```text
.
├── Dockerfile
├── backend/
│   ├── main.py              # FastAPI app, REST API, JWT auth, static UI
│   ├── multi_server.py      # SSH/remote Docker operations
│   ├── crypto.py            # Fernet encryption for stored credentials
│   ├── aggregate.py         # aggregate traffic/status collection
│   ├── discover.py          # subnet discovery helper
│   ├── notify.py            # Telegram/Webhook/Email notifications
│   ├── realtime.py          # WebSocket helpers
│   ├── requirements.txt
│   └── static/index.html    # synced copy of frontend/index.html
├── frontend/
│   └── index.html           # full SPA UI
└── deploy/
    ├── docker-compose.yml
    ├── .env.example
    ├── init-tunnel.sh       # optional local awg-tunnel bootstrap
    └── Caddyfile
```

---

## Быстрый старт

```bash
git clone https://github.com/sllikmll/amnezia-panel.git
cd amnezia-panel/deploy
cp .env.example .env
nano .env
```

Минимально задайте:

```env
PANEL_ADMIN_USER=admin
PANEL_ADMIN_PASSWORD=change-this-password
PANEL_SECRET_KEY=change-this-long-random-jwt-secret
PANEL_PORT=8888
AWG_ENDPOINT=your-public-host:51820
```

Запуск:

```bash
docker compose up -d
```

Открыть:

```text
http://<server-ip>:8888/
```

---

## Docker image

Готовый образ публикуется в GHCR:

```text
ghcr.io/sllikmll/amnezia-panel:latest
```

Сборка вручную:

```bash
docker build -t ghcr.io/sllikmll/amnezia-panel:latest .
```

---

## Конфигурация

| Переменная | Назначение | Пример |
|---|---|---|
| `PANEL_ADMIN_USER` | логин первого администратора | `admin` |
| `PANEL_ADMIN_PASSWORD` | пароль первого администратора | `change-this-password` |
| `PANEL_SECRET_KEY` | JWT signing secret, должен быть постоянным | `openssl rand -hex 32` |
| `PANEL_ENCRYPTION_KEY` | Fernet key для SSH credentials; если пусто — создаётся `/data/encryption.key` | `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` |
| `PANEL_PORT` | порт web-панели | `8888` |
| `AWG_DATA_DIR` | каталог данных на хосте/volume | `/data/awg` |
| `AWG_DB_PATH` | SQLite DB path внутри контейнера | `/data/panel.db` |
| `AWG_CONFIG_PATH` | локальный AWG config path | `/data/awg0.conf` |
| `AWG_ENDPOINT` | endpoint в клиентских конфигах | `vpn.example.com:51820` |
| `AWG_LISTEN_PORT` | UDP listen port | `51820` |

---

## Самообновление

Панель умеет проверять GitHub Releases при входе администратора:

- backend endpoint `GET /api/update/check` сравнивает `PANEL_VERSION` с latest release в `PANEL_REPO`;
- если latest release новее текущей версии, UI показывает баннер обновления;
- кнопка **Обновить** вызывает `POST /api/update/apply`;
- backend запускает фиксированный update-script `PANEL_UPDATE_COMMAND` без передачи произвольных команд из браузера;
- стандартный Docker script `/usr/local/bin/update-panel` делает `docker pull`, пересоздаёт контейнер `PANEL_CONTAINER_NAME` и стартует новый image.

Переменные:

| Переменная | Default | Назначение |
|---|---|---|
| `PANEL_VERSION` | `1.1.6` | текущая версия приложения |
| `PANEL_REPO` | `sllikmll/amnezia-panel` | GitHub repo для latest release check |
| `PANEL_IMAGE` | `ghcr.io/sllikmll/amnezia-panel:latest` | Docker image, который тянет updater |
| `PANEL_CONTAINER_NAME` | `awg-panel` | имя контейнера панели |
| `PANEL_UPDATE_COMMAND` | `/usr/local/bin/update-panel` | фиксированная команда самообновления |

Для работы updater нужен mount Docker socket:

```yaml
- /var/run/docker.sock:/var/run/docker.sock
```


## API

Проверенные endpoints текущего кода:

| Метод | URL | Описание |
|---|---|---|
| `POST` | `/api/login` | логин, возвращает JWT |
| `GET` | `/api/peers` | список peer'ов без private key/PSK |
| `POST` | `/api/peers` | создать peer и вернуть client config/QR |
| `DELETE` | `/api/peers/{peer_id}` | удалить peer |
| `PATCH` | `/api/peers/{peer_id}/toggle` | включить/выключить peer |
| `GET` | `/api/traffic/summary` | сводка трафика |
| `GET` | `/api/traffic/peer/{peer_id}` | трафик по peer |
| `GET` | `/api/connections` | лог подключений |
| `POST` | `/api/collect` | ручной сбор трафика |
| `GET` | `/api/servers` | список серверов без SSH secrets |
| `POST` | `/api/servers` | добавить сервер |
| `GET` | `/api/servers/{server_id}` | получить сервер |
| `PUT` | `/api/servers/{server_id}` | обновить сервер |
| `DELETE` | `/api/servers/{server_id}` | удалить сервер из панели |
| `POST` | `/api/servers/{server_id}/test` | проверить SSH/Docker/AWG |
| `POST` | `/api/servers/{server_id}/action` | `start` / `stop` / `restart` |
| `GET` | `/api/servers/{server_id}/peers` | runtime peer'ы на сервере |
| `GET` | `/api/aggregate/stats` | агрегированная статистика |
| `GET` | `/api/aggregate/servers` | сводка по серверам |
| `GET` | `/api/discover/scan?subnet=CIDR` | scan helper |
| `GET` | `/api/audit` | audit log |
| `GET/POST/PATCH/DELETE` | `/api/notifications/channels` | notification channels |
| `GET` | `/api/notifications/log` | notification delivery log |
| `WS` | `/ws` | realtime events |

Пример smoke:

```bash
TOKEN=$(curl -sS -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"YOUR_PASSWORD"}' \
  http://127.0.0.1:8888/api/login | jq -r .token)

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8888/api/servers
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8888/api/peers
```

---

## Безопасность

Что реализовано:

- Bcrypt-хэширование admin password.
- JWT HS256 с TTL 24 часа.
- Fernet-шифрование SSH password/key data в SQLite.
- `/api/servers` отдаёт только `has_password` / `has_key`, не сами credentials.
- `/api/peers` не отдаёт private key и PSK в списке.
- Compose не содержит тестовый пароль или захардкоженный JWT secret.

Что ещё нужно для production:

- HTTPS через Nginx Proxy Manager / Caddy / Traefik.
- Firewall allowlist для панели.
- Rate limit на `/api/login`.
- Нормальная смена пароля/пользователей в UI.
- Backup `/data/panel.db` и `/data/encryption.key`.
- Не хранить долговременные SSH password там, где можно использовать key auth.

---

## Проверки перед публикацией

В этой версии проверено:

- `python3 -m py_compile` для всех backend-модулей.
- Живой тестовый контейнер на `172.16.0.12:8888`: UI, login, `/api/servers`, `/api/peers`, `/api/traffic/summary`, `/api/connections`.
- Docker build свежего image.
- Smoke свежего image на отдельном порту.
- Исправлен packaging-регресс: Docker image теперь содержит полный `frontend/index.html`, а не старую урезанную копию.
- Исправлен remote SSH executor: stdin теперь работает для `docker exec -i` операций.
- Исправлен background traffic threshold worker: запрос приведён к реальной схеме `user_prefs`.

---

## Известные ограничения

- Local `awg-tunnel` bootstrap — тестовый helper, не полноценный production installer.
- IPv6 отдельно не прорабатывался.
- Нет RBAC/2FA.
- Нет автоматического backup/restore.
- Некоторые remote AWG операции зависят от конкретного layout контейнера и требуют live-тестов на настоящих серверах.

---

## License

MIT, если владелец проекта решит оставить эту лицензию. Пока это тестовый внутренний проект.
