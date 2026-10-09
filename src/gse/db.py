from importlib import resources

import psycopg


def connect(url: str) -> psycopg.Connection:
    # autocommit: setiap `with conn.transaction()` menjadi transaksi nyata yang langsung
    # tersimpan, bukan savepoint di dalam satu transaksi panjang yang hilang saat crash.
    return psycopg.connect(url, autocommit=True)


def migrate(conn: psycopg.Connection) -> list[str]:
    encoding = conn.execute("SHOW server_encoding").fetchone()[0]
    if isinstance(encoding, bytes):  # psycopg mengembalikan bytes pada database SQL_ASCII
        encoding = encoding.decode()
    if encoding.upper() not in ("UTF8", "UTF-8"):
        raise RuntimeError(f"database harus memakai encoding UTF8 (sekarang {encoding}); teks tweet berisi emoji")
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    files = sorted(
        (f for f in resources.files("gse.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied = []
    for f in files:
        if f.name in done:
            continue
        with conn.transaction():
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
        applied.append(f.name)
    return applied


def get_state(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM ingest_state WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO ingest_state (key, value) VALUES (%s, %s)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value),
    )


def add_usage(conn: psycopg.Connection, service: str, metric: str, amount: int) -> None:
    if amount <= 0:
        return
    conn.execute(
        "INSERT INTO api_usage (day, service, metric, amount)"
        " VALUES ((now() AT TIME ZONE 'UTC')::date, %s, %s, %s)"
        " ON CONFLICT (day, service, metric) DO UPDATE SET amount = api_usage.amount + EXCLUDED.amount",
        (service, metric, amount),
    )
