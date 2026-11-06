"""Command line: run the MCP server, check setup, and review drafts from a terminal."""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

from . import __version__
from .config import Settings

ENV_TEMPLATE = """\
# Reddit Marketing Agent configuration.
# Leave the Reddit values empty to run in read-only RSS mode (no setup needed).

# --- Reddit API app (https://www.reddit.com/prefs/apps, type "script") ---
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=

# --- Reddit login, only needed for publishing / inbox / account stats ---
REDDIT_USERNAME=
REDDIT_PASSWORD=
# Accounts with 2FA: use a refresh token instead of a password
REDDIT_REFRESH_TOKEN=

# --- Safety rails ---
# Publishing stays off until you set this to true.
RMA_POSTING_ENABLED=false
# chat = approve drafts in Claude's chat; terminal = only `reddit-marketing-agent approve <id>`
RMA_APPROVAL_MODE=chat
RMA_MAX_ACTIONS_PER_DAY=5
RMA_MIN_MINUTES_BETWEEN_ACTIONS=10
RMA_SUBREDDIT_COOLDOWN_HOURS=72
RMA_MAX_PROMO_RATIO=0.10
"""


def _agent():
    from .service import MarketingAgent

    return MarketingAgent(Settings.from_env())


def cmd_serve(_: argparse.Namespace) -> int:
    from .server import build_server

    build_server().run("stdio")
    return 0


def cmd_init(_: argparse.Namespace) -> int:
    settings = Settings.from_env(load_files=False)
    path = settings.data_dir / ".env"
    if path.exists():
        print(f"Config already exists: {path}")
        return 0
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(ENV_TEMPLATE)
    os.chmod(path, 0o600)
    print(f"Wrote {path}\nEdit it to add Reddit credentials (optional), then run: reddit-marketing-agent doctor")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    from .reddit import RedditError

    agent = _agent()
    s = agent.settings
    print(f"reddit-marketing-agent v{__version__}")
    print(f"  data dir:       {s.data_dir}")
    print(f"  mode:           {agent.backend.name}")
    print(f"  user agent:     {s.effective_user_agent}")
    print(f"  posting:        {'ENABLED' if s.posting_enabled else 'off'} (approval: {s.approval_mode})")
    print("  checking Reddit connection…")
    try:
        posts = agent.backend.listing("all", sort="hot", limit=3)
        print(f"  ✓ read OK ({len(posts)} posts from r/all)")
    except RedditError as exc:
        print(f"  ✗ read failed: {exc}")
        return 1
    if agent.backend.can_write:
        try:
            me = agent.backend.me()
            print(f"  ✓ logged in as u/{me['username']} ({me.get('total_karma')} karma)")
        except RedditError as exc:
            print(f"  ✗ login failed: {exc}")
            return 1
    return 0


def _print_draft(d: dict) -> None:
    head = f"#{d['draft_id']} [{d['status']}] {d['kind']} in r/{d['subreddit']}"
    if d.get("promotional"):
        head += "  (promotional)"
    print(head)
    if d.get("target"):
        print(f"   replying to: {d.get('replying_to') or ''} {d['target']}")
    if d.get("title"):
        print(f"   title: {d['title']}")
    if d.get("url"):
        print(f"   url: {d['url']}")
    if d.get("body"):
        print(textwrap.indent(textwrap.fill(d["body"], 96, replace_whitespace=False), "   | "))
    for c in d.get("checks", []):
        print(f"   [{c['level']}] {c['message']}")
    print()


def cmd_drafts(args: argparse.Namespace) -> int:
    drafts = _agent().list_drafts(args.status, None, args.limit)["drafts"]
    if not drafts:
        print("No drafts.")
    for d in drafts:
        _print_draft(d)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    agent = _agent()
    view = agent.list_drafts("all", None, 1000)["drafts"]
    match = next((d for d in view if d["draft_id"] == args.draft_id), None)
    if not match:
        print(f"No draft #{args.draft_id}")
        return 1
    _print_draft(match)
    if not args.yes:
        answer = input("Approve this draft for publishing? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Not approved.")
            return 1
    try:
        agent.approve_draft(args.draft_id)
    except ValueError as exc:
        print(f"Can't approve: {exc}")
        return 1
    print(f"Approved #{args.draft_id}. Claude can now publish it with publish_draft.")
    return 0


def cmd_discard(args: argparse.Namespace) -> int:
    print(json.dumps(_agent().discard_draft(args.draft_id)["draft"], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reddit-marketing-agent",
        description="MCP server that lets Claude run careful, human-approved Reddit marketing.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server over stdio (default)").set_defaults(fn=cmd_serve)
    sub.add_parser("init", help=f"Create a config file in {Path('~/.reddit-marketing-agent')}").set_defaults(
        fn=cmd_init
    )
    sub.add_parser("doctor", help="Check configuration and Reddit connectivity").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("drafts", help="List drafts in the review queue")
    p.add_argument(
        "--status", default="draft", choices=["draft", "approved", "published", "failed", "discarded", "all"]
    )
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_drafts)
    p = sub.add_parser("approve", help="Approve a draft for publishing")
    p.add_argument("draft_id", type=int)
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("discard", help="Discard a draft")
    p.add_argument("draft_id", type=int)
    p.set_defaults(fn=cmd_discard)

    args = parser.parse_args(argv)
    fn = getattr(args, "fn", cmd_serve)
    try:
        return fn(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
