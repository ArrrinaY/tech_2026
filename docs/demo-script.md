# Сценарии демонстрации — защита проекта (tech_2026)

**Важно:** в вашем проекте **внутренний** идентификатор пользователя в БД — `users.id` (его же ожидают `GET /api/v1/discovery/{user_id}/next` и ключ Redis `dating:discovery:<id>:queue`). `telegram_id` — это поле в таблице `users`, не ключ кэша ленты.

Подставьте свои числа там, где в примерах стоят `1`, `2`, `111111111` и т.д.

---

## Учётные данные инфраструктуры

Ниже — **фактические** значения из `project/docker-compose.yml` (их же подставляет Docker при создании контейнеров).

| Назначение | Адрес на хосте | Логин | Пароль | Как задано в compose |
|------------|----------------|-------|--------|----------------------|
| **PostgreSQL** | хост `localhost`, порт **5433** → в контейнере 5432 | `dating_user` | `dating_password` | `POSTGRES_USER`, `POSTGRES_PASSWORD`; база **`dating_db`** (`POSTGRES_DB`) |
| **RabbitMQ Management** | http://localhost:15672 | `dating_user` | `dating_password` | `RABBITMQ_DEFAULT_USER`, `RABBITMQ_DEFAULT_PASS` |
| **RabbitMQ AMQP** | `localhost:5672` | `dating_user` | `dating_password` | те же переменные |
| **MinIO Console** | http://localhost:9001 | `dating_user` | `dating_password` | `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` |
| **MinIO S3 API** | `localhost:9000` | `dating_user` | `dating_password` | те же |
| **Redis** | `localhost:6379` | — | *(пароль в compose не задан)* | без аутентификации |

Подключение к PostgreSQL **изнутри** контейнера (пароль для `psql` обычно не спрашивается):

```bash
docker exec -it dating_postgres psql -U dating_user -d dating_db
```

Подключение **с хоста** внешним клиентом: хост `localhost`, порт **5433**, пользователь **`dating_user`**, пароль **`dating_password`**, база **`dating_db`**.

---

## 0. Что поднято в Docker и что запускается вручную

### 0.1. Контейнеры из `project/docker-compose.yml`

Из каталога **`project/`**:

```bash
cd project
docker compose up -d
docker compose ps
```

Ожидаемые сервисы:

| Контейнер | Образ / роль |
|-----------|----------------|
| `dating_postgres` | PostgreSQL 15 |
| `dating_redis` | Redis 7 |
| `dating_rabbitmq` | RabbitMQ 3.12 + management |
| `dating_minio` | MinIO |
| `dating_event_consumer` | читает `match.created`, шлёт push в Telegram |

Порты на хосте (маппинг из `project/docker-compose.yml`):

| Сервис | Хост:порт |
|--------|-----------|
| PostgreSQL | **localhost:5433** → 5432 в контейнере |
| Redis | **localhost:6379** |
| RabbitMQ AMQP | **localhost:5672** |
| RabbitMQ Management UI | **http://localhost:15672** |
| MinIO S3 API | **localhost:9000** |
| MinIO Console | **http://localhost:9001** |

Логин **`dating_user`**, пароль **`dating_password`** (Postgres, RabbitMQ, MinIO root).

### 0.2. Profile Service и бот

Их нужно запускать **на машине разработчика** (или вынести в compose отдельно — сейчас так не сделано).

**Profile Service** :

```bash
pip install -r project/src/common/requirements.txt
pip install -r project/src/profile_service/requirements.txt
```

Файл настроек читается из **`project/.env`** (см. `common/config.py`: `BASE_DIR` указывает на каталог `project/`). Минимально для связи с Postgres **в контейнере** с хоста:

```env
DB_HOST=localhost
DB_PORT=5433
DB_USER=dating_user
DB_PASSWORD=dating_password
DB_NAME=dating_db
```

(Имена полей в Pydantic: `db_host`, `db_port` и т.д. — см. `project/src/common/config.py`.)

```bash
export PYTHONPATH=project/src   # Linux / macOS
# PowerShell: $env:PYTHONPATH = "project/src"

python -m uvicorn profile_service.main:app --host 0.0.0.0 --port 8201
```

Проверка:

```bash
curl -s http://localhost:8201/health
```

Ожидаемо: JSON с `"status":"healthy"`.

**Бот** (в другом терминале, тот же `PYTHONPATH`):

```bash
export BOT_TOKEN="ВАШ_ТОКЕН_ОТ_BOTFATHER"
python -m bot_service.main
```

**`event_consumer`** в compose читает тот же **`BOT_TOKEN`**: в **`project/.env`** (рядом с `docker-compose.yml`) добавьте строку `BOT_TOKEN=...` **до** `docker compose up`, иначе контейнер `dating_event_consumer` завершится сразу после старта (в коде проверка пустого токена).

