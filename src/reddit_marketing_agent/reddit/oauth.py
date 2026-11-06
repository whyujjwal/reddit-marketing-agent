"""Full-featured backend using Reddit's official OAuth JSON API (oauth.reddit.com)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import Settings
from .base import (
    SEARCH_SORTS,
    SORTS,
    TIME_FILTERS,
    NotSupported,
    PoliteSession,
    RedditBackend,
    RedditError,
    check_choice,
)
from .models import (
    clean_subreddit,
    comment_from_json,
    post_from_json,
    rule_from_json,
    subreddit_from_json,
)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"


class OAuthBackend(RedditBackend):
    has_metrics = True

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.has_app_credentials:
            raise ValueError("OAuthBackend needs REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET")
        self.settings = settings
        self.can_write = settings.has_user_auth
        self.name = "oauth-user" if self.can_write else "oauth-app"
        self.http = PoliteSession(
            settings.effective_user_agent,
            timeout=settings.request_timeout,
            min_interval=0.6,  # Reddit allows ~100 req/min per OAuth client
            max_wait=settings.max_rate_wait_seconds,
            cache_ttl=min(settings.cache_ttl_seconds, 120),
            transport=transport,
        )
        self._token: str | None = None
        self._token_expiry = 0.0

    def capabilities(self) -> dict[str, bool]:
        caps = super().capabilities()
        caps.update(subreddit_rules=True, subreddit_stats=True, metrics=True)
        caps.update(inbox=self.can_write, account=self.can_write, write=self.can_write)
        return caps

    # -- auth -----------------------------------------------------------------------------------
    def _fetch_token(self) -> None:
        s = self.settings
        if s.refresh_token:
            data = {"grant_type": "refresh_token", "refresh_token": s.refresh_token}
        elif s.username and s.password:
            data = {"grant_type": "password", "username": s.username, "password": s.password}
        else:
            data = {"grant_type": "client_credentials"}
        resp = self.http.request(
            "POST",
            TOKEN_URL,
            data=data,
            auth=(s.client_id or "", s.client_secret or ""),
            use_cache=False,
        )
        payload = _json(resp)
        if resp.status_code != 200 or "access_token" not in payload:
            err = payload.get("error") or resp.status_code
            raise RedditError(
                f"Reddit OAuth login failed ({err}). Check REDDIT_CLIENT_ID/SECRET and, for "
                "posting, REDDIT_USERNAME/PASSWORD (accounts with 2FA need REDDIT_REFRESH_TOKEN)."
            )
        self._token = payload["access_token"]
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600)) - 60

    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() >= self._token_expiry:
            self._fetch_token()
        return {"Authorization": f"bearer {self._token}"}

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> Any:
        params = {"raw_json": 1, **(params or {})}
        for attempt in range(2):
            resp = self.http.request(
                method,
                API + path,
                params=params,
                data=data,
                headers=self._headers(),
                use_cache=use_cache and method == "GET",
            )
            if resp.status_code == 401 and attempt == 0:
                self._token = None  # expired or revoked; refresh once
                continue
            break
        if resp.status_code == 403:
            raise RedditError(f"Reddit refused access to {path} (403: private, banned or quarantined?)")
        if resp.status_code == 404:
            raise RedditError(f"Not found on Reddit: {path}")
        if resp.status_code >= 400:
            raise RedditError(f"Reddit API error {resp.status_code} on {path}: {resp.text[:200]}")
        return _json(resp)

    # -- reads ----------------------------------------------------------------------------------
    def search_subreddits(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self._call("GET", "/subreddits/search", {"q": query, "limit": min(limit, 100), "include_over_18": 0})
        return [subreddit_from_json(c["data"]) for c in _children(data)]

    def subreddit_about(self, name: str) -> dict[str, Any]:
        data = self._call("GET", f"/r/{clean_subreddit(name)}/about")
        return subreddit_from_json(data.get("data", {}))

    def subreddit_rules(self, name: str) -> list[dict[str, Any]]:
        data = self._call("GET", f"/r/{clean_subreddit(name)}/about/rules")
        return [rule_from_json(r) for r in data.get("rules", [])]

    def listing(
        self, subreddit: str, sort: str = "hot", time_filter: str = "day", limit: int = 25
    ) -> list[dict[str, Any]]:
        sort = check_choice(sort, SORTS, "sort")
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if sort in {"top", "controversial"}:
            params["t"] = check_choice(time_filter, TIME_FILTERS, "time_filter")
        data = self._call("GET", f"/r/{clean_subreddit(subreddit)}/{sort}", params)
        return [post_from_json(c["data"]) for c in _children(data) if c.get("kind") == "t3"]

    def search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "month",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        params = {
            "q": query,
            "sort": check_choice(sort, SEARCH_SORTS, "sort"),
            "t": check_choice(time_filter, TIME_FILTERS, "time_filter"),
            "limit": min(limit, 100),
            "type": "link",
        }
        if subreddit:
            path = f"/r/{clean_subreddit(subreddit)}/search"
            params["restrict_sr"] = 1
        else:
            path = "/search"
        data = self._call("GET", path, params)
        return [post_from_json(c["data"]) for c in _children(data) if c.get("kind") == "t3"]

    def thread(
        self, post_id: str, comment_limit: int = 50, sort: str = "top"
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        data = self._call("GET", f"/comments/{post_id}", {"limit": comment_limit, "sort": sort, "depth": 4})
        if not isinstance(data, list) or len(data) < 2:
            raise RedditError(f"Unexpected thread response for {post_id}")
        posts = _children(data[0])
        if not posts:
            raise RedditError(f"Post {post_id} not found")
        post = post_from_json(posts[0]["data"])
        comments: list[dict[str, Any]] = []
        _flatten(_children(data[1]), comments, 0, comment_limit)
        return post, comments

    def info(self, fullnames: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(fullnames), 100):
            chunk = fullnames[i : i + 100]
            data = self._call("GET", "/api/info", {"id": ",".join(chunk)}, use_cache=False)
            for c in _children(data):
                if c.get("kind") == "t3":
                    out.append(post_from_json(c["data"]))
                elif c.get("kind") == "t1":
                    out.append(comment_from_json(c["data"]))
        return out

    def me(self) -> dict[str, Any]:
        if not self.can_write:
            raise NotSupported("Account details need a logged-in Reddit user (see README).")
        d = self._call("GET", "/api/v1/me", use_cache=False)
        return {
            "username": d.get("name"),
            "link_karma": d.get("link_karma"),
            "comment_karma": d.get("comment_karma"),
            "total_karma": d.get("total_karma"),
            "created_utc": d.get("created_utc"),
            "verified_email": d.get("has_verified_email"),
            "is_suspended": d.get("is_suspended", False),
        }

    def recent_activity(self, username: str, limit: int = 100) -> list[dict[str, Any]]:
        data = self._call("GET", f"/user/{username}/overview", {"limit": min(limit, 100)})
        out: list[dict[str, Any]] = []
        for c in _children(data):
            if c.get("kind") == "t3":
                out.append({"kind": "post", **post_from_json(c["data"])})
            elif c.get("kind") == "t1":
                out.append({"kind": "comment", **comment_from_json(c["data"])})
        return out

    def inbox(self, unread_only: bool = True, limit: int = 25) -> list[dict[str, Any]]:
        if not self.can_write:
            raise NotSupported("Reading the inbox needs a logged-in Reddit user (see README).")
        path = "/message/unread" if unread_only else "/message/inbox"
        data = self._call("GET", path, {"limit": min(limit, 100)}, use_cache=False)
        items = []
        for c in _children(data):
            d = c.get("data", {})
            items.append(
                {
                    "fullname": d.get("name"),
                    "type": "comment_reply" if d.get("was_comment") else "message",
                    "author": d.get("author"),
                    "subreddit": d.get("subreddit"),
                    "subject": d.get("subject"),
                    "body": d.get("body") or "",
                    "context": ("https://www.reddit.com" + d["context"]) if d.get("context") else None,
                    "created_utc": d.get("created_utc"),
                    "unread": d.get("new"),
                }
            )
        return items

    # -- writes ---------------------------------------------------------------------------------
    def submit_post(
        self,
        subreddit: str,
        title: str,
        body: str | None = None,
        url: str | None = None,
        flair_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_write()
        data = {
            "sr": clean_subreddit(subreddit),
            "title": title,
            "kind": "link" if url else "self",
            "api_type": "json",
            "sendreplies": "true",
            "resubmit": "false",
        }
        if url:
            data["url"] = url
        else:
            data["text"] = body or ""
        if flair_id:
            data["flair_id"] = flair_id
        payload = self._call("POST", "/api/submit", data=data, use_cache=False)
        result = _api_result(payload)
        d = result.get("data", {})
        return {"fullname": d.get("name"), "id": d.get("id"), "permalink": d.get("url")}

    def submit_comment(self, parent_fullname: str, body: str) -> dict[str, Any]:
        self._require_write()
        payload = self._call(
            "POST",
            "/api/comment",
            data={"thing_id": parent_fullname, "text": body, "api_type": "json"},
            use_cache=False,
        )
        result = _api_result(payload)
        things = result.get("data", {}).get("things", [])
        if not things:
            raise RedditError("Reddit accepted the comment but returned no details")
        c = comment_from_json(things[0]["data"])
        return {"fullname": c["fullname"], "id": c["id"], "permalink": c["permalink"]}

    def _require_write(self) -> None:
        if not self.can_write:
            raise NotSupported(
                "Posting needs a logged-in Reddit user: set REDDIT_USERNAME and REDDIT_PASSWORD "
                "(or REDDIT_REFRESH_TOKEN)."
            )

    def close(self) -> None:
        self.http.close()


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}


def _children(listing: Any) -> list[dict[str, Any]]:
    if isinstance(listing, dict):
        return listing.get("data", {}).get("children", []) or []
    return []


def _flatten(children: list[dict[str, Any]], out: list[dict[str, Any]], depth: int, cap: int) -> None:
    for c in children:
        if len(out) >= cap:
            return
        if c.get("kind") != "t1":
            continue
        d = c["data"]
        out.append(comment_from_json(d, depth))
        replies = d.get("replies")
        if isinstance(replies, dict):
            _flatten(_children(replies), out, depth + 1, cap)


def _api_result(payload: Any) -> dict[str, Any]:
    result = payload.get("json", {}) if isinstance(payload, dict) else {}
    errors = result.get("errors") or []
    if errors:
        msgs = "; ".join(" ".join(str(p) for p in e if p) for e in errors)
        raise RedditError(f"Reddit rejected the submission: {msgs}")
    return result
