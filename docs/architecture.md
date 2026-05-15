# Архитектура (как устроено в репозитории)

Ниже описано **фактическое** состояние кода в `project/`, без вымышленных отдельных микросервисов.

## 1. Пользователь и Telegram

Пользователь пишет боту в Telegram → сообщение проходит через **Telegram Bot API** (внешний сервис).

## 2. Bot Service (`bot_service`)

- Стек: **Python + aiogram 3** (отдельного FastAPI в боте нет).
- Роль: команды, кнопки, FSM анкеты, запросы к backend по **HTTP** к **одному** сервису — **Profile Service**.

## 3. Profile Service (`profile_service`)

- Стек: **FastAPI + SQLAlchemy async**.
- В **этом же** сервисе реализованы:
  - анкеты, предпочтения, лайки/пропуски, мэтчи;
  - расчёт **всех трёх уровней рейтинга** (первичный, поведенческий, комбинированный) и таблица `ratings` в общей БД;
  - загрузка фото в **MinIO** (S3-совместимое API);
  - кэш ленты знакомств в **Redis** (списки id профилей);
  - публикация в **RabbitMQ** события **`match.created`** (topic exchange `dating.events`) после взаимного лайка.

Отдельного процесса или пакета **«Rating Service»** в репозитории **нет** — это не отдельный deployable-сервис, а часть Profile Service.

## 4. Worker: Celery

- Код: `profile_service/celery_app.py`, `profile_service/tasks.py`.
- Брокер задач: **RabbitMQ** (тот же кластер, что и для событий, но **другая семантика**: очереди Celery, не topic `dating.events`).
- Задачи: периодический пересчёт рейтингов, прогрев Redis-кэша ленты (см. `beat_schedule` в `celery_app.py`).

## 5. Кэш: Redis

- Не отдельный микросервис «Cache Service», а **Redis** как инфраструктура, к которой подключается Profile Service (контейнер **`dating_redis`**, на хосте **localhost:6379**, пароль в compose не задан; клиент в `profile_service/main.py`).

## 6. Хранилище фото: MinIO

- S3-совместимое хранилище; загрузка и политика bucket в Profile Service.

## 7. Уведомления о мэтче: `event_consumer`

- Отдельный процесс в репозитории: `event_consumer/main.py`.
- Слушает очередь **`dating.bot_match_push`**, ключ **`match.created`**.
- Вызывает **Telegram Bot API** (`sendMessage`), чтобы оба пользователя получили сообщение о мэтче. В **`project/.env`** должен быть задан **`BOT_TOKEN`** (тот же, что у бота); в compose он подставляется в контейнер **`dating_event_consumer`**. Подключение к RabbitMQ в том же compose: **`dating_user`** / **`dating_password`** (AMQP **localhost:5672** с хоста при поднятом `dating_rabbitmq`).

## 8. База данных: PostgreSQL

- Одна БД для пользователей, профилей, предпочтений, взаимодействий и рейтингов (схема в `common/models.py`).

## Логи и метрики

- **structlog** + JSON-логирование: `common/logging_config.py`; подключение в profile service, bot, Celery-задачах.
- **Prometheus-client**: `common/metrics.py`, эндпоинт **`GET /metrics`** в Profile Service; в боте — счётчики/гистограммы при обработке сообщений.

![alt text](image-1.png)
