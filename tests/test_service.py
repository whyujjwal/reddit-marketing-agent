from __future__ import annotations

import pytest

from reddit_marketing_agent.reddit import NotSupported
from reddit_marketing_agent.service import MarketingAgent
from reddit_marketing_agent.store import Store

from .conftest import NOW, FakeBackend

GOOD_REPLY = (
    "I had the same problem with chasing payments. What fixed it for me was sending invoices on a "
    "fixed day, adding a clear late fee, and automating two reminders. Full disclosure: I built "
    "Invoicely, which does the reminders part, but a spreadsheet plus calendar alerts works too."
)


def test_campaign_roundtrip(agent, campaign):
    assert campaign["subreddits"] == ["freelance", "SaaS"]
    updated = agent.save_campaign(name="LAUNCH", product_name="Invoicely", description="v2", keywords=["x"])
    assert updated["id"] == campaign["id"] and updated["description"] == "v2"
    with pytest.raises(ValueError, match="No campaign named"):
        agent.find_opportunities(campaign="nope")


def test_find_opportunities_ranks_and_filters(agent, backend, campaign):
    backend.add(
        "good",
        title="Any recommendations for invoicing software? FreshBooks got too expensive",
        num_comments=4,
        score=5,
    )
    backend.add("meh", title="Weekly chat thread", num_comments=900, score=50)
    backend.add("locked", title="Looking for invoice software", locked=True)
    result = agent.find_opportunities(campaign="launch")
    ids = [o["post"]["fullname"] for o in result["opportunities"]]
    assert ids == ["t3_good"]
    assert result["opportunities"][0]["promo_policy"] == "unclear"
    # campaign subreddits searched as one multireddit, both sorts in API mode
    assert {s["subreddit"] for s in backend.searches} == {"freelance+SaaS"}
    assert {s["sort"] for s in backend.searches} == {"new", "relevance"}


def test_find_opportunities_requires_terms(agent):
    with pytest.raises(ValueError):
        agent.find_opportunities()


def test_draft_and_publish_flow(agent, backend, campaign, clock):
    backend.add("p1", title="How do you get clients to pay on time?")
    out = agent.draft_reply("https://www.reddit.com/r/SaaS/comments/p1/how/", GOOD_REPLY, campaign="launch")
    draft = out["draft"]
    assert draft["promotional"] is True and draft["blocked"] is False
    assert "confirm=true" in out["next_step"]

    preview = agent.publish_draft(draft["draft_id"])
    assert preview["published"] is False and preview["needs_confirmation"] is True
    assert backend.submitted == []

    done = agent.publish_draft(draft["draft_id"], confirm=True)
    assert done["published"] is True
    assert backend.submitted == [{"parent": "t3_p1", "body": GOOD_REPLY}]
    with pytest.raises(ValueError, match="already published"):
        agent.publish_draft(draft["draft_id"], confirm=True)

    # Second promo in the same subreddit right away is blocked by pacing + cooldown
    backend.add("p2", title="Invoice tools?")
    d2 = agent.draft_reply("t3_p2", GOOD_REPLY.replace("same", "exact same"), campaign="launch")["draft"]
    blocked = agent.publish_draft(d2["draft_id"], confirm=True)
    assert blocked["published"] is False
    assert {"too_fast", "subreddit_cooldown"} <= {c["code"] for c in blocked["blocked_by"]}
    assert len(backend.submitted) == 1


def test_blocked_draft_cannot_publish_until_revised(agent, backend, campaign):
    backend.add("p1", title="Invoice tools?")
    d = agent.draft_reply(
        "t3_p1", "Try Invoicely, it's great for invoices and reminders and much more, honestly.", campaign="launch"
    )["draft"]
    assert d["blocked"] is True
    assert agent.publish_draft(d["draft_id"], confirm=True)["published"] is False
    revised = agent.revise_draft(d["draft_id"], body=GOOD_REPLY)["draft"]
    assert revised["blocked"] is False
    assert agent.publish_draft(d["draft_id"], confirm=True)["published"] is True


def test_duplicate_text_blocked_after_publish(agent, backend, clock):
    backend.add("p1", title="q1")
    backend.add("p2", title="q2", subreddit="other")
    body = "Honest advice from doing this for years: " + "keep a simple checklist and review it every Friday. " * 2
    d1 = agent.draft_reply("t3_p1", body)["draft"]
    agent.publish_draft(d1["draft_id"], confirm=True)
    clock.advance(3600)
    d2 = agent.draft_reply("t3_p2", body)["draft"]
    assert "duplicate" in {c["code"] for c in d2["checks"]}


