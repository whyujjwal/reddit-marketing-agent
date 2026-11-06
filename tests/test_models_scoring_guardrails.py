from __future__ import annotations

import pytest

from reddit_marketing_agent import guardrails as gr
from reddit_marketing_agent.reddit.models import clean_subreddit, html_to_text, parse_thing_ref
from reddit_marketing_agent.scoring import build_search_query, score_post

from .conftest import NOW

CAMPAIGN = {"product_name": "Invoicely", "product_url": "https://www.invoicely.example.com/x", "disclosure": None}


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("https://www.reddit.com/r/SaaS/comments/1abc2d/some_title/", ("t3_", "1abc2d")),
        ("https://old.reddit.com/r/SaaS/comments/1abc2d/some_title/kx9z1/", ("t1_", "kx9z1")),
        ("t1_19s2zh7xo8xj7", ("t1_", "19s2zh7xo8xj7")),
        ("https://redd.it/1abc2d", ("t3_", "1abc2d")),
        ("t1_kx9z1", ("t1_", "kx9z1")),
        ("1abc2d", ("t3_", "1abc2d")),
    ],
)
def test_parse_thing_ref(ref, expected):
    assert parse_thing_ref(ref) == expected


def test_parse_thing_ref_rejects_garbage():
    with pytest.raises(ValueError):
        parse_thing_ref("not a reddit thing!")


def test_clean_subreddit():
    assert clean_subreddit("r/SaaS") == "SaaS"
    assert clean_subreddit("/r/SaaS/") == "SaaS"
    assert clean_subreddit("https://www.reddit.com/r/SaaS/top/") == "SaaS"
    assert clean_subreddit("SaaS+startups") == "SaaS+startups"


def test_html_to_text():
    html = '<div class="md"><p>Hello &amp; welcome</p><p>Line <a href="x">two</a></p></div>'
    assert html_to_text(html) == "Hello & welcome\n\nLine two"


def _post(**kw):
    base = {"title": "", "selftext": "", "created_utc": NOW - 3600, "num_comments": 5, "score": 3}
    base.update(kw)
    return base


def test_score_rewards_intent_relevance_and_freshness():
    hot = score_post(
        _post(title="Looking for an alternative to FreshBooks for invoicing?"),
        keywords=["invoicing"], competitors=["FreshBooks"], now=NOW,
    )  # fmt: skip
    cold = score_post(
        _post(title="My invoicing story", created_utc=NOW - 20 * 86400, num_comments=300),
        keywords=["invoicing"], now=NOW,
    )  # fmt: skip
    assert hot["score"] >= 70
    assert "mentions competitor FreshBooks" in hot["reasons"]
    assert cold["score"] < 25
    assert any("buried" in c for c in cold["cautions"])


@pytest.mark.parametrize("flag", ["locked", "archived", "removed"])
def test_score_zero_for_closed_threads(flag):
    assert score_post(_post(title="Recommend invoicing tools?", **{flag: True}), now=NOW)["score"] == 0


def test_score_skips_own_posts():
    assert score_post(_post(title="Any recs?", author="Me"), own_username="me", now=NOW)["score"] == 0


def test_keyword_matching_is_word_bounded():
    r = score_post(_post(title="pinvoicingx"), keywords=["invoicing"], now=NOW)
    assert not any(x.startswith("mentions") for x in r["reasons"])


def test_build_search_query():
    assert build_search_query(["invoice software", "FreshBooks", ""]) == '"invoice software" OR FreshBooks'
    assert len(build_search_query(["x" * 100] * 10)) <= 480


def test_promo_policy_detection():
    banned = gr.analyze_promo_policy([{"name": "No self-promotion", "description": "Don't."}])
    restricted = gr.analyze_promo_policy(
        [{"name": "Promotion", "description": "Self-promo only in the weekly thread, follow 10% rule"}]
    )
    unclear = gr.analyze_promo_policy([{"name": "Be kind", "description": "No personal attacks"}])
    assert banned["policy"] == "banned"
    assert restricted["policy"] == "restricted"
    assert unclear["policy"] == "unclear"


def _codes(checks):
    return {c["code"] for c in checks}


def test_undisclosed_product_mention_is_blocked():
    checks, promo = gr.check_draft(
        kind="comment", body="You should try Invoicely, it handles recurring invoices and reminders well for most freelancers.",
        title=None, url=None, campaign=CAMPAIGN, promo_policy="unclear",
    )  # fmt: skip
    assert promo is True
    assert "no_disclosure" in _codes(gr.blocking(checks))


def test_disclosed_mention_passes_and_domain_counts_as_mention():
    checks, promo = gr.check_draft(
        kind="comment",
        body="I'd export your client list first, then compare recurring billing features. Full disclosure: "
        "I built invoicely.example.com, which does reminders, but Wave is also solid and free.",
        title=None, url=None, campaign=CAMPAIGN, promo_policy="unclear",
    )  # fmt: skip
    assert promo is True
    assert not gr.blocking(checks)


def test_non_promotional_reply_has_no_promo_checks():
    checks, promo = gr.check_draft(
        kind="comment", body="Send invoices on the same weekday every month and add a late fee clause; it cut my late payments in half.",
        title=None, url=None, campaign=CAMPAIGN, promo_policy="banned",
    )  # fmt: skip
    assert promo is False
    assert not gr.blocking(checks)


def test_promo_in_banned_sub_and_shortener_are_blocked():
    checks, _ = gr.check_draft(
        kind="comment", body="I made Invoicely, try it: https://bit.ly/abc " + "word " * 30,
        title=None, url=None, campaign=CAMPAIGN, promo_policy="banned",
    )  # fmt: skip
    assert {"promo_banned", "shortener"} <= _codes(gr.blocking(checks))


def test_duplicate_and_closed_target_blocked():
    body = "Same text " * 10
    checks, _ = gr.check_draft(
        kind="comment", body=body, title=None, url=None, campaign=None, promo_policy=None,
        target={"locked": True}, recent_hashes={gr.body_hash(body.upper())},
    )  # fmt: skip
    assert {"duplicate", "locked"} <= _codes(gr.blocking(checks))


def _act(hours_ago, sub="SaaS", promo=0):
    return {"created_at": NOW - hours_ago * 3600, "subreddit": sub, "promotional": promo}


def _limits(activity, promotional=True, sub="SaaS"):
    return gr.check_publish_limits(
        activity=activity, subreddit=sub, promotional=promotional, max_per_day=3,
        min_minutes_between=10, promo_cooldown_hours=72, max_promo_ratio=0.1, now=NOW,
    )  # fmt: skip


def test_publish_limits():
    assert _codes(_limits([_act(1), _act(2), _act(3)], promotional=False)) == {"daily_limit"}
    assert "too_fast" in _codes(_limits([{**_act(0), "created_at": NOW - 120}], promotional=False))
    assert "subreddit_cooldown" in _codes(_limits([_act(30, promo=1)]))
    assert "subreddit_cooldown" not in _codes(_limits([_act(30, sub="other", promo=1)]))
    ratio = _limits([_act(30 + i, sub="x", promo=1) for i in range(5)], sub="y")
    assert "promo_ratio" in _codes(ratio)
    assert not gr.blocking([c for c in ratio if c["code"] == "promo_ratio"])
