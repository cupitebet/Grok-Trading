from dataclasses import dataclass
from datetime import timedelta

import psycopg

from gse.grok_scorer import prompt_hash

MAX_GAP = timedelta(hours=1)
MIN_DAYS = 14


@dataclass
class AssetCoverage:
    asset: str
    tweets_window: int
    tweets_24h: int
    first_seen: object
    latest: object
    max_gap: timedelta | None
    since_latest: timedelta | None
    recorded_gaps: int
    scored_window: int

    @property
    def days_covered(self) -> float:
        if not self.first_seen or not self.latest:
            return 0.0
        return (self.latest - self.first_seen).total_seconds() / 86400

    @property
    def gate1_ok(self) -> bool:
        """Gate 1 mengukur gangguan pengumpulan (jeda waktu), bukan kelengkapan sampel.

        recorded_gaps (tweet terlewat karena batas halaman) sengaja tidak menggagalkan gate:
        dengan kuota terbatas, query bervolume tinggi memang hanya bisa disampel.
        """
        worst = max((g for g in (self.max_gap, self.since_latest) if g is not None), default=None)
        return self.days_covered >= MIN_DAYS and worst is not None and worst <= MAX_GAP


def coverage(conn: psycopg.Connection, model: str, days: int = MIN_DAYS) -> list[AssetCoverage]:
    """Cakupan pengumpulan per aset dalam `days` hari terakhir.

    max_gap adalah jeda terpanjang antar tweet berurutan. Untuk aset ramai seperti BTC,
    jeda > 1 jam hampir pasti berarti pengumpulan sempat berhenti.
    """
    rows = conn.execute(
        """
        WITH w AS (
            SELECT ta.asset, t.tweet_id, t.created_at,
                   t.created_at - lag(t.created_at) OVER (PARTITION BY ta.asset ORDER BY t.created_at) AS gap
            FROM tweet_assets ta JOIN tweets_raw t ON t.tweet_id = ta.tweet_id
            WHERE t.created_at > now() - make_interval(days => %(days)s)
        )
        SELECT w.asset,
               count(*),
               count(*) FILTER (WHERE w.created_at > now() - interval '24 hours'),
               min(w.created_at), max(w.created_at), max(w.gap),
               now() - max(w.created_at),
               (SELECT count(*) FROM ingest_gaps g
                 WHERE g.asset = w.asset AND g.recorded_at > now() - make_interval(days => %(days)s)),
               count(s.tweet_id)
        FROM w
        LEFT JOIN tweet_scores s ON s.tweet_id = w.tweet_id AND s.asset = w.asset
                                AND s.model = %(model)s AND s.prompt_hash = %(ph)s
        GROUP BY w.asset ORDER BY w.asset
        """,
        {"days": days, "model": model, "ph": prompt_hash(model)},
    ).fetchall()
    return [AssetCoverage(*r) for r in rows]


def usage(conn: psycopg.Connection, days: int = 30) -> list[tuple]:
    return conn.execute(
        "SELECT service, metric,"
        " sum(amount) FILTER (WHERE day = (now() AT TIME ZONE 'UTC')::date),"
        " sum(amount)"
        " FROM api_usage WHERE day > (now() AT TIME ZONE 'UTC')::date - %s"
        " GROUP BY service, metric ORDER BY service, metric",
        (days,),
    ).fetchall()
