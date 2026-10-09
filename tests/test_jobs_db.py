import csv
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from conftest import fake_client, tweet, user

from gse import golden, market, status
from gse.config import Asset
from gse.db import get_state
from gse.ingest_x import ingest_asset, snapshot_t60
from gse.score_job import score_pending
from gse.x_client import RateLimited, SearchPage

ASSET = Asset("BTC", "BTCUSDT", "bitcoin")


def page(tweets, users=(), context=(), has_more=False):
    ids = [t["id"] for t in tweets]
    return SearchPage(list(tweets), list(users), list(context),
                      newest_id=max(ids, key=int) if ids else None,
                      oldest_id=min(ids, key=int) if ids else None, has_more=has_more)


class FakeX:
    def __init__(self, pages=(), lookup=(), fail_after=None):
        self.pages, self.lookup = list(pages), {t["id"]: t for t in lookup}
        self.fail_after, self.since_calls = fail_after, []

    def search_recent(self, query, since_id=None):
        self.since_calls.append(since_id)
        for i, p in enumerate(self.pages):
            if self.fail_after is not None and i >= self.fail_after:
                raise RateLimited(None)
            yield p

    def lookup_tweets(self, ids):
        return [self.lookup[i] for i in ids if i in self.lookup]


def count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


def test_ingest_stores_everything_and_is_idempotent(db):
    reply = tweet("11", text="this is the bottom", referenced=[{"type": "replied_to", "id": "9"}])
    p = page([tweet("10"), reply], users=[user("u1")], context=[tweet("9", author="u2", text="btc dead")])
    x = FakeX([p])

    r = ingest_asset(db, x, ASSET, max_pages=5)
    ingest_asset(db, x, ASSET, max_pages=5)

    assert r.tweets == 2 and not r.gap_recorded
    assert x.since_calls == [None, "11"]
    assert get_state(db, "x_since_id:BTC") == "11"
    assert count(db, "SELECT count(*) FROM tweets_raw") == 3
    assert count(db, "SELECT count(*) FROM tweets_raw WHERE is_context") == 1
    assert count(db, "SELECT count(*) FROM tweet_assets") == 2
    assert count(db, "SELECT count(*) FROM tweet_metrics WHERE kind = 't0'") == 2
    assert count(db, "SELECT count(*) FROM account_snapshots WHERE default_profile_image") == 1
    assert count(db, "SELECT amount FROM api_usage WHERE service = 'x'") == 4


def test_rate_limit_midway_keeps_old_since_id(db):
    x = FakeX([page([tweet("5")], has_more=True), page([tweet("4")])], fail_after=1)
    with pytest.raises(RateLimited):
        ingest_asset(db, x, ASSET, max_pages=5)
    assert count(db, "SELECT count(*) FROM tweets_raw") == 1
    assert get_state(db, "x_since_id:BTC") is None


def test_page_cap_records_gap_only_after_first_run(db):
    ingest_asset(db, FakeX([page([tweet("100")], has_more=True)]), ASSET, max_pages=1)
    assert count(db, "SELECT count(*) FROM ingest_gaps") == 0

    r = ingest_asset(db, FakeX([page([tweet("300"), tweet("250")], has_more=True)]), ASSET, max_pages=1)
    assert r.gap_recorded
    assert db.execute("SELECT after_id, before_id FROM ingest_gaps").fetchone() == ("100", "250")
    assert get_state(db, "x_since_id:BTC") == "300"


def test_context_tweet_becomes_match_when_found_by_query(db):
    ingest_asset(db, FakeX([page([tweet("2", referenced=[{"type": "quoted", "id": "1"}])],
                                 context=[tweet("1")])]), ASSET, 5)
    ingest_asset(db, FakeX([page([tweet("1")])]), ASSET, 5)
    assert count(db, "SELECT count(*) FROM tweets_raw WHERE is_context") == 0
    assert count(db, "SELECT count(*) FROM tweet_assets") == 2


def test_snapshot_t60_stores_metrics_and_marks_unavailable(db):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    ingest_asset(db, FakeX([page([tweet("1", created=old), tweet("2", created=old),
                                  tweet("3", created=fresh)])]), ASSET, 5)
    x = FakeX(lookup=[tweet("1", likes=50)])

    assert snapshot_t60(db, x, delay_minutes=60) == (1, 1)
    assert count(db, "SELECT like_count FROM tweet_metrics WHERE tweet_id = '1' AND kind = 't60'") == 50
    assert count(db, "SELECT count(*) FROM tweets_raw WHERE deleted_at IS NOT NULL") == 1
    assert snapshot_t60(db, x, delay_minutes=60) == (0, 0)


def scorer_reply(valid_ids, sentiment=lambda tid: 0.5):
    def reply(prompt):
        ids = re.findall(r'<tweet id="(\d+)"', prompt)
        return json.dumps({"items": [
            {"tweet_id": i, "sentiment": sentiment(i), "confidence": 0.9, "sarcasm": False,
             "is_prediction": False, "direction": "none", "horizon_days": 0,
             "is_promotional": False, "injection_attempt": False}
            for i in ids if i in valid_ids]})
    return reply


