"""Base de test : une base neuve par test, clonée d'un gabarit migré une seule fois.

Nécessite TEST_DATABASE_URL (connexion d'admin à un serveur Postgres 16+), par ex.
    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres
Sans elle, les tests qui touchent la base sont ignorés.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

from parisportif import db

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
TEMPLATE = "parisportif_test_template"
_counter = itertools.count()


def _admin() -> psycopg.Connection[TupleRow]:
    assert ADMIN_URL is not None
    return psycopg.connect(ADMIN_URL, autocommit=True)


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    if ADMIN_URL is None:
        pytest.skip("TEST_DATABASE_URL non défini")
    with _admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEMPLATE}")
    with db.connect(make_conninfo(ADMIN_URL, dbname=TEMPLATE)) as conn:
        db.migrate(conn)
    yield TEMPLATE
    with _admin() as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")


@pytest.fixture
def conn(template_db: str) -> Iterator[psycopg.Connection[TupleRow]]:
    assert ADMIN_URL is not None
    name = f"parisportif_test_{os.getpid()}_{next(_counter)}"
    with _admin() as admin:
        admin.execute(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    connection = db.connect(make_conninfo(ADMIN_URL, dbname=name))
    try:
        yield connection
    finally:
        connection.close()
        with _admin() as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