### 0.3. Celery 

```bash
export PYTHONPATH=project/src
cd project/src   # удобно, чтобы относительные пути не путали; можно и из корня
celery -A profile_service.celery_app worker --loglevel=info
```

В третьем терминале beat:

```bash
export PYTHONPATH=project/src
celery -A profile_service.celery_app beat --loglevel=info
```

Расписание смотрите в `project/src/profile_service/celery_app.py` (`beat_schedule`: пересчёт рейтингов и прогрев Redis).

---

## 1. Swagger UI — живая документация API

Открыть в браузере: **http://localhost:8201/docs**


1. Список тегов и путей: регистрация, профили, `discovery`, `interactions`, `matches`, админ-задачи Celery, `/metrics`.
2. Выполнить **Try it out**:
   - `POST /api/v1/users/register` с телом, например:
     ```json
     {"telegram_id": 100000001, "username": "demo_user_1", "first_name": "Демо1"}
     ```
   - В ответе запомнить **`id`** пользователя — это **`users.id`** для дальнейших шагов.
3. `GET /api/v1/users/{telegram_id}` — тем же `telegram_id`, что при регистрации.
4. `GET /api/v1/profiles/{user_id}` — подставить **`user_id` = internal id**, не telegram.

**Зачем отдельно говорить:** в ТЗ часто путают `telegram_id` и внутренний `id`; discovery и Redis завязаны на **внутренний** `users.id`.

---

## 2. PostgreSQL — схема, таблицы, индексы


```bash
docker exec -it dating_postgres psql -U dating_user -d dating_db
```

### 2.1. Список таблиц

```sql
\dt
```

Ожидаемые имена (создаются через `init_db()` → `Base.metadata.create_all` при старте Profile Service, см. `common/database.py` и `common/models.py`):  
`users`, `profiles`, `preferences`, `ratings`, `interactions`.

### 2.2. Структура и индексы рейтингов

```sql
\d ratings
```

Показать индекс **`ix_ratings_combined_score`** (сортировка/выдача по комбинированному рейтингу в discovery).

### 2.3. Структура и индексы взаимодействий

```sql
\d interactions
```

Полезно прокомментировать:

- **`ix_interactions_target_action`** — ускоряет подсчёты для поведенческого рейтинга по целевому профилю и типу действия.
- **`ix_interactions_actor_target`** — пары «кто — на чью анкету».
- **`ix_interactions_target_match`** — выборки мэтчей.

### 2.4. Выборка: пользователи и их рейтинги

```sql
SELECT u.id AS user_id, u.telegram_id, p.id AS profile_id,
       r.primary_score, r.behavioral_score, r.combined_score
FROM users u
JOIN profiles p ON p.user_id = u.id
LEFT JOIN ratings r ON r.profile_id = p.id
ORDER BY u.id;
```

 Мэтч хранится как пара взаимных лайков в **`interactions`** с флагом **`is_match = true`** (см. логику в `profile_service/main.py`, эндпоинт создания взаимодействия и `GET /api/v1/matches/{user_id}`).

Пример выборки последних взаимодействий с мэтчем:

```sql
SELECT id, actor_user_id, target_profile_id, action, is_match, created_at
FROM interactions
WHERE is_match = true
ORDER BY created_at DESC
LIMIT 10;
```

### 2.5. Выход из psql

```sql
\q
```

---

## 3. Redis — кэш очереди выдачи (discovery)

Ключ в коде: **`dating:discovery:<users.id>:queue`** (список в Redis, TTL **1800** секунд). См. `get_discovery_cache_key`, `rebuild_discovery_cache`, `pop_next_cached_profile_id` в `project/src/profile_service/main.py`.

### 3.1. Вход в redis-cli

```bash
docker exec -it dating_redis redis-cli
```

### 3.2. Подготовка данных (если кэш пуст)

Сначала с хоста дерните API выдачи (подставьте **внутренний** `user_id` пользователя, который уже с анкетой с заполненными **возрастом и полом** — иначе discovery вернёт ошибку/пусто по бизнес-правилам):

```bash
curl -s http://localhost:8201/api/v1/discovery/1/next
```

(Замените **`1`** на реальный `users.id`.)

### 3.3. Просмотр ключа в Redis

В `redis-cli` (пример для пользователя с internal id = 1):

```redis
TYPE dating:discovery:1:queue
LLEN dating:discovery:1:queue
LRANGE dating:discovery:1:queue 0 -1
TTL dating:discovery:1:queue
```

**Что сказать устно:**

- Список — это **очередь id профилей** для показа в ленте; **`LPOP`** в коде забирает следующий id.
- При нехватке записей вызывается **`rebuild_discovery_cache`** (пересборка из PostgreSQL с учётом предпочтений и уже просмотренных).
- Celery-периодическая задача **`warm_discovery_cache`** прогревает кэш для всех пользователей (см. `project/src/profile_service/tasks.py`).

