"""Zero-setup, read-only backend built on Reddit's public RSS/Atom feeds.

Reddit blocks unauthenticated JSON requests, but its RSS feeds are a public, documented feature.
They carry titles, authors, bodies and timestamps but **no scores or comment counts**, and they
are heavily rate limited, so this backend paces itself and caches aggressively. It is meant for
getting started and light research; real campaigns should use API credentials.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from ..config import Settings
from .base import (
    SEARCH_SORTS,
    SORTS,
    TIME_FILTERS,
    PoliteSession,
    RedditBackend,
    RedditError,
    check_choice,
)
from .models import REDDIT, clean_subreddit, html_to_text, make_comment, make_post

ATOM = "{http://www.w3.org/2005/Atom}"
_MD_RE = re.compile(r'<div class="md">(.*?)</div>\s*<!-- SC_ON -->', re.S)
_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')
_FEED_LINKS_RE = re.compile(r"<a [^>]*>\s*\[(link|comments)\]\s*</a>")


class RSSBackend(RedditBackend):
    name = "rss"
    can_write = False
    has_metrics = False

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.http = PoliteSession(
            settings.effective_user_agent,
            timeout=settings.request_timeout,
            min_interval=2.0,
            max_wait=settings.max_rate_wait_seconds,
            cache_ttl=settings.cache_ttl_seconds,
            transport=transport,
        )

    def _feed(self, path: str, params: dict[str, Any] | None = None) -> ET.Element:
        resp = self.http.request("GET", REDDIT + path, params=params)
        if resp.status_code == 404:
            raise RedditError(f"Not found on Reddit: {path}")
        if resp.status_code == 403:
            raise RedditError(f"Reddit refused {path} (403: private, banned or quarantined?)")
        if resp.status_code != 200:
            raise RedditError(f"Reddit returned HTTP {resp.status_code} for {path}")
        try:
            return safe_fromstring(resp.content)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise RedditError(f"Reddit returned an unreadable feed for {path}") from exc

    # -- reads ----------------------------------------------------------------------------------
    def search_subreddits(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        root = self._feed("/subreddits/search.rss", {"q": query, "limit": min(limit, 100)})
        out = []
        for entry in root.findall(f"{ATOM}entry"):
            link = _link(entry)
            name = clean_subreddit(link) if "/r/" in link else _text(entry, "title")
            out.append(
                {
                    "name": name,
                    "title": _text(entry, "title"),
                    "description": html_to_text(_body_html(_text(entry, "content"))),
                    "subscribers": None,
                    "active_users": None,
                    "over_18": None,
                    "type": None,
                    "submission_type": None,
                    "created_utc": _ts(_text(entry, "published") or _text(entry, "updated")),
                    "url": link or f"{REDDIT}/r/{name}/",
                }
            )
        return out

    def listing(
        self, subreddit: str, sort: str = "hot", time_filter: str = "day", limit: int = 25
    ) -> list[dict[str, Any]]:
        sort = check_choice(sort, SORTS, "sort")
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if sort in {"top", "controversial"}:
            params["t"] = check_choice(time_filter, TIME_FILTERS, "time_filter")
        root = self._feed(f"/r/{clean_subreddit(subreddit)}/{sort}.rss", params)
        return [p for p in (_entry_to_post(e) for e in root.findall(f"{ATOM}entry")) if p]

    def search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "month",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": query,
            "sort": check_choice(sort, SEARCH_SORTS, "sort"),
            "t": check_choice(time_filter, TIME_FILTERS, "time_filter"),
            "limit": min(limit, 100),
            "type": "link",
        }
        if subreddit:
            path = f"/r/{clean_subreddit(subreddit)}/search.rss"
            params["restrict_sr"] = 1
        else:
            path = "/search.rss"
        root = self._feed(path, params)
        return [p for p in (_entry_to_post(e) for e in root.findall(f"{ATOM}entry")) if p]

    def thread(
        self, post_id: str, comment_limit: int = 50, sort: str = "top"
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        root = self._feed(f"/comments/{post_id}/.rss", {"limit": comment_limit, "sort": sort})
        post: dict[str, Any] | None = None
        comments: list[dict[str, Any]] = []
        entries = root.findall(f"{ATOM}entry")
        for entry in entries:
            ident = _text(entry, "id")
            if ident.startswith("t3_") and post is None:
                post = _entry_to_post(entry)
            elif ident.startswith("t1_"):
                comments.append(_entry_to_comment(entry, post_id))
        if post is None:
            # Some feeds omit the post entry; synthesize it from the feed header.
            title = _text(root, "title")
            post = make_post(
                id=post_id,
                title=title,
                subreddit=_category(root),
                permalink=_link(root) or f"{REDDIT}/comments/{post_id}/",
            )
        return post, comments[:comment_limit]

    def close(self) -> None:
        self.http.close()


# -- Atom helpers --------------------------------------------------------------------------------


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(f"{ATOM}{tag}")
    return (child.text or "").strip() if child is not None else ""


def _link(el: ET.Element) -> str:
    for link in el.findall(f"{ATOM}link"):
        if link.get("rel") in (None, "alternate"):
            return link.get("href", "")
    return ""


def _category(el: ET.Element) -> str | None:
    cat = el.find(f"{ATOM}category")
    return cat.get("term") if cat is not None else None


def _author(el: ET.Element) -> str | None:
    author = el.find(f"{ATOM}author/{ATOM}name")
    if author is None or not author.text:
        return None
    return author.text.strip().removeprefix("/u/")


def _ts(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _body_html(content: str) -> str:
    if m := _MD_RE.search(content):
        return m.group(1)
    return _FEED_LINKS_RE.sub("", content.split("submitted by")[0])


def _entry_to_post(entry: ET.Element) -> dict[str, Any] | None:
    ident = _text(entry, "id")
    if not ident.startswith("t3_"):
        return None
    content = _text(entry, "content")
    permalink = _link(entry)
    link_match = _LINK_RE.search(content)
    external = link_match.group(1) if link_match else None
    is_self = external is None or external.rstrip("/") == permalink.rstrip("/")
    return make_post(
        id=ident[3:],
        fullname=ident,
        subreddit=_category(entry),
        title=_text(entry, "title"),
        author=_author(entry),
        selftext=html_to_text(_body_html(content)) if is_self else "",
        url=permalink if is_self else external,
        permalink=permalink,
        created_utc=_ts(_text(entry, "published") or _text(entry, "updated")),
        is_self=is_self,
    )


def _entry_to_comment(entry: ET.Element, post_id: str) -> dict[str, Any]:
    ident = _text(entry, "id")
    permalink = _link(entry)
    return make_comment(
        id=ident[3:],
        fullname=ident,
        subreddit=_category(entry),
        author=_author(entry),
        body=html_to_text(_body_html(_text(entry, "content"))),
        permalink=permalink,
        created_utc=_ts(_text(entry, "updated") or _text(entry, "published")),
        link_id=f"t3_{post_id}",
    )
