"""MCP server exposing the marketing agent's tools, prompts and playbook to Claude."""

from __future__ import annotations

import functools
import re
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .config import Settings
from .reddit import RedditError
from .service import MarketingAgent

INSTRUCTIONS = """\
You are operating a Reddit marketing agent for the user. Reddit communities punish spam, so this
toolset is built for *earned* attention: be genuinely useful in conversations where the user's
product is relevant, and be honest about affiliation.

Workflow: reddit_status -> save_campaign -> find_opportunities / track_mentions -> read_thread
-> draft_reply / draft_post -> show the user the exact draft -> publish_draft(confirm=true) only
after the user explicitly approves that specific draft in chat.

Rules you must follow:
- Never publish without the user's explicit approval of the exact text. Never set confirm=true on
  your own initiative.
- Always disclose affiliation when mentioning the product ("I built X", "I work on X").
- Most replies should help without mentioning the product at all (aim for 9 helpful : 1 promo).
- Respect each subreddit's self-promotion rules (subreddit_profile) and never try to evade
  removals, bans, rate limits, or the guardrail checks. Don't suggest alternate accounts or
  vote manipulation.
- Write like a real community member: specific, plain, no marketing hype, no copy-paste.
Read the resource reddit-marketing://playbook for the full playbook.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
LOCAL_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
NET_WRITE_LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
PUBLISH = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)  # fmt: skip

Sub = Annotated[str, Field(description="Subreddit name, with or without r/ (e.g. 'SaaS')")]
TimeFilter = Literal["hour", "day", "week", "month", "year", "all"]


def load_playbook() -> str:
    text = None
    try:
        text = resources.files("reddit_marketing_agent").joinpath("playbook.md").read_text("utf-8")
    except (FileNotFoundError, OSError):
        repo_copy = Path(__file__).resolve().parents[2] / "skills" / "reddit-marketing" / "SKILL.md"
        if repo_copy.is_file():
            text = repo_copy.read_text("utf-8")
    if not text:
        return INSTRUCTIONS
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def _safe(fn):
    """Turn expected failures into ToolErrors so Claude sees the actual message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (RedditError, ValueError, KeyError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(agent: MarketingAgent | None = None, settings: Settings | None = None) -> MCPServer:
    agent = agent or MarketingAgent(settings or Settings.from_env())
    mcp = MCPServer(
        name="reddit-marketing",
        title="Reddit Marketing Agent",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/whyujjwal/reddit-marketing-agent",
    )

    # -- status & research ----------------------------------------------------------------------
    @mcp.tool(annotations=READ)
    @_safe
    def reddit_status() -> dict[str, Any]:
        """Show the connection mode, what's possible in it, posting limits and today's usage, and
        (when logged in) account karma/age. Call this first in a session."""
        return agent.status()

    @mcp.tool(annotations=READ)
    @_safe
    def find_subreddits(
        query: Annotated[str, Field(description="Topic or audience, e.g. 'indie hackers' or 'home espresso'")],
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Discover communities about a topic, with subscriber counts when available."""
        return agent.find_subreddits(query, limit)

    @mcp.tool(annotations=READ)
    @_safe
    def subreddit_profile(subreddit: Sub) -> dict[str, Any]:
        """A subreddit's description, size, full rules, and how it treats self-promotion
        (banned / restricted / unclear). Check this before drafting anything for a community."""
        return agent.subreddit_profile(subreddit)

    @mcp.tool(annotations=READ)
    @_safe
    def subreddit_insights(subreddit: Sub) -> dict[str, Any]:
        """What works in a subreddit, from the past month's top posts: best posting hours (UTC) and
        days, text vs link share, question-title share, common flairs, typical scores."""
        return agent.subreddit_insights(subreddit)

    @mcp.tool(annotations=READ)
    @_safe
    def browse_subreddit(
        subreddit: Sub,
        sort: Literal["hot", "new", "top", "rising", "controversial"] = "hot",
        time_filter: Annotated[TimeFilter, Field(description="Only for sort=top/controversial")] = "day",
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """List posts in a subreddit, to get a feel for the community before joining in."""
        return agent.browse(subreddit, sort, time_filter, limit)

    @mcp.tool(annotations=READ)
    @_safe
    def search_reddit(
        query: Annotated[str, Field(description="Reddit search syntax works: OR, quotes, title:, author:")],
        subreddit: Annotated[
            str | None, Field(description="Restrict to a subreddit; combine several with + (e.g. 'SaaS+startups')")
        ] = None,
        sort: Literal["relevance", "hot", "top", "new", "comments"] = "relevance",
        time_filter: TimeFilter = "month",
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Search Reddit posts."""
        return agent.search(query, subreddit, sort, time_filter, limit)

    @mcp.tool(annotations=READ)
    @_safe
    def read_thread(
        post: Annotated[str, Field(description="Post URL, t3_ fullname, or post id")],
        comment_limit: Annotated[int, Field(ge=1, le=200)] = 40,
    ) -> dict[str, Any]:
        """Read a post and its comments. Always read a thread before drafting a reply to it."""
        return agent.read_thread(post, comment_limit)

    @mcp.tool(annotations=NET_WRITE_LOCAL)
    @_safe
    def find_opportunities(
        campaign: Annotated[
            str | None, Field(description="Campaign name; supplies keywords, competitors and subreddits")
        ] = None,
        keywords: Annotated[
            list[str] | None, Field(description="Problem/solution phrases people would use, e.g. 'invoice software'")
        ] = None,
        subreddits: Annotated[
            list[str] | None, Field(description="Limit to these subreddits (default: campaign's, else all of Reddit)")
        ] = None,
        competitors: Annotated[list[str] | None, Field(description="Competitor product names to watch for")] = None,
        time_filter: TimeFilter = "week",
        min_score: Annotated[int, Field(ge=0, le=100)] = 25,
        max_results: Annotated[int, Field(ge=1, le=50)] = 15,
    ) -> dict[str, Any]:
        """Find recent threads where someone is asking for help, recommendations or alternatives in
        the product's space, ranked 0-100 with reasons and cautions (locked, promo-banned, buried)."""
        return agent.find_opportunities(
            campaign, keywords, subreddits, competitors, time_filter, min_score, max_results
        )

    @mcp.tool(annotations=NET_WRITE_LOCAL)
    @_safe
    def track_mentions(
        campaign: Annotated[str | None, Field(description="Campaign whose product name and domain to watch")] = None,
        terms: Annotated[list[str] | None, Field(description="Explicit terms to watch instead")] = None,
        subreddits: list[str] | None = None,
        include_competitors: bool = False,
        include_seen: Annotated[bool, Field(description="Also return mentions reported before")] = False,
        time_filter: TimeFilter = "week",
    ) -> dict[str, Any]:
        """Find posts that mention the brand (or competitors). Remembers what it has shown you, so
        repeated calls return only new mentions."""
        return agent.track_mentions(terms, campaign, subreddits, include_competitors, include_seen, time_filter)

    # -- campaigns ------------------------------------------------------------------------------
    @mcp.tool(annotations=LOCAL_WRITE)
    @_safe
    def save_campaign(
        name: Annotated[str, Field(description="Short unique name, e.g. 'launch-q4'")],
        product_name: str,
        description: Annotated[str, Field(description="What the product does and for whom, in plain words")],
        product_url: str | None = None,
        audience: str | None = None,
        keywords: Annotated[
            list[str] | None, Field(description="Phrases your audience uses when they have the problem")
        ] = None,
        competitors: list[str] | None = None,
        subreddits: list[str] | None = None,
        tone: Annotated[
            str | None, Field(description="Voice for replies, e.g. 'friendly indie founder, concise'")
        ] = None,
        disclosure: Annotated[
            str | None, Field(description='Preferred disclosure line, e.g. "(I\'m the founder)"')
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a campaign (matched by name). Campaigns hold the product context that
        opportunity search, mention tracking and draft checks use."""
        return agent.save_campaign(
            name=name, product_name=product_name, description=description, product_url=product_url,
            audience=audience, keywords=keywords, competitors=competitors, subreddits=subreddits,
            tone=tone, disclosure=disclosure,
        )  # fmt: skip

    @mcp.tool(annotations=LOCAL_READ)
    @_safe
    def list_campaigns() -> dict[str, Any]:
        """List saved campaigns with their full settings."""
        return agent.list_campaigns()

    # -- drafts ---------------------------------------------------------------------------------
    @mcp.tool(annotations=NET_WRITE_LOCAL)
    @_safe
    def draft_reply(
        target: Annotated[str, Field(description="URL of the post or comment to reply to (or its t3_/t1_ fullname)")],
        body: Annotated[str, Field(description="Reply text in Reddit markdown")],
        campaign: str | None = None,
        rationale: Annotated[
            str | None, Field(description="Why this reply helps the thread (shown to the user)")
        ] = None,
    ) -> dict[str, Any]:
        """Save a reply to the review queue and run guardrail checks (disclosure, subreddit promo
        rules, spam signals, duplicates). Nothing is posted."""
        return agent.draft_reply(target, body, campaign, rationale)

    @mcp.tool(annotations=NET_WRITE_LOCAL)
    @_safe
    def draft_post(
        subreddit: Sub,
        title: str,
        body: Annotated[str | None, Field(description="Text post body (markdown)")] = None,
        url: Annotated[str | None, Field(description="Make it a link post to this URL instead")] = None,
        campaign: str | None = None,
        flair_id: str | None = None,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Save a new post to the review queue and run guardrail checks. Nothing is posted."""
        return agent.draft_post(subreddit, title, body, url, campaign, flair_id, rationale)

    @mcp.tool(annotations=LOCAL_READ)
    @_safe
    def list_drafts(
        status: Literal["draft", "approved", "published", "failed", "discarded", "all"] = "draft",
        campaign: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """List drafts in the review queue with their check results."""
        return agent.list_drafts(status, campaign, limit)

    @mcp.tool(annotations=NET_WRITE_LOCAL)
    @_safe
    def revise_draft(
        draft_id: int,
        body: str | None = None,
        title: str | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """Edit a draft and re-run the checks. Editing resets any approval."""
        return agent.revise_draft(draft_id, body, title, url)

    @mcp.tool(annotations=LOCAL_WRITE)
    @_safe
    def discard_draft(draft_id: int) -> dict[str, Any]:
        """Discard a draft."""
        return agent.discard_draft(draft_id)

    @mcp.tool(annotations=PUBLISH)
    @_safe
    def publish_draft(
        draft_id: int,
        confirm: Annotated[
            bool,
            Field(description="Must be true, and only after the user explicitly approved this exact draft in chat"),
        ] = False,
    ) -> dict[str, Any]:
        """Publish a draft to Reddit. Without confirm=true it returns a final preview and changes
        nothing. Re-checks the thread state, daily limits, pacing and per-subreddit cooldowns, and
        refuses if any blocking check fails."""
        return agent.publish_draft(draft_id, confirm)

    @mcp.tool(annotations=LOCAL_WRITE)
    @_safe
    def log_manual_post(
        draft_id: int,
        permalink: Annotated[str, Field(description="Link to the comment or post the user published by hand")],
    ) -> dict[str, Any]:
        """Record that the user posted a draft by hand, so pacing limits and reports include it."""
        return agent.log_manual_post(draft_id, permalink)

    # -- results --------------------------------------------------------------------------------
    @mcp.tool(annotations=READ)
    @_safe
    def performance_report(
        campaign: str | None = None,
        days: Annotated[int, Field(ge=1, le=365)] = 30,
    ) -> dict[str, Any]:
        """How published posts and replies are doing: scores, replies, removals, by subreddit."""
        return agent.performance_report(campaign, days)

    @mcp.tool(annotations=READ)
    @_safe
    def check_inbox(unread_only: bool = True, limit: Annotated[int, Field(ge=1, le=100)] = 25) -> dict[str, Any]:
        """Replies to your posts/comments and direct messages, so you can keep conversations going.
        Needs a logged-in account."""
        return agent.inbox(unread_only, limit)

    # -- playbook & prompts ---------------------------------------------------------------------
    @mcp.resource(
        "reddit-marketing://playbook",
        name="playbook",
        title="Reddit marketing playbook",
        description="How to market on Reddit without getting banned: principles, reply patterns, and the agent's workflow.",
        mime_type="text/markdown",
    )
    def playbook() -> str:
        return load_playbook()

    @mcp.prompt(title="Set up a campaign")
    def setup_campaign(product: str) -> str:
        """Interview me about a product and save a campaign for it."""
        return (
            f"Help me set up a Reddit marketing campaign for: {product}\n\n"
            "1. Call reddit_status to see what mode we're in.\n"
            "2. Ask me (briefly, a few questions at a time) what it does, who it's for, the problems "
            "it solves in my audience's own words, competitors, and my role (for disclosure).\n"
            "3. Use find_subreddits and subreddit_profile to propose 5-10 communities where my audience "
            "spends time, noting each one's self-promotion policy.\n"
            "4. Save it with save_campaign, then summarise the plan."
        )

    @mcp.prompt(title="Daily scout")
    def daily_scout(campaign: str) -> str:
        """Find today's best conversations and draft replies for my review."""
        return (
            f"Run the daily Reddit scout for campaign '{campaign}'.\n\n"
            "1. reddit_status, then find_opportunities for the campaign (time_filter=day; widen to week "
            "if there are few results) and track_mentions.\n"
            "2. read_thread on the top ~5 opportunities. Skip any where we can't genuinely help.\n"
            "3. Draft at most 3 replies with draft_reply. Most should help without mentioning the product; "
            "only mention it where it directly answers the question, with disclosure.\n"
            "4. Show me each draft verbatim with the thread link, why it helps, and any checks. Don't "
            "publish anything until I approve a specific draft."
        )

    @mcp.prompt(title="Weekly report")
    def weekly_report(campaign: str) -> str:
        """Summarise how the campaign did and what to do next week."""
        return (
            f"Write the weekly Reddit report for campaign '{campaign}'.\n\n"
            "Use performance_report (days=7), check_inbox and track_mentions. Cover: what was published, "
            "how it performed, removals and likely reasons, unanswered replies worth answering (offer "
            "drafts), new mentions, and 3 concrete suggestions for next week. Keep the promo ratio in view."
        )

    return mcp
