"""Storage for positive estimated spreads observed by the monitor."""
import os
import sqlite3
from contextlib import closing, contextmanager

COLUMNS = ("id", "pair", "buy", "sell", "currency", "amount", "started_at",
           "last_at", "duration_ms", "peak_net", "peak_rate", "peak_capacity")
SCHEMA = """
CREATE TABLE IF NOT EXISTS positive_spreads (
    id TEXT PRIMARY KEY,
    pair TEXT NOT NULL,
    buy TEXT NOT NULL,
    sell TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION NOT NULL,
    last_at DOUBLE PRECISION NOT NULL,
    duration_ms INTEGER NOT NULL,
    peak_net DOUBLE PRECISION NOT NULL,
    peak_rate DOUBLE PRECISION NOT NULL,
    peak_capacity DOUBLE PRECISION NOT NULL
)
"""


class History:
    def __init__(self):
        self.url = os.getenv("DATABASE_URL")
        self.path = os.getenv("HISTORY_PATH", "positive_spreads.sqlite3")
        self.label = ("PostgreSQL" if self.url else
                      "暫存（Heroku 未連接 PostgreSQL，重啟會清除）" if os.getenv("DYNO") else
                      "本機 SQLite")

    @contextmanager
    def session(self):
        if self.url:
            import psycopg
            connection = psycopg.connect(self.url, sslmode="require", connect_timeout=5)
        else:
            connection = sqlite3.connect(self.path, timeout=5)
        with closing(connection), connection, closing(connection.cursor()) as cursor:
            yield cursor

    def initialize(self):
        with self.session() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute("CREATE INDEX IF NOT EXISTS spread_last_at ON positive_spreads (last_at)")

    def save(self, events):
        columns = ", ".join(COLUMNS)
        markers = ", ".join(["%s" if self.url else "?"] * len(COLUMNS))
        updates = ", ".join(f"{name}=excluded.{name}" for name in COLUMNS[7:])
        with self.session() as cursor:
            cursor.executemany(
                f"INSERT INTO positive_spreads ({columns}) VALUES ({markers}) "
                f"ON CONFLICT (id) DO UPDATE SET {updates} WHERE excluded.last_at >= positive_spreads.last_at",
                [tuple(event[name] for name in COLUMNS) for event in events],
            )

    def recent(self, limit):
        with self.session() as cursor:
            cursor.execute(f"SELECT {', '.join(COLUMNS)} FROM positive_spreads "
                           f"ORDER BY last_at DESC LIMIT {'%s' if self.url else '?'}", (limit,))
            return [dict(zip(COLUMNS, row)) for row in cursor.fetchall()]
