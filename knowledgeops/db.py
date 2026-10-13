from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, url: str):
        self.pool = ConnectionPool(
            url,
            min_size=1,
            max_size=4,
            timeout=3,
            max_waiting=16,
            kwargs={
                "row_factory": dict_row,
                "options": "-c statement_timeout=4000 -c idle_in_transaction_session_timeout=5000",
            },
            open=False,
        )

    def open(self):
        self.pool.open()
        self.pool.wait(timeout=5)

    def close(self):
        self.pool.close()

    @contextmanager
    def connection(self):
        with self.pool.connection() as conn:
            yield conn
