import logging
import time

import httpx

log = logging.getLogger(__name__)


def get_with_retry(http: httpx.Client, url: str, *, params: dict | None = None, headers: dict | None = None,
                   retries: int = 3, sleep=None) -> httpx.Response:
    """GET dengan backoff eksponensial (1s, 2s, 4s) untuk gangguan sementara.

    Yang diulang: error jaringan/timeout dan HTTP 5xx. Status lain (termasuk 429 dan 4xx) langsung
    dikembalikan: rate limit punya penanganan sendiri, dan 4xx tidak akan berubah kalau diulang.
    Setelah percobaan terakhir, error jaringan dilempar dan respons 5xx dikembalikan ke pemanggil.
    """
    if sleep is None:
        sleep = time.sleep
    for attempt in range(retries + 1):
        try:
            resp = http.get(url, params=params, headers=headers)
        except httpx.TransportError as e:
            if attempt == retries:
                raise
            reason = repr(e)
        else:
            if resp.status_code < 500 or attempt == retries:
                return resp
            reason = f"HTTP {resp.status_code}"
        delay = 2 ** attempt
        log.warning("GET %s gagal (%s); ulangi dalam %ds (%d/%d)", url, reason, delay, attempt + 1, retries)
        sleep(delay)
    raise AssertionError("tidak terjangkau")
