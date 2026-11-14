"""Migration parameters and SQL helpers shared by the revision files."""

import re
from typing import Final

from alembic import context

IDENT: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def check_identifier(value: str, label: str) -> str:
    if not IDENT.fullmatch(value):
        raise ValueError(f"{label} must match {IDENT.pattern}: {value!r}")
    return value


def _attribute(name: str) -> str:
    return check_identifier(str(context.config.attributes[name]), name)


def app_role() -> str:
    return _attribute("app_role")


def schema() -> str:
    return _attribute("schema")


def grant(privileges: str, objects: str) -> str:
    return f'GRANT {privileges} ON {objects} TO "{app_role()}"'


def grant_update_columns(table: str, columns: str) -> str:
    return f'GRANT UPDATE ({columns}) ON {table} TO "{app_role()}"'


def revoke(privileges: str, objects: str) -> str:
    return f'REVOKE {privileges} ON {objects} FROM "{app_role()}"'


def pin_search_path(*signatures: str) -> list[str]:
    """ALTER statements that fix a function's search_path, with pg_temp last."""
    path = f"pg_catalog, {schema()}, pg_temp"
    return [f"ALTER FUNCTION {sig} SET search_path = {path}" for sig in signatures]
