"""Database connection factory."""

import psycopg
from psycopg import errors
from psycopg.rows import DictRow, dict_row

Conn = psycopg.Connection[DictRow]

_AUTH_STATES = frozenset({"28000", "28P01"})


def _category(exc: psycopg.Error) -> str:
    if isinstance(exc, errors.QueryCanceled) or exc.sqlstate == "57014":
        return "timeout"
    if exc.sqlstate in _AUTH_STATES:
        return "authentication failed"
    text = str(exc).lower()
    if "password authentication failed" in text or "authentication failed" in text:
        return "authentication failed"
    if "timeout expired" in text or "timed out" in text:
        return "timeout"
    if "connection refused" in text:
        return "refused"
    return "other"


def connect(database_url: str) -> Conn:
    try:
        return psycopg.connect(
            database_url,
            row_factory=dict_row,
            autocommit=False,
            application_name="refinery-watch",
        )
    except psycopg.Error as exc:
        raise psycopg.OperationalError(
            f"could not connect to the database: {_category(exc)}"
        ) from None