### 3.4. Выход

```redis
EXIT
```

---

## 4. Ранжирование — три уровня 

Логика в **`project/src/profile_service/main.py`**:  
`calculate_primary_score`, `calculate_behavioral_score`, `calculate_combined_score`, модель **`ratings`** в `common/models.py`.

### 4.1. Первичный (уровень 1)

Зависит от заполненности полей профиля и **наличия фото** (см. тесты в `project/src/profile_service/tests/test_scoring.py`).

### 4.2. Поведенческий (уровень 2)

Считается по **`interactions`** для **целевого** профиля (лайки / пропуски / мэтчи), один агрегирующий запрос в `calculate_behavioral_score`.

### 4.3. Комбинированный (уровень 3)

Веса: **0.6** × первичный + **0.4** × поведенческий (функция `calculate_combined_score`).

### 4.4. Демонстрация «до / после» через SQL

Посмотреть текущие значения для конкретного `profile_id` (например, 2):

```sql
SELECT primary_score, behavioral_score, combined_score
FROM ratings
WHERE profile_id = 2;
```

Добавить лайк через API (подставьте **`actor_user_id`** и **`target_profile_id`** из вашей БД):

```bash
curl -s -X POST http://localhost:8201/api/v1/interactions \
  -H "Content-Type: application/json" \
  -d '{"actor_user_id":1,"target_profile_id":2,"action":"like"}'
```

Снова выполнить `SELECT` по `ratings` для `profile_id = 2` — **`behavioral_score`** и **`combined_score`** должны обновиться после пересчёта в рамках запроса (см. обработчик в `main.py`).

---

## 5. RabbitMQ — два разных сценария

Открыть: **http://localhost:15672**  
Логин: **`dating_user`**, пароль: **`dating_password`**.

### 5.1. Celery

Вкладка **Queues**: появятся очереди с префиксом **`celery`** (имена хэшируются/зависят от конфигурации). **Брокер** задаётся в `project/src/profile_service/celery_app.py` через `settings.rabbitmq_url` (`common/config.py`).

**Что сказать:** это **транспорт задач** (пересчёт рейтингов, прогрев Redis), не путать с доменными событиями мэтча.

### 5.2. Topic exchange `dating.events` и очередь `dating.bot_match_push`

Публикует **Profile Service** (`common/event_publisher.py`) при **`match.created`**.  
Читает **`event_consumer`** (`project/src/event_consumer/main.py`): привязка очереди к ключу **`match.created`**.

В UI: **Exchanges** → `dating.events` (после первого успешного старта API и публикации; exchange объявляется при подключении publisher). **Queues** → `dating.bot_match_push`.

### 5.3. Проверка push о мэтче

1. Убедиться, что **`dating_event_consumer`** в статусе **Up** и в логах нет ошибки про `BOT_TOKEN`:
   ```bash
   docker logs dating_event_consumer --tail 50
   ```
2. В Telegram с **двух** тестовых аккаунтов пройти сценарий **взаимного лайка** (или вызвать **`POST /api/v1/interactions`** дважды с разными актёрами на профили друг друга — как в бизнес-логике мэтча).
3. Оба пользователя должны получить **личное сообщение** в Telegram (текст из `_handle_match_created` в `event_consumer/main.py`).

---

## 6. Celery — фоновые задачи (на хосте)

### 6.1. Ручной вызов задачи пересчёта (пример)

Из каталога с `PYTHONPATH=project/src`:

```bash
celery -A profile_service.celery_app call profile_service.tasks.recalculate_all_ratings
```

или прогрев кэша:

```bash
celery -A profile_service.celery_app call profile_service.tasks.warm_discovery_cache
```

### 6.2. Через HTTP API Profile Service (постановка в очередь Celery)

```bash
curl -s -X POST http://localhost:8201/api/v1/admin/tasks/recalculate-ratings
curl -s -X POST http://localhost:8201/api/v1/admin/tasks/warm-discovery-cache
```

В ответе — статус **`queued`** и **`task_id`** (см. обработчики в `main.py`).

### 6.3. Логи worker

Смотрите терминал, где запущен **`celery worker`**. В логах должны быть JSON-сообщения structlog из `tasks.py` (например, `ratings_recalculated`, `discovery_cache_warmed`).

---

## 7. MinIO — хранилище фотографий

Консоль: **http://localhost:9001**  
Логин **`dating_user`**, пароль **`dating_password`** (в compose: `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`). Консоль: **http://localhost:9001**.

**Что показать:**

- Бакет **`dating-photos`** (имя из `common/config.py`, создаётся при старте Profile Service).
- Объекты вида **`user-<internal_user_id>/...`** после загрузки фото через **`POST /api/v1/profiles/{user_id}/photos`**.

