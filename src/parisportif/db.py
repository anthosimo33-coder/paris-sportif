"""Connexion et migrations (fichiers SQL versionnés, appliqués dans l'ordre)."""

from __future__ import annotations

from importlib import resources

import psycopg
from psycopg.rows import TupleRow


def connect(dsn: str) -> psycopg.Connection[TupleRow]:
    conn = psycopg.connect(dsn)
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("parisportif.migrations")
    files = sorted(
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in root.iterdir()
        if entry.name.endswith(".sql")
    )
    return files


def migrate(conn: psycopg.Connection[TupleRow]) -> list[str]:
    """Applique les migrations manquantes, chacune dans sa transaction. Renvoie les appliquées."""
    with conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version     text PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        # Verrou : deux processus qui migrent en même temps ne doivent pas se marcher dessus.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('parisportif:migrate'))")
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migration")}
        applied: list[str] = []
        for name, sql in _migration_files():
            if name in done:
                continue
            with conn.transaction():
                conn.execute(sql.encode())
                conn.execute("INSERT INTO schema_migration (version) VALUES (%s)", (name,))
            applied.append(name)
    return applied
