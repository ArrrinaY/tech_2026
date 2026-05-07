-- 03_phantom_read.sql
-- Выполнять шагами в двух сессиях psql.

-- Перед стартом:
-- \i setup.sql

-- SESSION A
BEGIN;
SET TRANSACTION ISOLATION LEVEL READ COMMITTED;
SELECT COUNT(*) AS cnt FROM orders WHERE amount >= 200; -- 2

-- SESSION B
BEGIN;
INSERT INTO orders (client_id, amount) VALUES (3, 500);
COMMIT;

-- SESSION A
SELECT COUNT(*) AS cnt FROM orders WHERE amount >= 200; -- 3
COMMIT;

-- Проверка
SELECT COUNT(*) AS cnt FROM orders WHERE amount >= 200;
