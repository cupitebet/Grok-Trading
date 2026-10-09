import logging
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from gse.config import Asset
from gse.db import add_usage, get_state, set_state
from gse.x_client import SearchPage, XClient

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    asset: str
    tweets: int
    pages: int
    gap_recorded: bool


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _metrics_row(tweet_id: str, kind: str, pm: dict) -> tuple:
    return (
        tweet_id, kind,
        pm.get("like_count"), pm.get("retweet_count"), pm.get("reply_count"),
        pm.get("quote_count"), pm.get("bookmark_count"), pm.get("impression_count"),
    )


def insert_metrics(conn: psycopg.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO tweet_metrics (tweet_id, kind, like_count, retweet_count, reply_count,"
            " quote_count, bookmark_count, impression_count)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            rows,
        )


def store_page(conn: psycopg.Connection, page: SearchPage, asset: str) -> None:
    with conn.cursor() as cur:
        for u in page.users:
            pm = u.get("public_metrics", {})
            cur.execute(
                "INSERT INTO accounts (author_id, username, name, account_created_at, verified, verified_type)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (author_id) DO UPDATE SET username = EXCLUDED.username, name = EXCLUDED.name,"
                " verified = EXCLUDED.verified, verified_type = EXCLUDED.verified_type, updated_at = now()",
                (u["id"], u["username"], u.get("name"), _ts(u.get("created_at")),
                 u.get("verified"), u.get("verified_type")),
            )
            cur.execute(
                "INSERT INTO account_snapshots (author_id, snapshot_date, followers_count, following_count,"
                " tweet_count, listed_count, description, default_profile_image)"
                " VALUES (%s, (now() AT TIME ZONE 'UTC')::date, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (u["id"], pm.get("followers_count"), pm.get("following_count"), pm.get("tweet_count"),
                 pm.get("listed_count"), u.get("description"),
                 "default_profile_images" in (u.get("profile_image_url") or "")),
            )

        for t in page.context_tweets:
            if not t.get("created_at"):
                continue
            cur.execute(
                "INSERT INTO tweets_raw (tweet_id, author_id, created_at, text, lang, conversation_id,"
                " referenced, is_context, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s)"
                " ON CONFLICT (tweet_id) DO NOTHING",
                (t["id"], t.get("author_id", ""), _ts(t.get("created_at")), t.get("text", ""),
                 t.get("lang"), t.get("conversation_id"), Jsonb(t.get("referenced_tweets", [])), Jsonb(t)),
            )

        for t in page.tweets:
            cur.execute(
                "INSERT INTO tweets_raw (tweet_id, author_id, created_at, text, lang, conversation_id,"
                " referenced, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tweet_id) DO UPDATE SET is_context = false",
                (t["id"], t["author_id"], _ts(t["created_at"]), t["text"], t.get("lang"),
                 t.get("conversation_id"), Jsonb(t.get("referenced_tweets", [])), Jsonb(t)),
            )
            cur.execute(
                "INSERT INTO tweet_assets (tweet_id, asset) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (t["id"], asset),
            )

    insert_metrics(conn, [_metrics_row(t["id"], "t0", t.get("public_metrics", {})) for t in page.tweets])


def ingest_asset(conn: psycopg.Connection, x: XClient, asset: Asset, max_pages: int) -> IngestResult:
    """Ambil tweet baru sejak run terakhir.

    since_id hanya dimajukan setelah semua halaman tersimpan. Kalau run terputus
    (mis. rate limit), run berikutnya mengambil ulang dari since_id lama dan
    duplikat diabaikan, sehingga tidak ada tweet yang hilang.
    """
    key = f"x_since_id:{asset.symbol}"
    since_id = get_state(conn, key)
    newest_id, oldest_id, capped = None, None, False
    total = pages = 0

    for page in x.search_recent(asset.query, since_id=since_id):
        pages += 1
        with conn.transaction():
            store_page(conn, page, asset.symbol)
            add_usage(conn, "x", "posts_read", len(page.tweets))
        total += len(page.tweets)
        newest_id = newest_id or page.newest_id
        oldest_id = page.oldest_id or oldest_id
        if page.has_more and pages >= max_pages:
            capped = True
            break

    gap = capped and since_id is not None
    with conn.transaction():
        if gap:
            conn.execute(
                "INSERT INTO ingest_gaps (asset, after_id, before_id) VALUES (%s, %s, %s)",
                (asset.symbol, since_id, oldest_id),
            )
        if newest_id:
            set_state(conn, key, newest_id)

    if gap:
        log.warning("%s: batas %d halaman tercapai, tweet antara id %s dan %s terlewat",
                    asset.symbol, max_pages, since_id, oldest_id)
    elif capped:
        log.info("%s: run pertama dibatasi %d halaman, histori lebih lama tidak diambil",
                 asset.symbol, max_pages)
    return IngestResult(asset.symbol, total, pages, gap)


def snapshot_t60(conn: psycopg.Connection, x: XClient, delay_minutes: int, limit: int = 1000) -> tuple[int, int]:
    """Ambil metrik tweet yang sudah berumur >= delay_minutes. Return (tersimpan, tidak tersedia)."""
    ids = [r[0] for r in conn.execute(
        "SELECT t.tweet_id FROM tweets_raw t"
        " WHERE NOT t.is_context AND t.deleted_at IS NULL"
        "   AND t.created_at <= now() - make_interval(mins => %s)"
        "   AND NOT EXISTS (SELECT 1 FROM tweet_metrics m WHERE m.tweet_id = t.tweet_id AND m.kind = 't60')"
        " ORDER BY t.created_at LIMIT %s",
        (delay_minutes, limit),
    )]
    stored = unavailable = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        found = x.lookup_tweets(chunk)
        found_ids = {t["id"] for t in found}
        gone = [tid for tid in chunk if tid not in found_ids]
        with conn.transaction():
            insert_metrics(conn, [_metrics_row(t["id"], "t60", t.get("public_metrics", {})) for t in found])
            if gone:
                conn.execute(
                    "UPDATE tweets_raw SET deleted_at = now() WHERE tweet_id = ANY(%s)", (gone,)
                )
            add_usage(conn, "x", "posts_read", len(found))
        stored += len(found)
        unavailable += len(gone)
    return stored, unavailable
