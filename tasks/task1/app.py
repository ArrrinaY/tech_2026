import os
import time
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


DB_CONFIG = {
    "dbname": os.getenv("POSTGRES_DB", "shop"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
    "host": os.getenv("POSTGRES_HOST", "db"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
}


def connect_with_retry(max_attempts: int = 20, delay_seconds: int = 2):
    for attempt in range(1, max_attempts + 1):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            conn.autocommit = False
            return conn
        except psycopg2.OperationalError as exc:
            if attempt == max_attempts:
                raise
            print(f"[connect] attempt {attempt}/{max_attempts} failed: {exc}")
            time.sleep(delay_seconds)
    raise RuntimeError("could not connect to database")


@contextmanager
def transaction(conn):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def place_order_success(conn):
    print("[task1] successful order transaction")
    with transaction(conn) as cur:
        customer_id = 1
        items = [(1, 2), (2, 1)]  # (product_id, qty)

        cur.execute(
            """
            INSERT INTO orders (customer_id, order_date, total_amount)
            VALUES (%s, NOW(), 0)
            RETURNING order_id;
            """,
            (customer_id,),
        )
        order_id = cur.fetchone()["order_id"]

        total_amount = 0
        for product_id, qty in items:
            cur.execute(
                """
                SELECT product_name, price, stock_qty
                FROM products
                WHERE product_id = %s
                FOR UPDATE;
                """,
                (product_id,),
            )
            product = cur.fetchone()
            if not product:
                raise ValueError(f"product {product_id} not found")
            if product["stock_qty"] < qty:
                raise ValueError(
                    f"not enough stock for {product['product_name']}: "
                    f"have {product['stock_qty']}, need {qty}"
                )

            line_total = product["price"] * qty
            total_amount += line_total

            cur.execute(
                """
                INSERT INTO order_items (order_id, product_id, quantity, subtotal)
                VALUES (%s, %s, %s, %s);
                """,
                (order_id, product_id, qty, line_total),
            )
            cur.execute(
                """
                UPDATE products
                SET stock_qty = stock_qty - %s
                WHERE product_id = %s;
                """,
                (qty, product_id),
            )

        cur.execute(
            """
            UPDATE orders
            SET total_amount = %s
            WHERE order_id = %s;
            """,
            (total_amount, order_id),
        )
        print(f"  created order_id={order_id}, total={float(total_amount):.2f}")


def failed_order_with_rollback(conn):
    print("[task2] failed order transaction with rollback")
    try:
        with transaction(conn) as cur:
            customer_id = 2
            impossible_qty = 99_999
            product_id = 1

            cur.execute(
                """
                INSERT INTO orders (customer_id, order_date, total_amount)
                VALUES (%s, NOW(), 0)
                RETURNING order_id;
                """,
                (customer_id,),
            )
            order_id = cur.fetchone()["order_id"]

            cur.execute(
                """
                SELECT product_name, price, stock_qty
                FROM products
                WHERE product_id = %s
                FOR UPDATE;
                """,
                (product_id,),
            )
            product = cur.fetchone()
            if product["stock_qty"] < impossible_qty:
                raise ValueError(
                    f"rollback demo: not enough stock for {product['product_name']} "
                    f"(have {product['stock_qty']}, need {impossible_qty})"
                )

            # This block is not expected to run, but left as complete transaction logic.
            line_total = product["price"] * impossible_qty
            cur.execute(
                """
                INSERT INTO order_items (order_id, product_id, quantity, subtotal)
                VALUES (%s, %s, %s, %s);
                """,
                (order_id, product_id, impossible_qty, line_total),
            )
    except ValueError as exc:
        print(f"  rollback triggered: {exc}")


def transfer_balance_with_savepoint(conn):
    print("[task3] transfer transaction with savepoint")
    with transaction(conn) as cur:
        sender_id = 1
        receiver_id = 2
        transfer_amount = 120

        cur.execute(
            "SELECT balance FROM customers WHERE customer_id = %s FOR UPDATE;",
            (sender_id,),
        )
        sender_balance = cur.fetchone()["balance"]
        if sender_balance < transfer_amount:
            raise ValueError("sender has insufficient funds")

        cur.execute(
            "UPDATE customers SET balance = balance - %s WHERE customer_id = %s;",
            (transfer_amount, sender_id),
        )
        cur.execute("SAVEPOINT after_debit;")

        # Deliberately fail once to demonstrate partial rollback to savepoint.
        try:
            cur.execute(
                "UPDATE customers SET balance = balance + %s WHERE customer_id = %s;",
                (transfer_amount, 9999),
            )
            if cur.rowcount == 0:
                raise ValueError("receiver not found")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT after_debit;")
            cur.execute(
                "UPDATE customers SET balance = balance + %s WHERE customer_id = %s;",
                (transfer_amount, receiver_id),
            )

        print(f"  transferred {transfer_amount} from customer {sender_id} to {receiver_id}")


def print_snapshot(conn):
    print("\n[final state]")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT customer_id, first_name, balance
            FROM customers
            ORDER BY customer_id;
            """
        )
        customers = cur.fetchall()
        print("customers:", customers)

        cur.execute("SELECT COUNT(*) AS orders_count FROM orders;")
        print("orders_count:", cur.fetchone()["orders_count"])

        cur.execute(
            """
            SELECT product_id, product_name, stock_qty
            FROM products
            ORDER BY product_id;
            """
        )
        products = cur.fetchall()
        print("products:", products)


if __name__ == "__main__":
    connection = connect_with_retry()
    try:
        place_order_success(connection)
        failed_order_with_rollback(connection)
        transfer_balance_with_savepoint(connection)
        print_snapshot(connection)
    finally:
        connection.close()