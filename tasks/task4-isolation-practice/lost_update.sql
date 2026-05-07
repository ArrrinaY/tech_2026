-- 04_lost_update.sql
-- Выполнять шагами в двух сессиях psql.

-- Перед стартом:
-- \i setup.sql

-- SESSION A
BEGIN;
SELECT value FROM counters WHERE id = 1; -- 100
-- Запомнить: планируем записать 101

-- SESSION B
BEGIN;
SELECT value FROM counters WHERE id = 1; -- 100
UPDATE counters SET value = 101 WHERE id = 1;
COMMIT;

-- SESSION A
UPDATE counters SET value = 101 WHERE id = 1;
COMMIT;

-- Проверка (аномалия: итог 101 вместо 102)
SELECT id, name, value FROM counters WHERE id = 1;
