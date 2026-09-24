# SBER Transport Assistant

ИИ-ассистент для пассажиров общественного транспорта Тульской области. Проект создан для хакатона: пользователь задаёт вопрос обычным языком, GigaChat определяет задачу и при необходимости вызывает локальные проверяемые инструменты — поиск по официальным источникам, определение маршрута, выбор ответственного органа и экстренную памятку.

## Что умеет

- отвечает на типовые вопросы о проезде, оплате, льготах, маршрутах и расписании;
- понимает разговорные формулировки и ведёт многошаговый диалог;
- показывает только применимые официальные источники;
- не подставляет перевозчика или ведомство по совпадению текста — это делают детерминированные Router/RouteResolver;
- различает обычную проблему, жалобу и ситуацию безопасности;
- при неоднозначной опасной ситуации сначала уточняет, продолжается ли поездка;
- защищает пользовательский ответ от служебной разметки и неподтверждённых конкретных чисел/сроков/телефонов;
- включает frontend без отдельной сборки: HTML/CSS/JS отдаёт сам FastAPI.

## Стек

- Python 3.12+
- FastAPI + Uvicorn
- GigaChat API (`GigaChat-3-Ultra`)
- локальный BM25/fuzzy поиск по подготовленному корпусу официальных материалов
- Vanilla HTML/CSS/JavaScript

**Node.js не нужен.** В проекте нет npm/Vite/React-сборки.

## Быстрый локальный запуск Windows

1. Скопируйте `.env.example` в `.env`.
2. Заполните `GIGACHAT_CREDENTIALS` локально. Никогда не коммитьте `.env`.
3. Запустите `start.cmd` или `start.ps1`.
4. Откройте `http://127.0.0.1:8000`.

Скрипт создаст `.venv`, установит зависимости и запустит Uvicorn.

Ручной запуск:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Переменные окружения

Минимально требуется:

```env
GIGACHAT_CREDENTIALS=
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_MODEL=GigaChat-3-Ultra
GIGACHAT_VERIFY_SSL=false
```

Без `GIGACHAT_CREDENTIALS` приложение запускается в demo mode, но агент не отвечает через GigaChat.

## Проверка

```bash
pip install -r requirements-dev.txt
GIGACHAT_CREDENTIALS=test-placeholder python -m pytest -q
```

Тесты мокируют сетевые вызовы GigaChat, поэтому реальный ключ для CI не нужен. На момент подготовки репозитория: **48 tests passed**.

Health-check:

```text
GET /api/health
```

Диагностика GigaChat:

```text
GET /api/diagnostics/gigachat
```

## Структура

```text
app/
  main.py                 FastAPI и HTTP API
  agent_service.py        главный semantic-first agent loop
  agent_tools.py          функции, доступные GigaChat
  conversation.py         состояние многошагового диалога
  fact_guard.py           защита конкретных фактов
  responsibility.py       детерминированный Responsibility Router
  route_resolver.py       детерминированный Route Resolver
  text_search.py          поиск по официальному корпусу
  gigachat_client.py      OAuth + GigaChat API
  data/                   подготовленные проверяемые данные
  static/                 готовый frontend и его production-assets

tests/                    regression/API/router tests
docs/ARCHITECTURE.md      архитектура и инварианты
docs/DEPLOY.md            инструкция для Linux-сервера
docs/PROJECT_STATE.md     текущее стабильное состояние
```

## Данные и источники

Runtime использует подготовленные файлы `app/data/chunks.json`, `sources.json`, `routes.json`, `authorities.json`, `municipalities.json`, `responsibility_rules.json` и `emergency_guidance.json`.

Сырые HTML/PDF-копии официальных сайтов не хранятся в Git: они являются воспроизводимым кэшем. При необходимости корпус можно пересобрать:

```bash
python -m app.ingest
```

Команда создаёт локальные `app/data/raw_sources/` и `app/data/source_snapshots.json`; они исключены через `.gitignore`.

## Сервер

Для production нужны Python 3.12, systemd и Nginx. На сервере, в каталоге проекта:

```bash
sudo bash deploy/install.sh
```

Сайт: [gorodvdele.ru](https://gorodvdele.ru), сервер `89.223.120.160`. Обновление: `sudo bash deploy/update.sh`. Полная инструкция — в [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Безопасность

- `.env`, виртуальные окружения, raw source cache и runtime-логи исключены из Git;
- ключ GigaChat создаётся вручную только на сервере;
- repository не должен содержать OAuth/API credentials;
- debug trace endpoint работает только при `APP_ENV=development|dev`.
