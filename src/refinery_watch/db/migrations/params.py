"""Migration parameters and SQL helpers shared by the revision files."""

import re
from typing import Final

from alembic import context

IDENT: Final = re.compile(r"^[a-z_][a-z0-9_]*$")


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


def revoke(privileges: str, objects: str) -> str:
    return f'REVOKE {privileges} ON {objects} FROM "{app_role()}"'
