import os
from types import SimpleNamespace

import pytest

from gse.db import connect, migrate


@pytest.fixture
def db():
    """Postgres sungguhan; skema dikosongkan untuk setiap test. Lewati jika URL tidak diset."""
    url = os.environ.get("GSE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("GSE_TEST_DATABASE_URL tidak diset")
    conn = connect(url)
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    migrate(conn)
    yield conn
    conn.close()


def tweet(tid: str, author: str = "u1", text: str = "btc to the moon", created: str = "2026-10-01T00:00:00Z",
          lang: str = "en", referenced=None, likes: int = 1) -> dict:
    t = {"id": tid, "author_id": author, "text": text, "created_at": created, "lang": lang,
         "conversation_id": tid, "public_metrics": {"like_count": likes, "retweet_count": 0, "reply_count": 0,
                                                    "quote_count": 0, "bookmark_count": 0, "impression_count": 10}}
    if referenced:
        t["referenced_tweets"] = referenced
    return t


def user(uid: str = "u1") -> dict:
    return {"id": uid, "username": f"user_{uid}", "name": uid, "created_at": "2020-01-01T00:00:00Z",
            "verified": False, "public_metrics": {"followers_count": 100, "following_count": 50,
                                                  "tweet_count": 1000, "listed_count": 1},
            "profile_image_url": "https://pbs.twimg.com/default_profile_images/x.png"}


class FakeCompletions:
    """Pengganti client.chat.completions; `reply` menerima daftar tweet_id dan mengembalikan JSON string."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.reply(kwargs["messages"][1]["content"])
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(total_tokens=123))


def fake_client(reply) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(reply)))
