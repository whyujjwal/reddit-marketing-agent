"""Normalized shapes for Reddit things, shared by every backend.

Every backend returns plain dicts built by these helpers so the rest of the agent never has to
care whether data came from the OAuth JSON API or from public RSS feeds. Fields a backend cannot
provide are ``None``.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

REDDIT = "https://www.reddit.com"

_ID_RE = re.compile(r"^(t[1-6]_)?([a-z0-9]{2,16})$", re.I)
_URL_POST_RE = re.compile(r"/comments/([a-z0-9]+)(?:/[^/]*/([a-z0-9]+))?", re.I)
_SHORT_URL_RE = re.compile(r"redd\.it/([a-z0-9]+)", re.I)


def parse_thing_ref(ref: str) -> tuple[str, str]:
    """Turn a URL, fullname (``t3_abc``) or bare id into ``(kind_prefix, id)``.

    URLs pointing at a specific comment resolve to that comment (``t1_``); otherwise posts (``t3_``).
    """
    ref = ref.strip()
    if m := _URL_POST_RE.search(ref):
        post_id, comment_id = m.group(1), m.group(2)
        return ("t1_", comment_id.lower()) if comment_id else ("t3_", post_id.lower())
    if m := _SHORT_URL_RE.search(ref):
        return "t3_", m.group(1).lower()
    if m := _ID_RE.match(ref):
        return (m.group(1) or "t3_").lower(), m.group(2).lower()
    raise ValueError(f"Could not understand Reddit reference: {ref!r}")


def clean_subreddit(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^(https?://(www\.|old\.)?reddit\.com)?/?r/", "", name, flags=re.I)
    return name.strip("/").split("/")[0]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "blockquote", "pre", "table"}:
            self.parts.append("\n\n")
        elif tag in {"br", "li", "div", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(unescape(html) if "&lt;" in html else html)
    text = unescape("".join(parser.parts))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_post(**fields: Any) -> dict[str, Any]:
    keys = (
        "id", "fullname", "subreddit", "title", "author", "selftext", "url", "permalink",
        "score", "upvote_ratio", "num_comments", "created_utc", "is_self", "over_18",
        "locked", "archived", "stickied", "flair", "removed",
    )  # fmt: skip
    post = {k: fields.get(k) for k in keys}
    if post["id"] and not post["fullname"]:
        post["fullname"] = f"t3_{post['id']}"
    return post


def make_comment(**fields: Any) -> dict[str, Any]:
    keys = (
        "id", "fullname", "subreddit", "author", "body", "permalink", "score", "created_utc",
        "depth", "parent_id", "link_id", "is_submitter", "removed",
    )  # fmt: skip
    comment = {k: fields.get(k) for k in keys}
    if comment["id"] and not comment["fullname"]:
        comment["fullname"] = f"t1_{comment['id']}"
    return comment


# --- JSON API --------------------------------------------------------------------------------


def _removed(data: dict[str, Any], text_field: str) -> bool:
    return bool(
        data.get("removed_by_category") or data.get(text_field) in {"[removed]", "[deleted]"} or data.get("banned_by")
    )


def post_from_json(d: dict[str, Any]) -> dict[str, Any]:
    permalink = d.get("permalink") or ""
    return make_post(
        id=d.get("id"),
        fullname=d.get("name"),
        subreddit=d.get("subreddit"),
        title=d.get("title"),
        author=d.get("author"),
        selftext=d.get("selftext") or "",
        url=d.get("url"),
        permalink=REDDIT + permalink if permalink.startswith("/") else permalink or None,
        score=d.get("score"),
        upvote_ratio=d.get("upvote_ratio"),
        num_comments=d.get("num_comments"),
        created_utc=d.get("created_utc"),
        is_self=d.get("is_self"),
        over_18=d.get("over_18"),
        locked=d.get("locked"),
        archived=d.get("archived"),
        stickied=d.get("stickied"),
        flair=d.get("link_flair_text"),
        removed=_removed(d, "selftext"),
    )


def comment_from_json(d: dict[str, Any], depth: int | None = None) -> dict[str, Any]:
    permalink = d.get("permalink") or ""
    return make_comment(
        id=d.get("id"),
        fullname=d.get("name"),
        subreddit=d.get("subreddit"),
        author=d.get("author"),
        body=d.get("body") or "",
        permalink=REDDIT + permalink if permalink.startswith("/") else permalink or None,
        score=d.get("score"),
        created_utc=d.get("created_utc"),
        depth=d.get("depth", depth),
        parent_id=d.get("parent_id"),
        link_id=d.get("link_id"),
        is_submitter=d.get("is_submitter"),
        removed=_removed(d, "body"),
    )


def subreddit_from_json(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": d.get("display_name"),
        "title": d.get("title"),
        "description": d.get("public_description") or "",
        "subscribers": d.get("subscribers"),
        "active_users": d.get("active_user_count") or d.get("accounts_active"),
        "over_18": d.get("over18"),
        "type": d.get("subreddit_type"),
        "submission_type": d.get("submission_type"),
        "created_utc": d.get("created_utc"),
        "url": REDDIT + (d.get("url") or f"/r/{d.get('display_name')}/"),
    }


def rule_from_json(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": d.get("short_name"),
        "description": d.get("description") or "",
        "applies_to": d.get("kind"),
    }
