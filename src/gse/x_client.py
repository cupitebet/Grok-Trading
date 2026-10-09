from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

TWEET_FIELDS = "author_id,created_at,lang,conversation_id,referenced_tweets,public_metrics"
USER_FIELDS = "created_at,public_metrics,verified,verified_type,description,profile_image_url,name,username"


class XApiError(Exception):
    """Error 4xx/5xx dari X API, dengan potongan isi respons (mis. alasan query ditolak)."""


class RateLimited(Exception):
    def __init__(self, reset_at: datetime | None):
        super().__init__(f"X API rate limit, reset pada {reset_at}")
        self.reset_at = reset_at


@dataclass
class SearchPage:
    tweets: list[dict]
    users: list[dict]
    context_tweets: list[dict]
    newest_id: str | None
    oldest_id: str | None
    has_more: bool


class XClient:
    def __init__(self, bearer_token: str, http: httpx.Client | None = None,
                 base_url: str = "https://api.x.com/2"):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {bearer_token}"}

    def _get(self, path: str, params: dict) -> dict:
        resp = self.http.get(f"{self.base_url}{path}", params=params, headers=self.headers)
        if resp.status_code == 429:
            reset = resp.headers.get("x-rate-limit-reset")
            raise RateLimited(datetime.fromtimestamp(int(reset), timezone.utc) if reset else None)
        if resp.status_code >= 400:
            raise XApiError(f"HTTP {resp.status_code} dari {path}: {resp.text[:300]}")
        return resp.json()

    def search_recent(self, query: str, since_id: str | None = None):
        """Yield halaman hasil, terbaru dulu. Pemanggil yang menentukan kapan berhenti."""
        params = {
            "query": query,
            "max_results": 100,
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id,referenced_tweets.id",
            "user.fields": USER_FIELDS,
        }
        if since_id:
            params["since_id"] = since_id
        while True:
            body = self._get("/tweets/search/recent", params)
            meta = body.get("meta", {})
            includes = body.get("includes", {})
            token = meta.get("next_token")
            yield SearchPage(
                tweets=body.get("data", []),
                users=includes.get("users", []),
                context_tweets=includes.get("tweets", []),
                newest_id=meta.get("newest_id"),
                oldest_id=meta.get("oldest_id"),
                has_more=bool(token),
            )
            if not token:
                return
            params["next_token"] = token

    def lookup_tweets(self, ids: list[str]) -> list[dict]:
        """Tweet yang dihapus, diproteksi, atau akunnya di-suspend tidak ikut dikembalikan."""
        if not 0 < len(ids) <= 100:
            raise ValueError("lookup_tweets menerima 1-100 id")
        body = self._get("/tweets", {"ids": ",".join(ids), "tweet.fields": "public_metrics"})
        return body.get("data", [])
