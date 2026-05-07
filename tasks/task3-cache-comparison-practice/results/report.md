# Отчет по практике: сравнение типов кеширования

## Что реализовано

- `Lazy Loading / Cache-Aside / Write-Around`
- `Write-Through`
- `Write-Back`

Одна и та же система протестирована в трех вариантах с одинаковыми параметрами.

## Описание тестов

- Набор данных: 400 ключей (`user:1` ... `user:400`)
- Число запросов в каждом прогоне: 5000
- Профили нагрузки:
  - `read-heavy`: 80% read / 20% write
  - `balanced`: 50% read / 50% write
  - `write-heavy`: 20% read / 80% write
- Компоненты:
  - `application` (Python)
  - `cache` (in-memory simulation)
  - `DB` (simulated storage with read/write latency)
  - `load-generator` (встроен в скрипт)

## Таблица результатов

| Стратегия | Нагрузка | Throughput (req/sec) | Средняя задержка (ms) | Обращения в БД | Cache hit rate | Очередь Write-Back |
|---|---|---:|---:|---:|---:|---:|
| lazy-loading | read-heavy | 1392.3 | 0.717 | 1403 | 89.99% | 0 |
| write-through | read-heavy | 1444.7 | 0.691 | 1323 | 91.99% | 0 |
| write-back | read-heavy | 1579.3 | 0.596 | 1226 | 91.99% | 80 |
| lazy-loading | balanced | 643.2 | 1.553 | 2893 | 84.15% | 0 |
| write-through | balanced | 674.9 | 1.480 | 2700 | 91.85% | 0 |
| write-back | balanced | 747.8 | 1.292 | 2443 | 91.85% | 80 |
| lazy-loading | write-heavy | 419.6 | 2.380 | 4349 | 63.89% | 0 |
| write-through | write-heavy | 441.9 | 2.260 | 4057 | 92.54% | 0 |
| write-back | write-heavy | 494.6 | 2.005 | 3626 | 92.54% | 80 |

## Что происходит при накоплении записей в Write-Back

В `Write-Back` запись сразу попадает в кеш, а в БД уходит пачками.  
В замерах максимальный размер очереди отложенных записей: `80`.

## Выводы

- Для чтения (`read-heavy`) лучший результат у `Write-Back`.
- Для записи (`write-heavy`) лучший результат у `Write-Back`.
- Для смешанной нагрузки (`balanced`) лучший результат у `Write-Back`.

## Скрины/логи консоли

Лог выполнения тестов сохранен в файле:

- `tasks/task3/results/console-log.txt`
