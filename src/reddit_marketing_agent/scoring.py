"""Opportunity scoring: which threads are worth a genuinely helpful reply?

The score (0-100) rewards threads where someone is actively asking for help or recommendations
in your space, that are fresh enough to be seen, and that aren't locked, archived or buried.
It's a heuristic to rank where Claude should read closely, not a verdict.
"""

from __future__ import annotations

import re
import time
from typing import Any

INTENT_PATTERNS: list[tuple[str, int, str]] = [
    (r"\balternatives?\s+(to|for)\b", 22, "asking for alternatives"),
    (r"\b(looking for|searching for|in search of)\b", 18, "actively looking"),
    (r"\b(recommend|recommendations?|suggestions?)\b", 16, "asking for recommendations"),
    (r"\b(what|which) (tool|app|service|software|platform|product)s?\b", 16, "asking which tool"),
    (r"\b(best|good|top) (tool|app|way|service|software|platform)s?\b", 12, "asking for the best option"),
    (r"\bwhat do (you|y'all|people) use\b", 16, "asking what people use"),
    (r"\b(anyone|anybody) (know|tried|use|using)\b", 12, "asking for experiences"),
    (r"\b(is there|are there) (a|an|any)\b", 12, "asking if something exists"),
    (r"\bhow (do|can|should) (i|we|you)\b", 8, "asking how-to"),
    (r"\b(struggling|frustrated|tired of|fed up|pain in the)\b", 10, "describing a pain point"),
    (r"\b(switch(ing)? from|replace|replacing|moving away from|migrat(e|ing) from)\b", 14, "considering a switch"),
    (r"\b(worth it|vs\.?|versus|compared to)\b", 8, "comparing options"),
    (r"\b(help|advice)\b", 5, "asking for help"),
]  # fmt: skip


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def score_post(
    post: dict[str, Any],
    *,
    keywords: list[str] | None = None,
    competitors: list[str] | None = None,
    own_username: str | None = None,
    promo_policy: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now = now or time.time()
    title = (post.get("title") or "").lower()
    body = (post.get("selftext") or "").lower()
    text = f"{title}\n{body}"
    reasons: list[str] = []
    penalties: list[str] = []
    score = 0.0

    # Hard stops: you can't usefully join these.
    if post.get("locked"):
        return _result(post, 0, [], ["thread is locked"])
    if post.get("archived"):
        return _result(post, 0, [], ["thread is archived"])
    if post.get("removed"):
        return _result(post, 0, [], ["post was removed"])
    if own_username and (post.get("author") or "").lower() == own_username.lower():
        return _result(post, 0, [], ["this is your own post"])

    # Intent (max ~40)
    intent = 0
    for pattern, weight, label in INTENT_PATTERNS:
        if re.search(pattern, text):
            intent += weight
            reasons.append(label)
    if title.rstrip().endswith("?"):
        intent += 8
        reasons.append("title is a question")
    score += min(intent, 40)

    # Relevance (max ~35)
    kw_hits_title = [k for k in keywords or [] if _contains(title, k.lower())]
    kw_hits_body = [k for k in keywords or [] if k not in kw_hits_title and _contains(body, k.lower())]
    comp_hits = [c for c in competitors or [] if _contains(text, c.lower())]
    relevance = 12 * len(kw_hits_title) + 6 * len(kw_hits_body) + 14 * len(comp_hits)
    if kw_hits_title or kw_hits_body:
        reasons.append("mentions " + ", ".join(kw_hits_title + kw_hits_body))
    if comp_hits:
        reasons.append("mentions competitor " + ", ".join(comp_hits))
    score += min(relevance, 35)

    # Freshness (max 15)
    created = post.get("created_utc")
    if created:
        age_h = max(0.0, (now - float(created)) / 3600)
        if age_h <= 6:
            score += 15
            reasons.append("posted in the last 6h")
        elif age_h <= 24:
            score += 12
            reasons.append("posted today")
        elif age_h <= 72:
            score += 7
        elif age_h <= 24 * 7:
            score += 3
        else:
            penalties.append(f"{int(age_h // 24)} days old; few people will see a reply")
            score -= 5

    # Visibility (max 10): some traction but not a 500-comment pile-on
    n = post.get("num_comments")
    if n is not None:
        if n == 0:
            score += 6
            reasons.append("no replies yet; you could be first")
        elif n <= 25:
            score += 10
        elif n <= 100:
            score += 5
        else:
            penalties.append(f"{n} comments already; a new reply will be buried")
    s = post.get("score")
    if s is not None and s < 0:
        penalties.append("post is downvoted")
        score -= 5

    if post.get("stickied"):
        penalties.append("stickied/megathread: check the rules for promo threads")
        score -= 5
    if post.get("over_18"):
        penalties.append("NSFW")
        score -= 15
    if promo_policy == "banned":
        penalties.append("subreddit bans self-promotion: help only, don't mention your product")
        score -= 10
    elif promo_policy == "restricted":
        penalties.append("subreddit restricts self-promotion: read the rules first")

    return _result(post, score, reasons, penalties)


def _result(post: dict[str, Any], score: float, reasons: list[str], penalties: list[str]) -> dict[str, Any]:
    return {
        "score": int(max(0, min(100, round(score)))),
        "reasons": reasons,
        "cautions": penalties,
    }


def build_search_query(terms: list[str], max_len: int = 480) -> str:
    """Combine terms into one Reddit OR query (quoted phrases), staying under Reddit's limit."""
    parts: list[str] = []
    for term in terms:
        term = term.strip().replace('"', "")
        if not term:
            continue
        piece = f'"{term}"' if " " in term else term
        candidate = " OR ".join(parts + [piece])
        if len(candidate) > max_len:
            break
        parts.append(piece)
    return " OR ".join(parts)
