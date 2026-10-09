from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import psycopg

from gse.db import add_usage

INTERVAL_MS = {"1h": 3_600_000, "1d": 86_400_000}
# Hari pertama Binance spot; histori sebelum ini tidak ada.
EARLIEST_MS = 1_502_928_000_000
KLINE_LIMIT = 1000


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def parse_kline(k: list) -> dict:
    return {
        "open_time": _ms_to_dt(k[0]),
        "open": Decimal(k[1]), "high": Decimal(k[2]), "low": Decimal(k[3]), "close": Decimal(k[4]),
        "volume": Decimal(k[5]),
        "close_time": _ms_to_dt(k[6]),
    }


def fetch_klines(http: httpx.Client, base_url: str, symbol: str, interval: str, start_ms: int) -> list[list]:
    resp = http.get(
        f"{base_url.rstrip('/')}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "startTime": start_ms, "limit": KLINE_LIMIT},
    )
    resp.raise_for_status()
    return resp.json()


def sync_bars(conn: psycopg.Connection, http: httpx.Client, base_url: str, symbol: str, interval: str,
              now_ms: int | None = None) -> int:
    """Lanjutkan dari bar terakhir yang tersimpan. Bar yang belum tutup tidak disimpan."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"interval {interval} tidak didukung")
    step = INTERVAL_MS[interval]
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)

    last = conn.execute(
        "SELECT max(open_time) FROM bars WHERE symbol = %s AND interval = %s", (symbol, interval)
    ).fetchone()[0]
    start_ms = int(last.timestamp() * 1000) + step if last else EARLIEST_MS

    stored = 0
    while start_ms < now_ms:
        rows = fetch_klines(http, base_url, symbol, interval, start_ms)
        closed = [parse_kline(k) for k in rows if k[6] < now_ms]
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO bars (symbol, interval, open_time, close_time, open, high, low, close, volume)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    [(symbol, interval, b["open_time"], b["close_time"], b["open"], b["high"], b["low"],
                      b["close"], b["volume"]) for b in closed],
                )
            add_usage(conn, "binance", "requests", 1)
        stored += len(closed)
        if len(rows) < KLINE_LIMIT or not closed:
            break
        start_ms = rows[-1][0] + step
    return stored


FNG_URL = "https://api.alternative.me/fng/"


def parse_fear_greed(body: dict) -> list[tuple[date, int, str]]:
    return [
        (datetime.fromtimestamp(int(d["timestamp"]), timezone.utc).date(), int(d["value"]),
         d["value_classification"])
        for d in body.get("data", [])
    ]


def sync_fear_greed(conn: psycopg.Connection, http: httpx.Client, url: str = FNG_URL) -> int:
    """Ambil seluruh histori (limit=0). Nilai yang sudah ada diperbarui jika berubah."""
    resp = http.get(url, params={"limit": 0, "format": "json"})
    resp.raise_for_status()
    rows = parse_fear_greed(resp.json())
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO fear_greed (day, value, classification) VALUES (%s, %s, %s)"
                " ON CONFLICT (day) DO UPDATE SET value = EXCLUDED.value,"
                " classification = EXCLUDED.classification",
                rows,
            )
        add_usage(conn, "alternative_me", "requests", 1)
    return len(rows)
