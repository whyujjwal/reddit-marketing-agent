"""Guardrails that keep the agent on the right side of Reddit's rules and community norms.

Two jobs:

* :func:`analyze_promo_policy` reads a subreddit's rules and classifies how it treats
  self-promotion.
* :func:`check_draft` / :func:`check_publish_limits` review a draft before it goes out. Checks at
  level ``block`` stop publishing; ``warn`` and ``info`` are advice for the human reviewing it.

These are deliberately conservative. Reddit bans accounts (and sometimes whole domains) for
undisclosed promotion, spammy links, and repetitive posting, and that's the failure mode this
tool exists to avoid.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

BAN_PATTERNS = [
    r"\bno (self[- ]?promo(tion)?|promotion|advertis\w*|spam|marketing)\b",
    r"\bself[- ]?promo(tion)? (is )?(not allowed|prohibited|banned|forbidden)\b",
    r"\b(not allowed|prohibited|banned)[^.]{0,40}self[- ]?promo",
    r"\bdo not (promote|advertise|self[- ]?promote)\b",
    r"\bno (links to|plugging) your own\b",
    r"\bno (affiliate|referral) links\b",
]
RESTRICT_PATTERNS = [
    r"self[- ]?promo",
    r"\bpromot(e|ion|ional)\b",
    r"\badvertis",
    r"\b(9:1|10:1|10%|90/10)\b",
    r"\bmegathread\b",
    r"\b(weekly|monthly|daily) (thread|post)\b.*\b(share|promo|showcase)",
    r"\bspam\b",
]

DISCLOSURE_PATTERNS = [
    r"\b(i|we) (made|built|created|developed|launched|run|own|founded|started|work (on|at|for))\b",
    r"\b(i'?m|i am) (the |a |one of the )?(founder|co-?founder|creator|maker|developer|dev|author|ceo|cto|owner)\b",
    r"\bmy (own )?(app|tool|product|startup|company|project|side project|saas|site|website)\b",
    r"\bour (app|tool|product|startup|company|team|project|saas)\b",
    r"\b(full )?disclosure\b",
    r"\b(affiliated|affiliation)\b",
    r"\bdisclaimer\b",
]  # fmt: skip

SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at"}  # fmt: skip
_URL_RE = re.compile(r"https?://[^\s)\]>]+", re.I)


def analyze_promo_policy(rules: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
    """Classify a subreddit's stance on self-promotion: ``banned``, ``restricted``, or ``unclear``."""
    banned, restricted = [], []
    for rule in rules:
        text = f"{rule.get('name', '')}. {rule.get('description', '')}".lower()
        if any(re.search(p, text) for p in BAN_PATTERNS):
            banned.append(rule.get("name"))
        elif any(re.search(p, text) for p in RESTRICT_PATTERNS):
            restricted.append(rule.get("name"))
    desc = description.lower()
    if not banned and any(re.search(p, desc) for p in BAN_PATTERNS):
        banned.append("(sidebar description)")
    if banned:
        policy = "banned"
        advice = (
            "Self-promotion is not allowed. Only post genuinely helpful answers with no product "
            "mention or link. If you think your product truly answers a question, message the "
            "mods first."
        )
    elif restricted:
        policy = "restricted"
        advice = (
            "Promotion is limited (ratios, megathreads, or flair). Read these rules in full "
            "before mentioning your product, and always disclose your affiliation."
        )
    else:
        policy = "unclear"
        advice = (
            "No explicit self-promotion rule found. Reddit's site-wide norms still apply: "
            "contribute far more than you promote, and disclose your affiliation."
        )
    return {
        "policy": policy,
        "rules_banning": banned,
        "rules_restricting": restricted,
        "advice": advice,
    }


