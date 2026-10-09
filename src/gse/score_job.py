import logging
from itertools import groupby

import psycopg
from psycopg.types.json import Jsonb

from gse.db import add_usage
from gse.grok_scorer import TweetInput, prompt_hash, score_batch

log = logging.getLogger(__name__)

# Batas berlaku per aset (row_number), supaya antrean satu aset tidak menghabiskan jatah aset lain.
PENDING_SQL = """
SELECT tweet_id, asset, lang, text, context FROM (
SELECT ta.tweet_id, ta.asset, t.lang, t.text, ctx.text AS context,
       row_number() OVER (PARTITION BY ta.asset ORDER BY t.created_at DESC) AS rn
FROM tweet_assets ta
JOIN tweets_raw t ON t.tweet_id = ta.tweet_id
LEFT JOIN LATERAL (
    SELECT p.text FROM tweets_raw p
    WHERE p.tweet_id = (
        SELECT r->>'id' FROM jsonb_array_elements(t.referenced) r
        WHERE r->>'type' IN ('replied_to', 'quoted') LIMIT 1
    )
) ctx ON true
LEFT JOIN score_attempts sa
    ON sa.tweet_id = ta.tweet_id AND sa.asset = ta.asset AND sa.prompt_hash = %(ph)s
WHERE t.deleted_at IS NULL
  AND COALESCE(sa.attempts, 0) < %(max_attempts)s
  AND (NOT %(only_golden)s OR EXISTS (
      SELECT 1 FROM golden_labels g WHERE g.tweet_id = ta.tweet_id AND g.asset = ta.asset
  ))
  AND NOT EXISTS (
      SELECT 1 FROM tweet_scores s
      WHERE s.tweet_id = ta.tweet_id AND s.asset = ta.asset
        AND s.model = %(model)s AND s.prompt_hash = %(ph)s
  )
) pending
WHERE rn <= %(limit)s
ORDER BY asset, rn
"""


def score_pending(conn: psycopg.Connection, client, model: str, batch_size: int = 20,
                  limit: int = 200, max_attempts: int = 3, only_golden: bool = False) -> tuple[int, int]:
    """Nilai tweet yang belum punya skor untuk (model, prompt_hash) saat ini, terbaru dulu.

    `limit` berlaku per aset. Return (dinilai, gagal).
    """
    ph = prompt_hash(model)
    rows = conn.execute(PENDING_SQL, {"ph": ph, "model": model, "max_attempts": max_attempts,
                                      "limit": limit, "only_golden": only_golden}).fetchall()
    inputs = [TweetInput(tweet_id=r[0], asset=r[1], lang=r[2], text=r[3], context=r[4]) for r in rows]

    scored = failed = 0
    for asset, group in groupby(inputs, key=lambda t: t.asset):
        group = list(group)
        for i in range(0, len(group), batch_size):
            batch = group[i:i + batch_size]
            # Error API (down, rate limit, kredit habis) dibiarkan naik: run berhenti, batch yang
            # sudah tersimpan tetap ada, dan attempts tidak bertambah karena tweet-nya tidak bersalah.
            result = score_batch(client, model, batch)
            items, missing, tokens = result.items, result.missing_ids, result.tokens
            if missing:
                log.warning("%s: %d tweet tanpa skor valid dari Grok", asset, len(missing))

            with conn.transaction():
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO tweet_scores (tweet_id, asset, model, prompt_hash, sentiment, confidence,"
                        " sarcasm, is_prediction, direction, horizon_days, is_promotional, injection_attempt, raw)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        [(it["tweet_id"], asset, model, ph, float(it["sentiment"]), float(it["confidence"]),
                          bool(it["sarcasm"]), bool(it["is_prediction"]), it["direction"],
                          int(it["horizon_days"]) if it["direction"] != "none" else None,
                          bool(it["is_promotional"]), bool(it["injection_attempt"]), Jsonb(it))
                         for it in items],
                    )
                    cur.executemany(
                        "INSERT INTO score_attempts (tweet_id, asset, prompt_hash, attempts) VALUES (%s, %s, %s, 1)"
                        " ON CONFLICT (tweet_id, asset, prompt_hash)"
                        " DO UPDATE SET attempts = score_attempts.attempts + 1, last_attempt = now()",
                        [(tid, asset, ph) for tid in missing],
                    )
                add_usage(conn, "xai", "requests", 1)
                add_usage(conn, "xai", "tokens", tokens)
            scored += len(items)
            failed += len(missing)
    return scored, failed
