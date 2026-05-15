# Dating Telegram Bot

Telegram-бот знакомств: анкеты, лента с лайками/пропусками, три уровня рейтинга, Redis-кэш выдачи, MinIO для фото, фоновые задачи Celery, push о мэтче через RabbitMQ и отдельный `event_consumer


## Статус этапов

| Этап | Описание | Статус |
|------|-----------|--------|
| 1 | Планирование, архитектура, схема БД, метрики/логи | готово |
| 2 | Бот, регистрация, анкета, Docker Compose (инфра) | готово |
| 3 | Лента, лайки/пропуски, рейтинги, Redis, меню команд | готово |
| 4 | Celery + beat, MinIO, push о мэтче, тесты, CI, JMeter, чек-листы | готово |

---

## Стек

| Слой | Технологии |
|------|------------|
| Бот | Python 3.11, aiogram 3, httpx |
| API | FastAPI, SQLAlchemy 2 (async), asyncpg, Pydantic v2 |
| БД | PostgreSQL 15 |
| Кэш | Redis 7 |
| Очереди | RabbitMQ 3 (Celery + topic-события `match.created`, aio-pika / pika) |
| Фон | Celery, Celery Beat |
| Фото | MinIO (S3-совместимый API) |
| Метрики | prometheus-client, `GET /metrics` |
| Логи | structlog + JSON (`python-json-logger`) |
| Инфра | Docker Compose (`project/docker-compose.yml`) |
| CI | GitHub Actions (profile + bot) |
| Нагрузка | Apache JMeter (`project/load-tests/`) |

---

## Архитектура (фактически в репозитории)

```
Telegram User
      │
      ▼
  bot_service (aiogram) ──HTTP──► profile_service (FastAPI)
      │                                    │
      │                              PostgreSQL
      │                              Redis (кэш ленты)
      │                              MinIO (фото)
      │                                    │
      │                                    ├── publish match.created ──► RabbitMQ topic "dating.events"
      │                                    │
      └── polling ◄────────────────────────┴── Celery worker (тот же RabbitMQ как broker задач)

RabbitMQ  ◄──  event_consumer  ──►  Telegram Bot API (sendMessage обоим при мэтче, нужен BOT_TOKEN)
```

- **Отдельного микросервиса «Rating» нет:** все три уровня рейтинга считаются в `profile_service` (модель `ratings` в общей БД).
- **Два сценария RabbitMQ:** очереди **Celery** (пересчёт рейтингов, прогрев кэша) и **topic** `dating.events` / `match.created` для `event_consumer` (не путать с транспортом задач Celery).

Подробнее: `docs/architecture.md`, `docs/services.md`.

---

## Схема базы данных

См. SQLAlchemy-модели в `project/src/common/models.py`.

| Таблица | Назначение |
|---------|------------|
| `users` | `telegram_id`, имя, связь с профилем |
| `profiles` | био, интересы, фото (URL), возраст, пол, город, `completeness_score` |
| `preferences` | фильтры ленты (возраст, пол, город) |
| `ratings` | `primary_score`, `behavioral_score`, `combined_score` |
| `interactions` | лайк / пропуск / суперлайк, флаг мэтча |

---

## Ранжирование (три уровня)

1. **Первичный** — заполненность анкеты (в т.ч. наличие фото), шкала до 100.  
2. **Поведенческий** — статистика по `interactions` (лайки, пропуски, мэтчи).  
3. **Комбинированный** — взвешенная сумма первичного и поведенческого (см. `calculate_combined_score` в `profile_service/main.py`).

Пересчёт: при изменениях профиля/взаимодействий и по расписанию Celery Beat.

---

## Redis

- Ключи вида `dating:discovery:<internal_user_id>:queue` — список id профилей для выдачи в ленте, TTL 30 мин.  
- Прогрев: задача `warm_discovery_cache` в `profile_service/tasks.py`.

---

## API (Profile Service)

Базовый URL по умолчанию: `http://localhost:8201` (порт в `common/config.py` → `profile_service_port`).

| Метод | Путь | Назначение |
|-------|------|------------|
| GET | `/health` | Проверка живости |
| GET | `/metrics` | Prometheus |
| POST | `/api/v1/users/register` | Регистрация |
| GET/PUT/DELETE | `/api/v1/users/{telegram_id}` | Пользователь |
| POST/GET/PUT/DELETE | `/api/v1/profiles`, `/api/v1/profiles/{user_id}` | Анкета |
| POST | `/api/v1/profiles/{user_id}/photos` | Загрузка фото → MinIO |
| POST | `/api/v1/preferences` | Предпочтения |
| GET | `/api/v1/discovery/{user_id}/next` | Следующий кандидат |
| POST | `/api/v1/interactions` | Лайк / пропуск / суперлайк |
| GET | `/api/v1/matches/{user_id}` | Мэтчи |
| POST | `/api/v1/admin/tasks/recalculate-ratings` | Поставить задачу пересчёта |
| POST | `/api/v1/admin/tasks/warm-discovery-cache` | Поставить прогрев кэша |