def body_hash(text: str) -> str:
    normalized = re.sub(r"\W+", " ", (text or "").lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def mentions_product(text: str, campaign: dict[str, Any] | None) -> bool:
    if not campaign:
        return False
    lowered = text.lower()
    needles = [campaign.get("product_name") or ""]
    url = campaign.get("product_url") or ""
    if url:
        domain = re.sub(r"^https?://(www\.)?", "", url.lower()).split("/")[0]
        needles.append(domain)
    return any(n and re.search(r"(?<![a-z0-9])" + re.escape(n.lower()) + r"(?![a-z0-9])", lowered) for n in needles)  # fmt: skip


def has_disclosure(text: str, campaign: dict[str, Any] | None = None) -> bool:
    lowered = text.lower()
    custom = (campaign or {}).get("disclosure")
    if custom and custom.lower().strip() in lowered:
        return True
    return any(re.search(p, lowered) for p in DISCLOSURE_PATTERNS)


def _check(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def check_draft(
    *,
    kind: str,
    body: str,
    title: str | None,
    url: str | None,
    campaign: dict[str, Any] | None,
    promo_policy: str | None,
    target: dict[str, Any] | None = None,
    recent_hashes: set[str] | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """Review draft content. Returns ``(checks, is_promotional)``."""
    checks: list[dict[str, str]] = []
    full_text = "\n".join(x for x in (title, body, url) if x)
    promotional = mentions_product(full_text, campaign)
    links = _URL_RE.findall(full_text)

    if target:
        if target.get("locked"):
            checks.append(_check("block", "locked", "The target thread is locked."))
        if target.get("archived"):
            checks.append(_check("block", "archived", "The target thread is archived."))
        if target.get("removed"):
            checks.append(_check("block", "removed", "The target post was removed."))

    if promotional:
        if not has_disclosure(full_text, campaign):
            checks.append(
                _check(
                    "block",
                    "no_disclosure",
                    "Mentions your product without saying you're affiliated with it. Add a plain "
                    'disclosure such as "(I built this)" or "full disclosure: I work on X".',
                )
            )
        if promo_policy == "banned":
            checks.append(
                _check(
                    "block",
                    "promo_banned",
                    "This subreddit's rules ban self-promotion. Remove the product mention and "
                    "link, or ask the moderators first.",
                )
            )
        elif promo_policy == "restricted":
            checks.append(
                _check(
                    "warn",
                    "promo_restricted",
                    "This subreddit limits self-promotion. Make sure this complies (ratio rules, "
                    "required flair, megathread-only).",
                )
            )
        elif promo_policy is None:
            checks.append(
                _check(
                    "warn",
                    "rules_unchecked",
                    "Couldn't check the subreddit rules (needs API credentials). Read them yourself before publishing.",
                )
            )

    for link in links:
        domain = re.sub(r"^https?://(www\.)?", "", link.lower()).split("/")[0]
        if domain in SHORTENERS:
            checks.append(_check("block", "shortener", f"Link shorteners ({domain}) are treated as spam on Reddit."))
    if len(links) > 2:
        checks.append(_check("warn", "many_links", f"{len(links)} links; more than 1-2 reads as spam."))
    if promotional and kind == "comment" and len(links) > 1:
        checks.append(_check("warn", "multi_promo_links", "Keep promotional comments to one link at most."))

    words = len(re.findall(r"\w+", body or ""))
    if kind == "comment" and words < 25:
        checks.append(
            _check("warn", "too_short", "Very short reply. Low-effort comments get downvoted, more so if promotional.")
        )
    if promotional and words < 60:
        checks.append(
            _check(
                "info",
                "lead_with_value",
                "Lead with a genuinely useful answer; the product mention should be a small part.",
            )
        )
    if title:
        letters = [c for c in title if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6 and len(letters) > 8:
            checks.append(_check("warn", "shouting", "Title is mostly caps."))
        if title.count("!") >= 2:
            checks.append(_check("warn", "hype", "Multiple exclamation marks read as marketing hype."))
        if len(title) > 300:
            checks.append(_check("block", "title_length", "Reddit titles are limited to 300 characters."))
    if body and len(body) > 10000 and kind == "comment":
        checks.append(_check("block", "too_long", "Comments are limited to 10,000 characters."))
    if recent_hashes and body and body_hash(body) in recent_hashes:
        checks.append(
            _check(
                "block",
                "duplicate",
                "You've already published this exact text. Reddit flags copy-paste posting as spam.",
            )
        )
    return checks, promotional


def check_publish_limits(
    *,
    activity: list[dict[str, Any]],
    subreddit: str,
    promotional: bool,
    max_per_day: int,
    min_minutes_between: int,
    promo_cooldown_hours: int,
    max_promo_ratio: float,
    now: float | None = None,
) -> list[dict[str, str]]:
    """Pace publishing. ``activity`` should cover at least the last 30 days."""
    now = now or time.time()
    checks: list[dict[str, str]] = []
    day = [a for a in activity if a["created_at"] >= now - 86400]
    if len(day) >= max_per_day:
        checks.append(
            _check("block", "daily_limit", f"Daily limit reached ({max_per_day} actions in 24h). Try tomorrow.")
        )
    if activity:
        last = max(a["created_at"] for a in activity)
        mins = (now - last) / 60
        if mins < min_minutes_between:
            checks.append(
                _check(
                    "block",
                    "too_fast",
                    f"Last action was {mins:.0f} min ago; wait at least {min_minutes_between} min between actions.",
                )
            )
    if promotional:
        cutoff = now - promo_cooldown_hours * 3600
        same_sub = [
            a for a in activity
            if a["promotional"] and a["subreddit"].lower() == subreddit.lower() and a["created_at"] >= cutoff
        ]  # fmt: skip
        if same_sub:
            checks.append(
                _check(
                    "block",
                    "subreddit_cooldown",
                    f"You promoted in r/{subreddit} within the last {promo_cooldown_hours}h. Space it out.",
                )
            )
        total = len(activity) + 1
        promo = sum(1 for a in activity if a["promotional"]) + 1
        ratio = promo / total
        if total >= 5 and ratio > max_promo_ratio:
            checks.append(
                _check(
                    "warn",
                    "promo_ratio",
                    f"{promo}/{total} of your logged actions in 30 days are promotional "
                    f"({ratio:.0%}). Aim for no more than {max_promo_ratio:.0%}: answer questions "
                    "and join discussions without mentioning your product too.",
                )
            )
    return checks


def blocking(checks: list[dict[str, str]]) -> list[dict[str, str]]:
    return [c for c in checks if c["level"] == "block"]