Публичная политика чтения задаётся в коде (`ensure_minio_bucket_public_read` в `profile_service/main.py`), чтобы бот мог открывать URL картинок.

---

## 8. Метрики Prometheus 

Метрики отдаёт **сам** Profile Service.

```bash
curl -s http://localhost:8201/metrics | head -40
```

Примеры имён из `common/metrics.py`:

- `request_duration_seconds_bucket` (гистограмма длительности запросов),
- `rating_calculation_duration_seconds_*`,
- `cached_profiles_count`,
- `domain_events_published_total` (счётчик публикаций в topic exchange),
- счётчики бота `bot_messages_processed_total` (если смотреть экспорт с другого процесса — у бота отдельного `/metrics` нет, метрики собираются в том же registry при интеграции; на защите достаточно показать `/metrics` API).

Покажите рост счётчиков после нескольких запросов к `/health` и регистрации.

---

## 9. Структурированные логи (structlog + JSON)

Конфигурация: **`project/src/common/logging_config.py`**.

**Profile Service** — логи в stdout процесса uvicorn (каждая строка JSON при настроенном `JsonFormatter` + structlog).

**Пример просмотра логов consumer:**

```bash
docker logs dating_event_consumer --tail 30
```

Ищите строки вида **`match_push_sent`** после успешной отправки в Telegram.

**Бот** — аналогично в терминале, где запущен `python -m bot_service.main`.

---

## 10. Автотесты (pytest)

Из **корня** репозитория `tech_2026/`:

```bash
pip install -r project/src/common/requirements.txt
pip install -r project/src/profile_service/requirements.txt
pip install -r project/src/bot_service/requirements.txt
export PYTHONPATH=project/src
pytest project/src/profile_service/tests project/src/bot_service/tests -v
```

Ожидаемо: **`26 passed`** 

---

## 11. Бот — живая демонстрация в Telegram

Рекомендуемая последовательность (команды из меню настраиваются в `bot_service/main.py`):

1. **`/start`** — регистрация пользователя через API, приветствие.
2. **`/fill`** — мастер заполнения анкеты (имя, био, возраст, пол, город, фото по шагам).
3. **`/profile`** — своя анкета и строки рейтинга (первичный / поведенческий / комбинированный).
4. **`/search`** — лента: лайк / пропуск; после **взаимного** лайка — уведомление в личку от **`event_consumer`** + мэтчи в **`/matches`**.
5. **`/help`** — напоминание команд.

**На что обратить внимание преподавателя:** бот **не** содержит бизнес-логики рейтингов — только вызывает HTTP API Profile Service.

---

## 12. Нагрузочное и ручное тестирование

- **JMeter:** `project/load-tests/profile-service-smoke.jmx`, инструкции — `project/load-tests/README.md` и `docs/LOAD_TESTING_JMETER.md`.
- **Ручной чек-лист:** `docs/MANUAL_TESTING.md`.

---

## Быстрая шпаргалка — ответы на типовые вопросы

| Вопрос | Ответ (по вашему проекту) |
|--------|---------------------------|
| Зачем Redis, если есть PostgreSQL? | Выдача следующей анкеты не должна каждый раз гонять тяжёлый запрос с сортировкой; в Redis — готовая **очередь id** профилей с TTL и метрикой размера кэша. |
| Зачем RabbitMQ дважды? | **Celery** использует брокер для **задач**; отдельно **topic** `dating.events` / `match.created` — для **асинхронного push** в Telegram через `event_consumer`, чтобы API не ждал ответа Bot API. |
| Зачем Celery, если рейтинг пересчитывается при запросах? | Периодический **полный** пересчёт и **прогрев** кэша для всех пользователей — фоново, по расписанию (`beat_schedule`). |
| Почему нет отдельного Rating Service? | Рейтинги — часть доменной логики Profile Service и таблицы `ratings`; отдельного deployable-сервиса в репозитории нет (см. `docs/architecture.md`). |
| Где хранятся мэтчи? | В **`interactions`** с `is_match=true`; отдельной таблицы `matches` нет. |
| Почему `event_consumer` падает при старте? | Не задан **`BOT_TOKEN`** в `project/.env` для подстановки в `docker compose`. |

---


- [ ] `docker compose ps` — **5** сервисов **Up**, особенно **`dating_event_consumer`**.
- [ ] `curl http://localhost:8201/health` — OK.
- [ ] `http://localhost:8201/docs` открывается.
- [ ] `http://localhost:15672` — RabbitMQ Management: логин **`dating_user`**, пароль **`dating_password`**.
- [ ] В Telegram готовы **два** тестовых аккаунта для мэтча.
- [ ] `pytest` — **26** зелёных тестов (или актуальное число после ваших правок).


