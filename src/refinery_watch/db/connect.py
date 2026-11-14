"""Database connection factory."""

import psycopg
from psycopg.rows import DictRow, dict_row

Conn = psycopg.Connection[DictRow]


def connect(database_url: str) -> Conn:
    return psycopg.connect(
        database_url,
        row_factory=dict_row,
        autocommit=False,
        application_name="refinery-watch",
    )
