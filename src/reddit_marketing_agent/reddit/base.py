"""Backend interface, errors, and a polite HTTP session shared by both backends."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx


class RedditError(RuntimeError):
    """Something went wrong talking to Reddit."""


class RateLimited(RedditError):
    """Reddit asked us to slow down and the wait would be too long to block on."""


class NotSupported(RedditError):
    """The active backend can't do this (usually: needs API credentials)."""


SORTS = {"hot", "new", "top", "rising", "controversial"}
SEARCH_SORTS = {"relevance", "hot", "top", "new", "comments"}
TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}


def check_choice(value: str, allowed: set[str], label: str) -> str:
    value = value.lower().strip()
    if value not in allowed:
        raise ValueError(f"{label} must be one of {sorted(allowed)}, got {value!r}")
    return value


class PoliteSession:
    """An httpx client that respects Reddit's ``x-ratelimit-*`` headers and caches GETs.

    Reddit reports how many requests are left in the current window and when it resets. When the
    budget runs out we sleep until the reset (if that's short) instead of hammering the API, and
    we keep a small TTL cache so repeated tool calls don't spend requests.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 20.0,
        min_interval: float = 0.0,
        max_wait: float = 65.0,
        cache_ttl: int = 600,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self.min_interval = min_interval
        self.max_wait = max_wait
        self.cache_ttl = cache_ttl
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._cache: dict[str, tuple[float, httpx.Response]] = {}
        self.remaining: float | None = None

    def close(self) -> None:
        self.client.close()

    # -- pacing ---------------------------------------------------------------------------------
    def _wait_turn(self) -> None:
        delay = self._next_allowed - self._clock()
        if delay > self.max_wait:
            raise RateLimited(
                f"Reddit rate limit reached; next request allowed in ~{int(delay)}s. "
                "Try again shortly (adding Reddit API credentials raises the limit a lot)."
            )
        if delay > 0:
            self._sleep(delay)

    def _record_limits(self, resp: httpx.Response) -> None:
        now = self._clock()
        next_allowed = now + self.min_interval
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining is not None:
            try:
                self.remaining = float(remaining)
            except ValueError:
                self.remaining = None
        if self.remaining is not None and self.remaining < 1 and reset:
            try:
                next_allowed = max(next_allowed, now + float(reset) + 0.5)
            except ValueError:
                pass
        self._next_allowed = next_allowed

    # -- requests -------------------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        use_cache: bool = True,
    ) -> httpx.Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cache_key = None
        if method == "GET" and use_cache and self.cache_ttl > 0:
            cache_key = str(httpx.URL(url, params=params))
            hit = self._cache.get(cache_key)
            if hit and self._clock() - hit[0] < self.cache_ttl:
                return hit[1]

        with self._lock:
            for attempt in range(2):
                self._wait_turn()
                try:
                    resp = self.client.request(method, url, params=params, data=data, headers=headers, auth=auth)
                except httpx.HTTPError as exc:
                    raise RedditError(f"Network error talking to Reddit: {exc}") from exc
                self._record_limits(resp)
                if resp.status_code == 429 and attempt == 0:
                    retry_after = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset")
                    wait = float(retry_after) if retry_after else 10.0
                    self._next_allowed = max(self._next_allowed, self._clock() + wait + 0.5)
                    continue
                break

        if resp.status_code == 429:
            raise RateLimited(
                "Reddit is rate limiting this connection (HTTP 429). Wait a minute and retry; "
                "RSS mode allows very few requests per minute, API credentials allow far more."
            )
        if resp.status_code == 403 and "blocked by network security" in resp.text[:5000]:
            raise RedditError(
                "Reddit blocked this unauthenticated request. Configure Reddit API credentials "
                "(see README) to use the official API."
            )
        if cache_key and resp.status_code == 200:
            self._cache[cache_key] = (self._clock(), resp)
        return resp


class RedditBackend:
    """Interface implemented by :class:`OAuthBackend` and :class:`RSSBackend`.

    All methods return normalized dicts from :mod:`.models`.
    """

    name = "base"
    can_write = False
    has_metrics = False  # scores / comment counts available?

    def capabilities(self) -> dict[str, bool]:
        return {
            "search": True,
            "browse": True,
            "threads": True,
            "subreddit_search": True,
            "subreddit_rules": False,
            "subreddit_stats": False,
            "metrics": self.has_metrics,
            "inbox": False,
            "account": False,
            "write": self.can_write,
        }

    def _unsupported(self, what: str) -> NotSupported:
        return NotSupported(
            f"{what} needs Reddit API credentials (current mode: {self.name}). "
            "See the README section 'Getting Reddit API access'."
        )

    def search_subreddits(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        raise self._unsupported("Subreddit search")

    def subreddit_about(self, name: str) -> dict[str, Any]:
        raise self._unsupported("Subreddit details")

    def subreddit_rules(self, name: str) -> list[dict[str, Any]]:
        raise self._unsupported("Subreddit rules")

    def listing(
        self, subreddit: str, sort: str = "hot", time_filter: str = "day", limit: int = 25
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "month",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def thread(
        self, post_id: str, comment_limit: int = 50, sort: str = "top"
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raise NotImplementedError

    def info(self, fullnames: list[str]) -> list[dict[str, Any]]:
        raise self._unsupported("Fetching live metrics")

    def me(self) -> dict[str, Any]:
        raise self._unsupported("Account details")

    def inbox(self, unread_only: bool = True, limit: int = 25) -> list[dict[str, Any]]:
        raise self._unsupported("Reading your inbox")

    def submit_post(
        self,
        subreddit: str,
        title: str,
        body: str | None = None,
        url: str | None = None,
        flair_id: str | None = None,
    ) -> dict[str, Any]:
        raise self._unsupported("Posting")

    def submit_comment(self, parent_fullname: str, body: str) -> dict[str, Any]:
        raise self._unsupported("Commenting")

    def close(self) -> None:  # pragma: no cover - trivial
        pass
