"""Alembic environment. Online only, one transaction per migration."""

from alembic import context
from sqlalchemy import create_engine, pool, text

from refinery_watch.db.migrations.params import check_identifier


def sqlalchemy_url(database_url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg://" + database_url[len(prefix) :]
    return database_url


def run_online() -> None:
    attrs = context.config.attributes
    schema = check_identifier(str(attrs["schema"]), "schema")
    check_identifier(str(attrs["app_role"]), "app_role")
    engine = create_engine(sqlalchemy_url(str(attrs["database_url"])), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f"SET search_path TO {schema}"))
        connection.commit()
        context.configure(
            connection=connection,
            version_table_schema=schema,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
run_online()