def test_score_job_stores_scores_and_retries_missing_up_to_limit(db):
    reply = tweet("2", referenced=[{"type": "replied_to", "id": "1"}])
    ingest_asset(db, FakeX([page([reply, tweet("3")], context=[tweet("1", text="PARENT TEXT")])]), ASSET, 5)
    client = fake_client(scorer_reply({"2"}))

    assert score_pending(db, client, "m") == (1, 1)
    assert "PARENT TEXT" in client.chat.completions.calls[0]["messages"][1]["content"]
    assert db.execute("SELECT horizon_days, sentiment FROM tweet_scores").fetchone() == (None, 0.5)

    assert score_pending(db, client, "m") == (0, 1)
    assert score_pending(db, client, "m") == (0, 1)
    assert score_pending(db, client, "m") == (0, 0)          # tweet 3 sudah 3x gagal
    assert count(db, "SELECT attempts FROM score_attempts WHERE tweet_id = '3'") == 3
    assert count(db, "SELECT amount FROM api_usage WHERE metric = 'tokens'") == 123 * 3


def test_score_job_limit_is_per_asset(db):
    ingest_asset(db, FakeX([page([tweet("1"), tweet("2"), tweet("3")])]), ASSET, 5)
    ingest_asset(db, FakeX([page([tweet("4")])]), Asset("ETH", "ETHUSDT", "eth"), 5)
    client = fake_client(scorer_reply({"1", "2", "3", "4"}))

    assert score_pending(db, client, "m", limit=2) == (3, 0)
    assert db.execute("SELECT asset, count(*) FROM tweet_scores GROUP BY asset ORDER BY asset").fetchall() == [
        ("BTC", 2), ("ETH", 1)]


def test_score_job_api_error_does_not_burn_attempts(db):
    ingest_asset(db, FakeX([page([tweet("1")])]), ASSET, 5)
    client = fake_client(lambda _: httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        score_pending(db, client, "m")
    assert count(db, "SELECT count(*) FROM score_attempts") == 0


def test_golden_export_import_and_evaluate(db, tmp_path):
    ingest_asset(db, FakeX([page([tweet(str(i), text=f"t{i}") for i in range(1, 6)])]), ASSET, 5)
    out = tmp_path / "golden.csv"
    assert golden.export_sample(db, out, n=10, labeler="ana") == 5

    with out.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert set(rows[0]) == set(golden.EXPORT_COLUMNS) and rows[0]["label"] == ""
    for r in rows:
        r["label"] = str((int(r["tweet_id"]) - 3) / 2).replace(".", ",")   # koma desimal ala Excel ID
    rows[0]["label"] = ""
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=golden.EXPORT_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    res = golden.import_labels(db, out, "ana")
    assert (res.imported, res.skipped_empty, res.errors) == (4, 1, [])
    assert golden.export_sample(db, out, n=10, labeler="ana") == 1

    score_pending(db, fake_client(scorer_reply({"1", "2", "3", "4", "5"}, lambda i: int(i) / 10)), "m",
                  only_golden=True)
    ev = golden.evaluate(db, "m")
    assert ev.labeled == 4 and ev.scored == 4 and ev.spearman == pytest.approx(1.0)
    assert count(db, "SELECT count(*) FROM tweet_scores") == 4


def test_status_gate1_detects_gaps(db):
    now = datetime.now(timezone.utc)
    steady = [tweet(str(i), created=(now - timedelta(minutes=30 * i)).isoformat()) for i in range(1, 15 * 48)]
    ingest_asset(db, FakeX([page(steady)]), ASSET, 5)
    [c] = status.coverage(db, "m", days=16)
    assert c.days_covered > 14 and c.max_gap == timedelta(minutes=30) and c.gate1_ok

    db.execute("INSERT INTO ingest_gaps (asset, after_id, before_id) VALUES ('BTC', '1', '2')")
    [c] = status.coverage(db, "m", days=16)
    assert c.recorded_gaps == 1 and c.gate1_ok        # sampel terpotong tidak menggagalkan gate

    db.execute("DELETE FROM tweet_assets WHERE tweet_id IN ('100', '101', '102')")
    [c] = status.coverage(db, "m", days=16)
    assert c.max_gap == timedelta(hours=2) and not c.gate1_ok


@respx.mock
def test_sync_bars_paginates_and_skips_open_bar(db, monkeypatch):
    monkeypatch.setattr(market, "KLINE_LIMIT", 2)
    day = market.INTERVAL_MS["1d"]
    start = market.EARLIEST_MS

    def k(i):
        return [start + i * day, "1", "2", "0.5", "1.5", "10", start + (i + 1) * day - 1]

    route = respx.get("https://api.binance.com/api/v3/klines").mock(side_effect=[
        httpx.Response(200, json=[k(0), k(1)]),
        httpx.Response(200, json=[k(2), k(3)]),        # k(3) belum tutup
    ])
    now_ms = start + 3 * day + 1000
    with httpx.Client() as http:
        assert market.sync_bars(db, http, "https://api.binance.com", "BTCUSDT", "1d", now_ms=now_ms) == 3
    assert route.calls[1].request.url.params["startTime"] == str(start + 2 * day)

    respx.get("https://api.binance.com/api/v3/klines").mock(return_value=httpx.Response(200, json=[k(3)]))
    with httpx.Client() as http:
        n = market.sync_bars(db, http, "https://api.binance.com", "BTCUSDT", "1d", now_ms=start + 4 * day + 1)
    assert n == 1 and count(db, "SELECT count(*) FROM bars") == 4
