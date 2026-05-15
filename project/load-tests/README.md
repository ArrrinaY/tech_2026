# Нагрузочное тестирование (Apache JMeter)

## Предусловия

1. Поднять инфраструктуру: из каталога `project` выполнить `docker compose up -d` (Postgres **`dating_user`** / **`dating_password`**, порт хоста **5433**, база **`dating_db`**).
2. Запустить **profile_service** на хосте (порт по умолчанию `8201`), чтобы были доступны `GET /health` и `GET /metrics`. Нужен `PYTHONPATH=project/src` и переменные к БД, например `DB_HOST=localhost`, `DB_PORT=5433`, `DB_USER=dating_user`, `DB_PASSWORD=dating_password`, `DB_NAME=dating_db`.

## Запуск плана `profile-service-smoke.jmx`

С установленным JMeter:

```bash
jmeter -n -t profile-service-smoke.jmx -l results/profile-smoke.jtl -e -o results/profile-smoke-report
```

Параметры хоста и порта (если сервис не на localhost:8201):

```bash
jmeter -Jhost=127.0.0.1 -Jport=8201 -n -t profile-service-smoke.jmx -l results/profile-smoke.jtl
```

Через Docker (пример с образом [justb4/jmeter](https://hub.docker.com/r/justb4/jmeter); версию подставьте свою):

```bash
docker run --rm --network host -v "%CD%":/tests -w /tests justb4/jmeter:5.6 jmeter -n -t profile-service-smoke.jmx -l results.jtl
```

Подробности и ожидаемые метрики см. `docs/LOAD_TESTING_JMETER.md`.
