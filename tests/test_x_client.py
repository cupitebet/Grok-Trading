import httpx
import pytest
import respx

from gse.x_client import RateLimited, XApiError, XClient

BASE = "https://api.x.com/2"


@respx.mock
def test_search_paginates_with_since_id_and_next_token():
    route = respx.get(f"{BASE}/tweets/search/recent").mock(side_effect=[
        httpx.Response(200, json={"data": [{"id": "3"}, {"id": "2"}],
                                  "includes": {"users": [{"id": "u1"}], "tweets": [{"id": "9"}]},
                                  "meta": {"newest_id": "3", "oldest_id": "2", "next_token": "abc"}}),
        httpx.Response(200, json={"data": [{"id": "1"}], "meta": {"newest_id": "1", "oldest_id": "1"}}),
    ])
    pages = list(XClient("tok").search_recent("q", since_id="0"))

    assert [len(p.tweets) for p in pages] == [2, 1]
    assert [p.has_more for p in pages] == [True, False]
    assert pages[0].users == [{"id": "u1"}] and pages[0].context_tweets == [{"id": "9"}]
    first, second = (c.request.url.params for c in route.calls)
    assert first["since_id"] == "0" and "next_token" not in first
    assert second["next_token"] == "abc"
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_search_empty_result():
    respx.get(f"{BASE}/tweets/search/recent").mock(
        return_value=httpx.Response(200, json={"meta": {"result_count": 0}}))
    pages = list(XClient("tok").search_recent("q"))
    assert len(pages) == 1 and pages[0].tweets == [] and pages[0].newest_id is None


@respx.mock
def test_rate_limit_raises_with_reset_time():
    respx.get(f"{BASE}/tweets/search/recent").mock(
        return_value=httpx.Response(429, headers={"x-rate-limit-reset": "1791533730"}))
    with pytest.raises(RateLimited) as exc:
        next(XClient("tok").search_recent("q"))
    assert exc.value.reset_at.year == 2026


@respx.mock
def test_lookup_returns_found_tweets_only():
    respx.get(f"{BASE}/tweets").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "1", "public_metrics": {"like_count": 5}}],
        "errors": [{"resource_id": "2", "title": "Not Found Error"}],
    }))
    assert [t["id"] for t in XClient("tok").lookup_tweets(["1", "2"])] == ["1"]


@respx.mock
def test_client_error_includes_response_body():
    respx.get(f"{BASE}/tweets/search/recent").mock(
        return_value=httpx.Response(400, text='{"detail":"operator $ not available"}'))
    with pytest.raises(XApiError, match="operator"):
        next(XClient("tok").search_recent("$BTC"))


def test_lookup_rejects_bad_batch_size():
    with pytest.raises(ValueError):
        XClient("tok").lookup_tweets([str(i) for i in range(101)])
