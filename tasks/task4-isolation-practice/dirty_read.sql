-- 01_dirty_read.sql
-- PostgreSQL: dirty read не воспроизводится (READ UNCOMMITTED ~= READ COMMITTED)
-- Выполнять шагами в двух сессиях psql.

-- Перед стартом:
-- \i setup.sql

-- SESSION A
BEGIN;
UPDATE accounts SET balance = balance - 500 WHERE id = 1;
-- Не делай COMMIT

-- SESSION B
BEGIN;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT id, owner, balance FROM accounts WHERE id = 1;
COMMIT;

-- SESSION A
ROLLBACK;

-- Проверка
SELECT id, owner, balance FROM accounts WHERE id = 1;
