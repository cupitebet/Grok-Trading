import httpx
import pytest
import respx

from gse import market
from gse.retry import get_with_retry
from gse.x_client import RateLimited, XApiError, XClient

URL = "https://api.x.com/2/tweets"


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


@respx.mock
def test_retries_5xx_with_exponential_backoff_then_succeeds():
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={})])
    sleeps = Sleeps()
    with httpx.Client() as http:
        resp = get_with_retry(http, URL, sleep=sleeps)
    assert resp.status_code == 200 and route.call_count == 3 and sleeps == [1, 2]


@respx.mock
def test_retries_network_errors_and_raises_after_last_attempt():
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    sleeps = Sleeps()
    with httpx.Client() as http, pytest.raises(httpx.ConnectError):
        get_with_retry(http, URL, retries=2, sleep=sleeps)
    assert route.call_count == 3 and sleeps == [1, 2]


@respx.mock
def test_returns_last_5xx_response_when_retries_run_out():
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as http:
        assert get_with_retry(http, URL, retries=1, sleep=Sleeps()).status_code == 500
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("status", [400, 404, 429])
def test_does_not_retry_client_errors_or_rate_limit(status):
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    sleeps = Sleeps()
    with httpx.Client() as http:
        assert get_with_retry(http, URL, sleep=sleeps).status_code == status
    assert route.call_count == 1 and sleeps == []


@respx.mock
def test_x_client_recovers_from_5xx():
    respx.get(URL).mock(side_effect=[httpx.Response(503),
                                     httpx.Response(200, json={"data": [{"id": "1"}]})])
    assert XClient("tok", base_url="https://api.x.com/2", sleep=Sleeps()).lookup_tweets(["1"]) == [{"id": "1"}]


@respx.mock
def test_x_client_persistent_5xx_becomes_xapierror_and_429_stays_ratelimited():
    respx.get(URL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(XApiError, match="HTTP 500"):
        XClient("tok", sleep=Sleeps()).lookup_tweets(["1"])
    respx.get(URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimited):
        XClient("tok", sleep=Sleeps()).lookup_tweets(["1"])


@respx.mock
def test_binance_klines_retry_on_5xx(monkeypatch):
    monkeypatch.setattr("gse.retry.time.sleep", lambda _: None)
    route = respx.get("https://api.binance.com/api/v3/klines").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[])])
    with httpx.Client() as http:
        assert market.fetch_klines(http, "https://api.binance.com", "BTCUSDT", "1d", 0) == []
    assert route.call_count == 2
