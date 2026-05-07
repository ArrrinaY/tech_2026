-- 02_non_repeatable_read.sql
-- Выполнять шагами в двух сессиях psql.

-- Перед стартом:
-- \i setup.sql

-- SESSION A
BEGIN;
SET TRANSACTION ISOLATION LEVEL READ COMMITTED;
SELECT balance FROM accounts WHERE id = 2; -- 1000

-- SESSION B
BEGIN;
UPDATE accounts SET balance = balance + 300 WHERE id = 2;
COMMIT;

-- SESSION A
SELECT balance FROM accounts WHERE id = 2; -- 1300
COMMIT;

-- Проверка
SELECT id, owner, balance FROM accounts WHERE id = 2;
