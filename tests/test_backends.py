"""Backend tests against canned Reddit responses (no network)."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from reddit_marketing_agent.config import Settings
from reddit_marketing_agent.reddit import OAuthBackend, RateLimited, RedditError, RSSBackend
from reddit_marketing_agent.reddit.base import PoliteSession

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <category term="SaaS" label="r/SaaS"/>
  <title>newest submissions : SaaS</title>
  <link rel="alternate" href="https://www.reddit.com/r/SaaS/new/"/>
  <entry>
    <author><name>/u/founder_jane</name><uri>https://www.reddit.com/user/founder_jane</uri></author>
    <category term="SaaS" label="r/SaaS"/>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class=&quot;md&quot;&gt;&lt;p&gt;Looking for a FreshBooks alternative &amp;amp; cheaper.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt; &amp;#32; submitted by &amp;#32; &lt;a href=&quot;https://www.reddit.com/user/founder_jane&quot;&gt; /u/founder_jane &lt;/a&gt; &lt;br/&gt; &lt;span&gt;&lt;a href=&quot;https://www.reddit.com/r/SaaS/comments/1abc/alt/&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt;</content>
    <id>t3_1abc</id>
    <link href="https://www.reddit.com/r/SaaS/comments/1abc/alt/"/>
    <updated>2026-11-06T01:42:46+00:00</updated>
    <published>2026-11-06T01:42:46+00:00</published>
    <title>Any FreshBooks alternatives?</title>
  </entry>
  <entry>
    <author><name>/u/linker</name></author>
    <category term="SaaS" label="r/SaaS"/>
    <content type="html">&lt;span&gt;&lt;a href=&quot;https://example.com/blog&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt;</content>
    <id>t3_2def</id>
    <link href="https://www.reddit.com/r/SaaS/comments/2def/blog/"/>
    <published>2026-11-05T01:00:00+00:00</published>
    <title>My blog post</title>
  </entry>
</feed>"""

THREAD = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Any FreshBooks alternatives? : SaaS</title>
  <entry><id>t3_1abc</id><title>Any FreshBooks alternatives?</title><category term="SaaS"/>
    <link href="https://www.reddit.com/r/SaaS/comments/1abc/alt/"/><content type="html">&lt;div class="md"&gt;&lt;p&gt;Body&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;</content></entry>
  <entry><id>t1_c1</id><title>/u/helper on Any FreshBooks alternatives?</title><author><name>/u/helper</name></author>
    <link href="https://www.reddit.com/r/SaaS/comments/1abc/alt/c1/"/><content type="html">&lt;div class="md"&gt;&lt;p&gt;Try Wave.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
    <updated>2026-11-06T02:00:00+00:00</updated></entry>
