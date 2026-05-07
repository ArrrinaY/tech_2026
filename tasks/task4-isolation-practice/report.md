# Отчет: Аномалии изоляции в SQL

**База данных:** PostgreSQL (docker `dating_postgres`)  
**Аномалии:** Dirty Read, Non-Repeatable Read, Phantom Read, Lost Update

## Подготовка окружения

```powershell
cd C:\tech_2026
Get-Content -Raw "tasks/task4/setup.sql" | docker exec -i dating_postgres psql -U dating_user -d dating_db
```

Открыть два терминала и в каждом подключиться к БД:

```powershell
docker exec -it dating_postgres psql -U dating_user -d dating_db
```

---

## Dirty Read

Сценарий: `../dirty_read.sql`

![Dirty Read completed scenarios](./dirty_read_completed_scenarios.png)

Итог: в PostgreSQL грязное чтение не проявляется, читаются только зафиксированные данные.

Как избежать:
- `READ COMMITTED` и выше;
- не использовать режимы чтения незакоммиченных данных.

---

## Non-Repeatable Read

Сценарий: `../non_repeatable_read.sql`

![Non-Repeatable Read completed scenarios](./non_repeatable_read_completed_scenarios.png)

Итог: в одной транзакции одинаковый `SELECT` вернул разные значения.

Как избежать:
- `REPEATABLE READ` или `SERIALIZABLE`.

---

## Phantom Read

Сценарий: `../phantom_read.sql`

![Phantom Read completed scenarios](./phantom_read_completed_scenarios.png)

Итог: повторный запрос по условию вернул другое число строк.

Как избежать:
- `REPEATABLE READ` / `SERIALIZABLE`.

---

## Lost Update

Сценарий: `../lost_update.sql`

![Lost Update completed scenarios](./lost_update_completed_scenarios.png)

Итог: финальное значение `101` вместо ожидаемого `102`.

Как избежать:
- атомарные обновления: `UPDATE ... SET value = value + 1`;
- `SELECT ... FOR UPDATE`;
- при необходимости `SERIALIZABLE`.
