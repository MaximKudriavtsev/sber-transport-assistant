# Deployment

Single-server deployment на Linux (Ubuntu/Debian). Файлы лежат в `deploy/`.

## Что нужно на сервере

- Python **3.12** и `python3.12-venv`
- Git
- Nginx, если нужен домен или HTTPS
- systemd

Node.js не нужен. Frontend статический и отдаётся FastAPI.

## 1. Клонирование

```bash
sudo mkdir -p /opt/sber-transport-assistant
sudo chown "$USER":"$USER" /opt/sber-transport-assistant
git clone https://github.com/Gavrilov71/sber-transport-assistant.git /opt/sber-transport-assistant
cd /opt/sber-transport-assistant
```

## 2. Установка сервиса

```bash
sudo bash deploy/install.sh
```

Скрипт создаёт системного пользователя `sber-transport`, Linux `.venv`, ставит runtime-зависимости из `requirements.txt`, копирует `deploy/sber-transport.service` и запускает сервис.

Если `.env` ещё нет, скрипт копирует `.env.example`. До заполнения ключа приложение работает в demo mode. После правки ключа:

```bash
sudo systemctl restart sber-transport
```

Минимум в `.env`:

```env
APP_ENV=production
APP_HOST=127.0.0.1
APP_PORT=8000
GIGACHAT_CREDENTIALS=<Authorization Key>
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_MODEL=GigaChat-3-Ultra
GIGACHAT_VERIFY_SSL=false
```

`chmod 600 .env` делает `install.sh`. Не добавляйте `.env` в Git.

Проверка на самом сервере:

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/diagnostics/gigachat
```

`/api/health` должен вернуть `status: ok`. После настройки GigaChat `agent_ready` должен стать `true`.

## 3. Nginx

Сервер: `89.223.120.160`. Домен: `gorodvdele.ru`.

`deploy/install.sh` сам включает `deploy/nginx-sber-transport.conf` для `gorodvdele.ru` и `www.gorodvdele.ru`. Снаружи закрыты `/docs`, `/redoc`, `/openapi.json`, `/api/diagnostics/` и `/api/debug/`. Диагностика остаётся доступна с `127.0.0.1:8000`.

При `APP_ENV=production` приложение само не публикует OpenAPI.

HTTPS, если сертификата ещё нет:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d gorodvdele.ru -d www.gorodvdele.ru
```

## 4. Обновление

```bash
sudo bash deploy/update.sh
```

Перед обновлением на production можно прогнать тесты:

```bash
.venv/bin/pip install -r requirements-dev.txt
GIGACHAT_CREDENTIALS=test-placeholder-for-mocked-tests .venv/bin/python -m pytest -q
```

На сервере для работы приложения достаточно `requirements.txt`. `requirements-dev.txt` нужен для тестов и пересборки корпуса.

## 5. Один worker

Conversation state хранится в памяти процесса. Несколько Uvicorn workers получили бы разные состояния диалога. Пока нет Redis или БД, в unit-файле стоит `--workers 1`.

## 6. Rollback

```bash
git log --oneline -10
```

Верните известный стабильный commit и выполните `sudo systemctl restart sber-transport`. На production не делайте произвольный `git reset --hard` без фиксации текущего commit SHA.

## 7. Что не копировать с локальной машины

- `.venv/` с Windows или macOS;
- локальный `.env`;
- `references/` с исходниками дизайна;
- `raw_sources/` и большие PDF-кэши;
- `.pytest_cache/`, `__pycache__/`, файлы IDE.

Сервер сам создаёт Linux `.venv` из `requirements.txt`.