</feed>"""


def rss_backend(handler) -> RSSBackend:
    return RSSBackend(Settings(cache_ttl_seconds=0), transport=httpx.MockTransport(handler))


def test_rss_listing_parses_posts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text=FEED, headers={"content-type": "application/atom+xml"})

    posts = rss_backend(handler).listing("r/SaaS", sort="new", limit=5)
    assert seen["url"].startswith("https://www.reddit.com/r/SaaS/new.rss")
    assert "reddit-marketing-agent" in seen["ua"]
    first, second = posts
    assert first["fullname"] == "t3_1abc"
    assert first["author"] == "founder_jane"
    assert first["subreddit"] == "SaaS"
    assert first["selftext"] == "Looking for a FreshBooks alternative & cheaper."
    assert first["is_self"] is True and first["score"] is None
    assert second["is_self"] is False and second["url"] == "https://example.com/blog"


def test_rss_search_builds_query_and_thread_parses_comments():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        body = THREAD if "/comments/" in request.url.path else FEED
        return httpx.Response(200, text=body)

    b = rss_backend(handler)
    b.search('"invoice software" OR FreshBooks', subreddit="SaaS+freelance", sort="new", time_filter="week")
    assert calls[0].path == "/r/SaaS+freelance/search.rss"
    q = parse_qs(calls[0].query.decode())
    assert q["restrict_sr"] == ["1"] and q["sort"] == ["new"] and q["t"] == ["week"]

    post, comments = b.thread("1abc")
    assert post["title"] == "Any FreshBooks alternatives?"
    assert comments[0]["author"] == "helper" and comments[0]["body"] == "Try Wave."


def test_rss_unsupported_features_explain_credentials():
    b = rss_backend(lambda r: httpx.Response(200, text=FEED))
    with pytest.raises(RedditError, match="credentials"):
        b.subreddit_rules("SaaS")
    with pytest.raises(RedditError, match="credentials"):
        b.submit_comment("t3_x", "hi")


def test_rss_rejects_xml_entity_bombs():
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><feed>&lol2;</feed>'
    b = rss_backend(lambda r: httpx.Response(200, text=bomb))
    with pytest.raises(RedditError, match="unreadable"):
        b.listing("SaaS")


def test_polite_session_honors_rate_limit_headers_and_retries_429():
    t = {"now": 0.0}
    slept = []
    responses = [
        httpx.Response(200, text="ok", headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "18"}),
        httpx.Response(429, text="slow", headers={"retry-after": "5"}),
        httpx.Response(200, text="ok2"),
    ]

    def sleep(s):
        slept.append(round(s, 1))
        t["now"] += s

    s = PoliteSession(
        "ua",
        transport=httpx.MockTransport(lambda r: responses.pop(0)),
        cache_ttl=0,
        sleep=sleep,
        clock=lambda: t["now"],
    )
    assert s.request("GET", "https://www.reddit.com/a.rss").text == "ok"
    assert s.request("GET", "https://www.reddit.com/b.rss").text == "ok2"
    assert slept == [18.5, 5.5]


def test_polite_session_raises_when_wait_too_long_and_explains_blocks():
    s = PoliteSession(
        "ua", max_wait=10, cache_ttl=0, sleep=lambda s: None, clock=lambda: 0.0,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "300"})),
    )  # fmt: skip
    s.request("GET", "https://www.reddit.com/a.rss")
    with pytest.raises(RateLimited):
        s.request("GET", "https://www.reddit.com/b.rss")

    blocked = PoliteSession(
        "ua",
        cache_ttl=0,
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="You've been blocked by network security.")),
    )
    with pytest.raises(RedditError, match="credentials"):
        blocked.request("GET", "https://www.reddit.com/a.json")


def test_polite_session_caches_gets():
    hits = []
    s = PoliteSession(
        "ua", cache_ttl=60, transport=httpx.MockTransport(lambda r: hits.append(1) or httpx.Response(200, text="x"))
    )
    s.request("GET", "https://www.reddit.com/a.rss", params={"q": "1"})
    s.request("GET", "https://www.reddit.com/a.rss", params={"q": "1"})
    assert len(hits) == 1


# -- OAuth -----------------------------------------------------------------------------------


def listing(*children):
    return {"kind": "Listing", "data": {"children": list(children)}}


def t3(id, **kw):
    return {
        "kind": "t3",
        "data": {
            "id": id,
            "name": f"t3_{id}",
            "subreddit": "SaaS",
            "title": kw.pop("title", "T"),
            "permalink": f"/r/SaaS/comments/{id}/t/",
            "score": 5,
            "num_comments": 2,
            "is_self": True,
            **kw,
        },
    }


def t1(id, replies=None, **kw):
    return {
        "kind": "t1",
        "data": {
            "id": id,
            "name": f"t1_{id}",
            "body": kw.pop("body", "hi"),
            "author": "a",
            "permalink": f"/r/SaaS/comments/p/t/{id}/",
            "replies": replies or "",
            **kw,
        },
    }


class FakeReddit:
    def __init__(self):
        self.tokens = 0
        self.requests: list[httpx.Request] = []
        self.expire_next = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/access_token":
            self.tokens += 1
            form = parse_qs(request.content.decode())
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200, json={"access_token": f"tok{self.tokens}", "expires_in": 3600, "grant": form["grant_type"][0]}
            )
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"error": 401})
        assert request.headers["authorization"] == f"bearer tok{self.tokens}"
        if path == "/r/SaaS/search":
            return httpx.Response(200, json=listing(t3("a1", title="Need invoicing help?")))
        if path == "/comments/a1":
            tree = listing(t1("c1", replies=listing(t1("c2", body="nested"), {"kind": "more", "data": {}})))
            return httpx.Response(200, json=[listing(t3("a1")), tree])
        if path == "/r/SaaS/about/rules":
            return httpx.Response(
                200, json={"rules": [{"short_name": "No spam", "description": "No self-promotion", "kind": "all"}]}
            )
        if path == "/api/comment":
            form = parse_qs(request.content.decode())
            if form["text"][0] == "fail":
                return httpx.Response(
                    200, json={"json": {"errors": [["RATELIMIT", "you are doing that too much", "ratelimit"]]}}
                )
            return httpx.Response(
                200, json={"json": {"errors": [], "data": {"things": [t1("new1", body=form["text"][0])]}}}
            )
        if path == "/api/submit":
            return httpx.Response(
                200,
                json={
                    "json": {
                        "errors": [],
                        "data": {"id": "n1", "name": "t3_n1", "url": "https://www.reddit.com/r/SaaS/comments/n1/x/"},
                    }
                },
            )
        return httpx.Response(404, json={})


def oauth(fake, **kw) -> OAuthBackend:
    settings = Settings(client_id="id", client_secret="secret", cache_ttl_seconds=0, **kw)
    return OAuthBackend(settings, transport=httpx.MockTransport(fake))


def test_oauth_app_only_reads_and_thread_flattening():
    fake = FakeReddit()
    b = oauth(fake)
    assert b.name == "oauth-app" and not b.can_write
    posts = b.search("invoicing", subreddit="SaaS")
    assert posts[0]["fullname"] == "t3_a1" and posts[0]["score"] == 5
    assert posts[0]["permalink"] == "https://www.reddit.com/r/SaaS/comments/a1/t/"
    post, comments = b.thread("a1")
    assert [(c["id"], c["depth"]) for c in comments] == [("c1", 0), ("c2", 1)]
    assert b.subreddit_rules("SaaS")[0]["name"] == "No spam"
    assert fake.tokens == 1
    assert parse_qs(fake.requests[0].content.decode()) == {"grant_type": ["client_credentials"]}
    with pytest.raises(RedditError, match="logged-in"):
        b.submit_comment("t3_a1", "hello")


def test_oauth_user_posts_and_surfaces_reddit_errors():
    fake = FakeReddit()
    b = oauth(fake, username="me", password="pw")
    assert b.name == "oauth-user" and b.can_write
    assert b.submit_comment("t3_a1", "hello")["fullname"] == "t1_new1"
    assert b.submit_post("SaaS", "Title", body="Body")["permalink"].endswith("/n1/x/")
    with pytest.raises(RedditError, match="too much"):
        b.submit_comment("t3_a1", "fail")


def test_oauth_refreshes_expired_token():
    fake = FakeReddit()
    b = oauth(fake)
    b.search("x", subreddit="SaaS")
    fake.expire_next = True
    b.search("y", subreddit="SaaS")
    assert fake.tokens == 2


SUB_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>t5_abc</id><title>Self Employment</title>
    <link href="https://www.reddit.com/r/selfemployed/"/>
    <content type="html">For the self employed. &lt;a href=&quot;https://www.reddit.com/r/selfemployed/&quot;&gt;[link]&lt;/a&gt;</content>
  </entry>
</feed>"""


def test_rss_subreddit_search_strips_feed_links():
    subs = rss_backend(lambda r: httpx.Response(200, text=SUB_FEED)).search_subreddits("self employed")
    assert subs == [
        {
            "name": "selfemployed",
            "title": "Self Employment",
            "description": "For the self employed.",
            "subscribers": None,
            "active_users": None,
            "over_18": None,
            "type": None,
            "submission_type": None,
            "created_utc": None,
            "url": "https://www.reddit.com/r/selfemployed/",
        }
    ]
