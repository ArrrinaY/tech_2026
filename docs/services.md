# Описание сервисов (фактически в репозитории)

## 1. Bot Service (`project/src/bot_service/`)

- **Технологии:** aiogram 3.x, httpx, общий модуль `common` (конфиг, логи, метрики).
- **Без** FastAPI: бот — это процесс polling/long polling, не отдельный HTTP-сервер API.
- **Функции:** команды и сценарии в Telegram; все обращения к бизнес-логике идут в **Profile Service** по HTTP (`PROFILE_SERVICE_BASE_URL` из настроек).

## 2. Profile Service (`project/src/profile_service/`)

- **Технологии:** FastAPI, SQLAlchemy async, asyncpg, MinIO-клиент, Redis async, aio-pika для публикации событий.
- **Функции:**
  - пользователи, анкеты, предпочтения, лайки/пропуски, мэтчи;
  - **рейтинги всех трёх уровней** (первичный, поведенческий, комбинированный) — в этом же сервисе, не в отдельном «Rating Service»;
  - загрузка фото в MinIO, URL в `profiles.photo_urls`;
  - Redis-кэш очереди выдачи анкет (discovery);
  - при взаимном лайке — публикация **`match.created`** в RabbitMQ (`common/event_publisher.py`);
  - метрики и `/metrics`.

## 3. Celery worker (код рядом с Profile Service)

- **Файлы:** `profile_service/celery_app.py`, `profile_service/tasks.py`.
- **Функции:** фоновый пересчёт рейтингов, прогрев Redis-кэша по расписанию; брокер — **RabbitMQ**.

## 4. Redis

- Поднимается в **`project/docker-compose.yml`**; используется из **Profile Service** для кэша ленты (ключи вида `dating:discovery:<user_id>:queue`).

## 5. MinIO (S3-совместимое хранилище)

- Поднимается в **`project/docker-compose.yml`** (root **`dating_user`** / **`dating_password`**, API **localhost:9000**, консоль **http://localhost:9001**); запись объектов из **Profile Service** при загрузке фото анкеты.

## 6. RabbitMQ

- **Celery:** брокер задач (URL в `common/config.py` → `celery_app.py`).
- **События мэтча:** topic exchange `dating.events`, routing key `match.created` — публикует Profile Service; читает **`event_consumer`**.

## 7. Event Consumer (`project/src/event_consumer/`)

- Отдельный процесс: слушает очередь **`dating.bot_match_push`**, отправляет обоим пользователям сообщение через **Telegram Bot API** (`BOT_TOKEN` в окружении).

## 8. PostgreSQL

- Одна БД; сущности в `common/models.py`, доступ из Profile Service и Celery-задач через `common/database.py`.
