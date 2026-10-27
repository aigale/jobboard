"""Bring an existing database up to date with the models.

New tables are created by `create_all`. For tables that already exist, any column the models
have but the database lacks is added with ALTER TABLE. Scalar defaults (text, numbers, booleans)
are applied to existing rows, so old crawled jobs become origin='crawled', companies stay
approved, and so on. It can also drop a NOT NULL that the models no longer need (Postgres:
ALTER COLUMN ... DROP NOT NULL; SQLite: the table is copied into a fresh one). Renames or type
changes would need a real migration tool such as Alembic.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, literal, text
from sqlalchemy.engine import Engine

log = logging.getLogger("sinag.migrate")


def _default_sql(column, dialect) -> str | None:
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    return str(literal(value, type_=column.type).compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


def add_missing_columns(engine: Engine, metadata) -> list[str]:
    added: list[str] = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                q = engine.dialect.identifier_preparer.quote
                ddl = f'ALTER TABLE {q(table.name)} ADD COLUMN {q(col.name)} {col.type.compile(dialect=engine.dialect)}'
                default = _default_sql(col, engine.dialect)
                if default is not None:
                    ddl += f" DEFAULT {default}"
                    if not col.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    if added:
        log.info("database upgraded: added %s", ", ".join(added))
    return added


def relax_not_null(engine: Engine, metadata) -> list[str]:
    """Columns the models allow to be empty but the database still marks NOT NULL."""
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    todo: dict[str, list[str]] = {}
    for table in metadata.sorted_tables:
        if table.name not in existing:
            continue
        db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        for col in table.columns:
            dc = db_cols.get(col.name)
            if dc is not None and col.nullable and not col.primary_key and not dc["nullable"]:
                todo.setdefault(table.name, []).append(col.name)
    if not todo:
        return []
    q = engine.dialect.identifier_preparer.quote
    done = []
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()
            with conn.begin():
                for name in todo:
                    table = metadata.tables[name]
                    cols = [c["name"] for c in insp.get_columns(name) if c["name"] in table.columns]
                    old = f"_old_{name}"
                    conn.exec_driver_sql(f"ALTER TABLE {q(name)} RENAME TO {q(old)}")
                    for idx in list(table.indexes):           # index names clash with the renamed table's
                        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {q(idx.name)}")
                    table.create(conn)
                    collist = ", ".join(q(c) for c in cols)
                    conn.exec_driver_sql(f"INSERT INTO {q(name)} ({collist}) SELECT {collist} FROM {q(old)}")
                    conn.exec_driver_sql(f"DROP TABLE {q(old)}")
                    done += [f"{name}.{c}" for c in todo[name]]
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()
    else:
        with engine.begin() as conn:
            for name, cols in todo.items():
                for c in cols:
                    conn.execute(text(f"ALTER TABLE {q(name)} ALTER COLUMN {q(c)} DROP NOT NULL"))
                    done.append(f"{name}.{c}")
    log.info("database upgraded: %s may now be empty", ", ".join(done))
    return done
