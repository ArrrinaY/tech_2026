-- setup.sql
-- запускать перед демонстрацией любой аномалии

DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS counters;

CREATE TABLE accounts (
    id      INT PRIMARY KEY,
    owner   TEXT NOT NULL,
    balance INT NOT NULL
);

INSERT INTO accounts (id, owner, balance) VALUES
(1, 'Alice', 1000),
(2, 'Bob',   1000);

CREATE TABLE orders (
    id         SERIAL PRIMARY KEY,
    client_id  INT NOT NULL,
    amount     INT NOT NULL
);

INSERT INTO orders (client_id, amount) VALUES
(1, 100),
(1, 200),
(2, 300);

CREATE TABLE products (
    id    INT PRIMARY KEY,
    name  TEXT NOT NULL,
    stock INT NOT NULL
);

INSERT INTO products (id, name, stock) VALUES
(1, 'Keyboard', 10);

CREATE TABLE counters (
    id    INT PRIMARY KEY,
    name  TEXT NOT NULL,
    value INT NOT NULL
);

INSERT INTO counters (id, name, value) VALUES
(1, 'page_views', 100);

SELECT 'setup done' AS status;