def test_posting_disabled_and_read_only_modes(tmp_path, clock, settings):
    settings.posting_enabled = False
    backend = FakeBackend()
    backend.add("p1", title="q")
    agent = MarketingAgent(settings, backend=backend, store=Store(tmp_path / "a.db", clock=clock), clock=clock)
    d = agent.draft_reply("t3_p1", GOOD_REPLY)
    assert "post it by hand" in d["next_step"]
    with pytest.raises(ValueError, match="turned off"):
        agent.publish_draft(d["draft"]["draft_id"], confirm=True)

    settings.posting_enabled = True
    rss = FakeBackend(can_write=False, metrics=False)
    rss.add("p1", title="q")
    agent2 = MarketingAgent(settings, backend=rss, store=Store(tmp_path / "b.db", clock=clock), clock=clock)
    d2 = agent2.draft_reply("https://www.reddit.com/r/SaaS/comments/p1/q/", GOOD_REPLY)["draft"]
    with pytest.raises(NotSupported):
        agent2.publish_draft(d2["draft_id"], confirm=True)
    logged = agent2.log_manual_post(d2["draft_id"], "https://www.reddit.com/r/SaaS/comments/p1/q/abc12/")
    assert logged["draft"]["status"] == "published"
    assert agent2.status()["limits"]["actions_last_24h"] == 1


def test_terminal_approval_mode(agent, backend, settings):
    settings.approval_mode = "terminal"
    backend.add("p1", title="q")
    d = agent.draft_reply("t3_p1", GOOD_REPLY)["draft"]
    with pytest.raises(ValueError, match="terminal"):
        agent.publish_draft(d["draft_id"], confirm=True)
    agent.approve_draft(d["draft_id"])
    assert agent.publish_draft(d["draft_id"], confirm=True)["published"] is True


def test_promo_banned_subreddit_blocks_mentions(agent, backend, campaign):
    backend.rules = [{"name": "No self-promotion", "description": "Spam and advertising will be removed."}]
    backend.add("p1", title="q")
    d = agent.draft_reply("t3_p1", GOOD_REPLY, campaign="launch")["draft"]
    assert "promo_banned" in {c["code"] for c in d["checks"] if c["level"] == "block"}
    profile = agent.subreddit_profile("SaaS")
    assert profile["self_promotion"]["policy"] == "banned"
    assert profile["rules"][0]["name"] == "No self-promotion"


def test_track_mentions_only_returns_new(agent, backend, campaign):
    backend.add("m1", title="Has anyone tried Invoicely?")
    first = agent.track_mentions(campaign="launch")
    assert first["new_mentions"] == 1
    assert first["terms"] == ["Invoicely", "invoicely.example.com"]
    backend.add("m2", title="Invoicely vs FreshBooks")
    second = agent.track_mentions(campaign="launch")
    assert [m["fullname"] for m in second["mentions"]] == ["t3_m2"]
    assert len(agent.track_mentions(campaign="launch", include_seen=True)["mentions"]) == 2


def test_performance_report_refreshes_metrics(agent, backend, campaign):
    backend.add("p1", title="q")
    d = agent.draft_reply("t3_p1", GOOD_REPLY, campaign="launch")["draft"]
    agent.publish_draft(d["draft_id"], confirm=True)
    backend.posts["t1_c1"] = {"fullname": "t1_c1", "score": 12, "num_comments": 3, "removed": False}
    report = agent.performance_report(campaign="launch")
    assert report["total_actions"] == 1
    assert report["by_subreddit"]["SaaS"]["score"] == 12
    assert report["best"][0]["score"] == 12


def test_status_reports_account_and_limits(agent):
    s = agent.status()
    assert s["mode"] == "oauth-user"
    assert s["account"]["account_age_days"] == 400
    assert s["posting_enabled"] is True


def test_subreddit_insights(agent, backend):
    for i in range(6):
        backend.add(
            f"p{i}", title=f"Question {i}?", score=10 * i, num_comments=i, created_utc=NOW - i * 3600, flair="Help"
        )
    ins = agent.subreddit_insights("SaaS")
    assert ins["question_title_share"] == 1.0
    assert ins["common_flairs"] == ["Help"]
    assert ins["median_score"] == 25
