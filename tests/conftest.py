from __future__ import annotations

import time
from typing import Any

import pytest

from reddit_marketing_agent.config import Settings
from reddit_marketing_agent.reddit.base import RedditBackend
from reddit_marketing_agent.reddit.models import make_post
from reddit_marketing_agent.service import MarketingAgent
from reddit_marketing_agent.store import Store

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeBackend(RedditBackend):
    """In-memory Reddit for service tests."""

    def __init__(self, *, can_write: bool = True, metrics: bool = True, rules: list | None = None) -> None:
        self.can_write = can_write
        self.has_metrics = metrics
        self.name = "oauth-user" if can_write else ("oauth-app" if metrics else "rss")
        self.posts: dict[str, dict[str, Any]] = {}
        self.rules = rules if rules is not None else []
        self.submitted: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []

    def capabilities(self) -> dict[str, bool]:
        caps = super().capabilities()
        caps["subreddit_rules"] = self.has_metrics
        return caps

    def add(self, id: str, **fields: Any) -> dict[str, Any]:
        fields.setdefault("subreddit", "SaaS")
        fields.setdefault("created_utc", NOW - 3600)
        fields.setdefault("is_self", True)
        fields.setdefault("permalink", f"https://www.reddit.com/r/{fields['subreddit']}/comments/{id}/x/")
        post = make_post(id=id, **fields)
        self.posts[post["fullname"]] = post
        return post

    def search(self, query, subreddit=None, sort="relevance", time_filter="month", limit=25):
        self.searches.append({"query": query, "subreddit": subreddit, "sort": sort})
        return list(self.posts.values())[:limit]

    def listing(self, subreddit, sort="hot", time_filter="day", limit=25):
        return list(self.posts.values())[:limit]

    def thread(self, post_id, comment_limit=50, sort="top"):
        return self.posts[f"t3_{post_id}"], []

    def subreddit_rules(self, name):
        if not self.has_metrics:
            raise self._unsupported("rules")
        return self.rules

    def subreddit_about(self, name):
        if not self.has_metrics:
            raise self._unsupported("about")
        return {"name": name, "description": "", "subscribers": 1000}

    def info(self, fullnames):
        if not self.has_metrics:
            raise self._unsupported("info")
        return [self.posts[f] for f in fullnames if f in self.posts]

    def me(self):
        return {"username": "tester", "total_karma": 500, "created_utc": NOW - 400 * 86400}

    def submit_comment(self, parent_fullname, body):
        self.submitted.append({"parent": parent_fullname, "body": body})
        n = len(self.submitted)
        return {"fullname": f"t1_c{n}", "id": f"c{n}", "permalink": f"https://www.reddit.com/r/SaaS/comments/p/x/c{n}/"}

    def submit_post(self, subreddit, title, body=None, url=None, flair_id=None):
        self.submitted.append({"subreddit": subreddit, "title": title, "body": body, "url": url})
        n = len(self.submitted)
        return {
            "fullname": f"t3_n{n}",
            "id": f"n{n}",
            "permalink": f"https://www.reddit.com/r/{subreddit}/comments/n{n}/x/",
        }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, posting_enabled=True)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def agent(settings, backend, clock, tmp_path) -> MarketingAgent:
    return MarketingAgent(settings, backend=backend, store=Store(tmp_path / "t.db", clock=clock), clock=clock)


@pytest.fixture
def campaign(agent):
    return agent.save_campaign(
        name="launch",
        product_name="Invoicely",
        product_url="https://invoicely.example.com",
        description="Simple invoicing for freelancers",
        keywords=["invoice software", "invoicing"],
        competitors=["FreshBooks"],
        subreddits=["r/freelance", "SaaS"],
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
