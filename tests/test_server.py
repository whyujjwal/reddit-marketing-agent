"""MCP protocol tests: in-process client, plus a real stdio subprocess."""

from __future__ import annotations

import json
import os
import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters

from reddit_marketing_agent.server import build_server, load_playbook

EXPECTED_TOOLS = {
    "reddit_status", "find_subreddits", "subreddit_profile", "subreddit_insights",
    "browse_subreddit", "search_reddit", "read_thread", "find_opportunities", "track_mentions",
    "save_campaign", "list_campaigns", "draft_reply", "draft_post", "list_drafts", "revise_draft",
    "discard_draft", "publish_draft", "log_manual_post", "performance_report", "check_inbox",
}  # fmt: skip


def _data(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


def test_tools_prompts_and_playbook(agent, backend):
    server = build_server(agent)

    async def run():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == EXPECTED_TOOLS
            assert tools["publish_draft"].annotations.destructive_hint is True
            assert tools["search_reddit"].annotations.read_only_hint is True
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert prompts == {"setup_campaign", "daily_scout", "weekly_report"}
            res = await client.read_resource("reddit-marketing://playbook")
            assert "Reddit Marketing Playbook" in res.contents[0].text
            assert "explicit approval" in (client.instructions or "")

            backend.add("p1", title="Any tools for invoices?")
            saved = await client.call_tool(
                "save_campaign", {"name": "c", "product_name": "Invoicely", "description": "d"}
            )
            assert not saved.is_error
            draft = await client.call_tool(
                "draft_reply", {"target": "t3_p1", "body": "Try Invoicely! " * 3, "campaign": "c"}
            )
            d = _data(draft)["draft"]
            assert d["blocked"] is True
            publish = await client.call_tool("publish_draft", {"draft_id": d["draft_id"], "confirm": True})
            assert _data(publish)["published"] is False
            missing = await client.call_tool("publish_draft", {"draft_id": 999})
            assert missing.is_error and "No draft with id 999" in missing.content[0].text

    anyio.run(run)


def test_playbook_has_no_frontmatter():
    text = load_playbook()
    assert text.startswith("# Reddit Marketing Playbook")


@pytest.mark.skipif(sys.platform == "win32", reason="stdio smoke test uses POSIX paths")
def test_stdio_server_starts(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "reddit_marketing_agent"],
        env={**os.environ, "RMA_DATA_DIR": str(tmp_path), "REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": ""},
    )

    async def run():
        async with Client(params) as client:
            tools = await client.list_tools()
            assert len(tools.tools) == len(EXPECTED_TOOLS)
            status = _data(await client.call_tool("reddit_status", {}))
            assert status["mode"] == "rss"
            assert status["posting_enabled"] is False

    anyio.run(run)