Swagger UI FastAPI: `http://localhost:8201/docs` (если сервис запущен локально).

---

## Команды бота (меню)

| Команда | Действие |
|---------|----------|
| `/start` | Старт, регистрация пользователя через API |
| `/help` | Справка |
| `/fill` | Мастер заполнения анкеты |
| `/profile` | Показать свою анкету |
| `/search` | Лента и лайки/пропуски |
| `/matches` | Список мэтчей |

Дополнительно: inline-кнопки главного меню (см. `bot_service/main.py`).

---

## Тесты

### Сводка

| Показатель | Значение |
|------------|----------|
| **Всего автотестов** | **26** (`def test_…`) |
| **Область** | `project/src/…/tests/` — папка `tasks/` в этот счёт не входит |
| **Конфиг async** | корневой `pytest.ini` (`asyncio_mode = auto`), зависимость `pytest-asyncio` в `bot_service/requirements.txt` |

### Запуск из корня репозитория

Каталог запуска — **`tech_2026/`** (чтобы подхватился `pytest.ini`).

**Linux / macOS / Git Bash**

```bash
pip install -r project/src/common/requirements.txt
pip install -r project/src/profile_service/requirements.txt
pip install -r project/src/bot_service/requirements.txt
export PYTHONPATH=project/src
pytest project/src/profile_service/tests project/src/bot_service/tests -v
```

**Windows (PowerShell)**

```powershell
pip install -r project/src/common/requirements.txt
pip install -r project/src/profile_service/requirements.txt
pip install -r project/src/bot_service/requirements.txt
$env:PYTHONPATH = "project/src"
pytest project/src/profile_service/tests project/src/bot_service/tests -v
```

### Состав тестов

#### `project/src/profile_service/tests/test_scoring.py` — 6 тестов

| Тест | Что проверяет |
|------|----------------|
| `test_calculate_completeness_score_full_profile` | полная анкета → полнота **1.0** |
| `test_calculate_completeness_score_partial_profile` | частичная анкета → полнота **0.4** |
| `test_calculate_combined_score` | комбинированный рейтинг: `80` + `50` → **68** |
| `test_calculate_primary_score_full` | первичный рейтинг при полном профиле **с фото** → **100** |
| `test_calculate_primary_score_without_photo` | первичный рейтинг **без фото** → **80** |
| `test_get_discovery_cache_key` | ключ Redis для ленты: `dating:discovery:42:queue` |

#### `project/src/bot_service/tests/test_profile_api_client.py` — 19 тестов

Проверяют **реальный слой бота к Profile Service** и вспомогательные функции из `bot_service/main.py` через **`httpx.MockTransport`** (без Telegram и без живого API).

| Тест | Что проверяет |
|------|----------------|
| `test_register_user_returns_json_on_201` | `POST /api/v1/users/register` → **201**, разбор JSON |
| `test_register_user_returns_json_on_409` | **409** (уже есть пользователь) → JSON |
| `test_register_user_returns_none_on_error_status` | **500** → `None` |
| `test_update_user_name_success` / `…_failure` | `PUT /api/v1/users/{telegram_id}` → **200** / **404** |
| `test_get_user_from_profile_service_200_and_cache` | `GET /users` + **кэш** бота (второй вызов без второго HTTP) |
| `test_get_user_from_profile_service_404` | пользователь не найден |
| `test_get_profile_from_profile_service` | `GET /api/v1/profiles/{user_id}` |
| `test_get_next_discovery_profile_200_and_404` | `GET /discovery/.../next` → **200** затем **404** |
| `test_send_interaction_to_profile_service` | `POST /api/v1/interactions` |
| `test_get_matches_200_and_404_empty` | `GET /matches` → список и **404 → []** |
| `test_save_profile_data` | `PUT` профиля → успех / **422** |
| `test_upload_profile_photo` | `POST …/photos` → **200** / ошибка |
| `test_prioritize_photo_urls_prefers_minio_host` | сортировка URL фото (MinIO вперёд) |
| `test_format_gender_russian_and_unknown` | `format_gender` |
| `test_is_skip_command_variants` | пропуск шага анкеты по тексту |
| `test_should_rate_limit_search_action` | антиспам ленты |
| `test_is_duplicate_rate_action` | дедуп лайка |
| `test_get_search_keyboard_contains_profile_id` | callback_data клавиатуры лайка/пропуска |

#### `project/src/bot_service/tests/test_smoke.py` — 1 тест

| Тест | Что проверяет |
|------|----------------|
| `test_settings_load` | `get_settings()`, порт profile service **8201** |

Между тестами бота **`conftest.py`** сбрасывает глобальный `httpx.AsyncClient`, кэш пользователя и словари rate-limit.

### CI

`.github/workflows/profile-service-ci.yml` и `bot-service-ci.yml`, `PYTHONPATH=project/src`. Для бота в триггеры добавлен корневой `pytest.ini`.

---

## Локальный запуск

### 1. Инфраструктура

Из каталога `project/`:

```bash
docker compose up -d
```

Поднимаются: PostgreSQL (порт хоста **5433** → 5432 в контейнере, пользователь **`dating_user`**, пароль **`dating_password`**, база **`dating_db`**), Redis, RabbitMQ (**`dating_user`** / **`dating_password`**), MinIO (root **`dating_user`** / **`dating_password`**), **event_consumer**.

В **`project/.env`** (или переменных окружения перед `docker compose`) задайте **`BOT_TOKEN`** — тот же, что у бота; иначе `event_consumer` завершится с ошибкой (нужен для push о мэтче).

### 2. Profile Service

```bash
set PYTHONPATH=project\src
pip install -r project\src\profile_service\requirements.txt
pip install -r project\src\common\requirements.txt
python -m uvicorn profile_service.main:app --host 0.0.0.0 --port 8201
```

По умолчанию в **`project/.env`** достаточно `DB_HOST=localhost`, `DB_PORT=5433`, `DB_USER=dating_user`, `DB_PASSWORD=dating_password`, `DB_NAME=dating_db` (остальные поля — в `project/src/common/config.py`).

### 3. Celery (опционально)

Worker и beat из каталога с `PYTHONPATH=project\src`, команды как в документации Celery для приложения `profile_service.celery_app:celery_app`.

### 4. Бот

```bash
set PYTHONPATH=project\src
set BOT_TOKEN=ваш_токен
python -m bot_service.main
```

На Windows можно использовать `project/botctl.ps1` для profile + bot (см. скрипт).

---

## Структура репозитория

```text
tech_2026/
├── project/
│   ├── docker-compose.yml      # Postgres, Redis, RabbitMQ, MinIO, event_consumer
│   ├── botctl.ps1              # удобный запуск profile + bot (Windows)
│   ├── load-tests/             # JMeter: profile-service-smoke.jmx, README
│   └── src/
│       ├── common/             # config, models, db, metrics, logging, event_publisher
│       ├── bot_service/        # aiogram, main.py, tests/
│       ├── profile_service/    # FastAPI, Celery, tasks, tests/
│       └── event_consumer/     # push о мэтче через Telegram API
├── docs/                       # architecture, services, MANUAL_TESTING, JMeter, схема БД
├── tasks/                      # учебные задания курса (отдельно от продукта в project/)
├── .github/workflows/
└── README.md                   # этот файл
```

---

## Документация и артефакты защиты

| Файл | Содержание |
|------|------------|
| `docs/architecture.md` | Архитектура по факту кода |
| `docs/services.md` | Описание сервисов |
| `docs/demo-script.md` | Пошаговые сценарии демонстрации на защиту |
| `docs/MANUAL_TESTING.md` | Чек-лист ручных тестов |
| `docs/LOAD_TESTING_JMETER.md` | Нагрузочное тестирование JMeter |
| `project/load-tests/README.md` | Запуск JMeter |

---

## Соответствие критериям оценивания (ориентир)

| Критерий | Где смотреть |
|----------|----------------|
| Рейтинг (3 уровня) | `common/models.py` (`ratings`), расчёт в `profile_service/main.py` |
| Redis | Кэш ленты `dating:discovery:*` |
| Celery | `profile_service/celery_app.py`, `tasks.py` |
| MQ (не только Celery) | `match.created` → `common/event_publisher.py`, `event_consumer/main.py` |
| Метрики и логи | `common/metrics.py`, `common/logging_config.py`, `/metrics` |
| S3 / MinIO | загрузка фото в `profile_service/main.py` |
| CI для бота | `.github/workflows/bot-service-ci.yml` |
| JMeter | `project/load-tests/*.jmx`, `docs/LOAD_TESTING_JMETER.md` |

---

## Полезные URL (локально)

| Сервис | URL |
|--------|-----|
| Swagger (Profile Service) | http://localhost:8201/docs |
| Метрики | http://localhost:8201/metrics |
| RabbitMQ Management | http://localhost:15672 — логин **`dating_user`**, пароль **`dating_password`** |
| MinIO Console | http://localhost:9001 — **`dating_user`** / **`dating_password`** |
