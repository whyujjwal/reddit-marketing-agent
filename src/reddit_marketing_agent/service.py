"""The agent's core logic. The MCP server and the CLI are thin layers over :class:`MarketingAgent`."""

from __future__ import annotations

import hashlib
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import guardrails as gr
from .config import Settings
from .reddit import NotSupported, RedditBackend, RedditError, make_backend
from .reddit.models import clean_subreddit, parse_thing_ref, truncate
from .scoring import build_search_query, score_post
from .store import Store

DAY = 86400
_SUB_IN_URL = re.compile(r"/r/([A-Za-z0-9_]+)", re.I)
_POST_IN_URL = re.compile(r"/comments/([a-z0-9]+)", re.I)


def _age(created: float | None, now: float) -> str | None:
    if not created:
        return None
    h = (now - float(created)) / 3600
    if h < 1:
        return f"{max(1, int(h * 60))}m ago"
    if h < 48:
        return f"{int(h)}h ago"
    return f"{int(h // 24)}d ago"


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], False)}


class MarketingAgent:
    def __init__(
        self,
        settings: Settings,
        backend: RedditBackend | None = None,
        store: Store | None = None,
        clock=time.time,
    ) -> None:
        self.settings = settings
        self.backend = backend or make_backend(settings)
        self.store = store or Store(settings.db_path)
        self.clock = clock
        self._policy_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    # -- presentation helpers -------------------------------------------------------------------
    def _post_view(self, p: dict[str, Any], body_chars: int = 500) -> dict[str, Any]:
        now = self.clock()
        return _compact(
            {
                "fullname": p.get("fullname"),
                "subreddit": p.get("subreddit"),
                "title": p.get("title"),
                "author": p.get("author"),
                "age": _age(p.get("created_utc"), now),
                "score": p.get("score"),
                "num_comments": p.get("num_comments"),
                "flair": p.get("flair"),
                "permalink": p.get("permalink"),
                "link_url": None if p.get("is_self") in (True, None) else p.get("url"),
                "text": truncate(p.get("selftext"), body_chars),
                "locked": p.get("locked"),
                "archived": p.get("archived"),
                "removed": p.get("removed"),
                "nsfw": p.get("over_18"),
            }
        )

    def _comment_view(self, c: dict[str, Any], body_chars: int = 1200) -> dict[str, Any]:
        return _compact(
            {
                "fullname": c.get("fullname"),
                "author": c.get("author"),
                "depth": c.get("depth"),
                "score": c.get("score"),
                "age": _age(c.get("created_utc"), self.clock()),
                "text": truncate(c.get("body"), body_chars),
                "op": c.get("is_submitter"),
                "permalink": c.get("permalink"),
            }
        )

    def _campaign(self, ref: str | None) -> dict[str, Any] | None:
        if not ref:
            return None
        campaign = self.store.get_campaign(ref)
        if campaign is None:
            names = ", ".join(c["name"] for c in self.store.list_campaigns()) or "none yet"
            raise ValueError(f"No campaign named {ref!r}. Existing campaigns: {names}")
        return campaign

    def _promo_policy(self, subreddit: str) -> dict[str, Any] | None:
        key = subreddit.lower()
        hit = self._policy_cache.get(key)
        if hit and self.clock() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            rules = self.backend.subreddit_rules(subreddit)
            about = {}
            try:
                about = self.backend.subreddit_about(subreddit)
            except NotSupported:
                pass
            policy = gr.analyze_promo_policy(rules, about.get("description", ""))
            policy["rules"] = rules
        except NotSupported:
            policy = None
        self._policy_cache[key] = (self.clock(), policy)
        return policy

    # -- status ---------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        s = self.settings
        now = self.clock()
        activity = self.store.activity_since(now - 30 * DAY)
        today = [a for a in activity if a["created_at"] >= now - DAY]
        out: dict[str, Any] = {
            "mode": self.backend.name,
            "mode_explained": {
                "rss": "No API credentials: read-only research via public RSS feeds (no scores, "
                "no rules, slow rate limit). Drafts work; post them yourself and log them.",
                "oauth-app": "Reddit API app credentials: full read access (scores, rules, stats). "
                "Add a Reddit login to enable posting.",
                "oauth-user": "Logged in to Reddit: full read access plus posting (if enabled).",
            }.get(self.backend.name, ""),
            "capabilities": self.backend.capabilities(),
            "posting_enabled": s.posting_enabled and self.backend.can_write,
            "approval_mode": s.approval_mode,
            "limits": {
                "max_actions_per_day": s.max_actions_per_day,
                "actions_last_24h": len(today),
                "min_minutes_between_actions": s.min_minutes_between_actions,
                "subreddit_promo_cooldown_hours": s.subreddit_promo_cooldown_hours,
                "max_promo_ratio": s.max_promo_ratio,
            },
            "activity_last_30d": {
                "total": len(activity),
                "promotional": sum(a["promotional"] for a in activity),
            },
            "drafts_pending": len(self.store.list_drafts("draft")) + len(self.store.list_drafts("approved")),
            "campaigns": [c["name"] for c in self.store.list_campaigns()],
            "data_dir": str(s.data_dir),
        }
        if not s.posting_enabled:
            out["posting_note"] = (
                "Publishing is off (RMA_POSTING_ENABLED is not true). Drafts can still be "
                "created, reviewed, and posted by hand."
            )
        if self.backend.can_write:
            try:
                me = self.backend.me()
                age_days = int((now - (me.get("created_utc") or now)) / DAY)
                me["account_age_days"] = age_days
                out["account"] = me
                tips = []
                if age_days < 30:
                    tips.append("Account is under 30 days old; many subreddits auto-remove new accounts' posts.")
                if (me.get("total_karma") or 0) < 100:
                    tips.append("Low karma: build it with helpful, non-promotional comments before promoting.")
                if tips:
                    out["account_tips"] = tips
            except RedditError as exc:
                out["account_error"] = str(exc)
        return out

    # -- research -------------------------------------------------------------------------------
    def find_subreddits(self, query: str, limit: int = 10) -> dict[str, Any]:
        subs = self.backend.search_subreddits(query, limit=limit)
        return {"query": query, "subreddits": [_compact(s) for s in subs]}

    def subreddit_profile(self, subreddit: str) -> dict[str, Any]:
        name = clean_subreddit(subreddit)
        out: dict[str, Any] = {"subreddit": name}
        try:
            out["about"] = _compact(self.backend.subreddit_about(name))
        except NotSupported as exc:
            out["about_unavailable"] = str(exc)
        policy = self._promo_policy(name)
        if policy is None:
            out["rules_unavailable"] = (
                "Rules need API credentials in this mode. Read them at "
                f"https://www.reddit.com/r/{name}/about/rules before posting."
            )
        else:
            out["rules"] = policy.get("rules", [])
            out["self_promotion"] = {k: v for k, v in policy.items() if k != "rules"}
        return out

    def subreddit_insights(self, subreddit: str) -> dict[str, Any]:
        name = clean_subreddit(subreddit)
        posts = self.backend.listing(name, sort="top", time_filter="month", limit=100)
        if not posts:
            return {"subreddit": name, "note": "No posts found in the last month."}
        metrics = self.backend.has_metrics
        hours, days = Counter(), Counter()
        for p in posts:
            if not p.get("created_utc"):
                continue
            dt = datetime.fromtimestamp(float(p["created_utc"]), tz=timezone.utc)
            weight = max(1, p.get("score") or 1) if metrics else 1
            hours[dt.hour] += weight
            days[dt.strftime("%A")] += weight
        titles = [p.get("title") or "" for p in posts]
        out: dict[str, Any] = {
            "subreddit": name,
            "sample": f"top {len(posts)} posts of the past month",
            "best_hours_utc": [h for h, _ in hours.most_common(4)],
            "best_days": [d for d, _ in days.most_common(3)],
            "text_post_share": round(sum(1 for p in posts if p.get("is_self")) / len(posts), 2),
            "question_title_share": round(sum(t.strip().endswith("?") for t in titles) / len(titles), 2),
            "median_title_length": int(statistics.median(len(t) for t in titles)),
            "common_flairs": [f for f, _ in Counter(p["flair"] for p in posts if p.get("flair")).most_common(6)],
            "top_posts": [
                _compact({"title": p.get("title"), "score": p.get("score"), "num_comments": p.get("num_comments"), "permalink": p.get("permalink")})
                for p in posts[:8]
            ],
        }  # fmt: skip
        if metrics:
            scores = [p.get("score") or 0 for p in posts]
            comments = [p.get("num_comments") or 0 for p in posts]
            out["median_score"] = int(statistics.median(scores))
            out["median_comments"] = int(statistics.median(comments))
            out["weighting"] = "hours/days weighted by post score"
        else:
            out["weighting"] = "hours/days by post count (scores unavailable in RSS mode)"
        return out

    def browse(self, subreddit: str, sort: str = "hot", time_filter: str = "day", limit: int = 25) -> dict[str, Any]:
        posts = self.backend.listing(subreddit, sort=sort, time_filter=time_filter, limit=limit)
        return {
            "subreddit": clean_subreddit(subreddit),
            "sort": sort,
            "posts": [self._post_view(p, 300) for p in posts],
        }

    def search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "month",
        limit: int = 25,
    ) -> dict[str, Any]:
        posts = self.backend.search(query, subreddit=subreddit, sort=sort, time_filter=time_filter, limit=limit)
        return {"query": query, "subreddit": subreddit, "posts": [self._post_view(p, 300) for p in posts]}

    def read_thread(self, post: str, comment_limit: int = 40) -> dict[str, Any]:
        prefix, ident = parse_thing_ref(post)
        if prefix != "t3_":
            m = _POST_IN_URL.search(post)
            if not m:
                raise ValueError("Pass the post URL (or t3_ id) to read a thread.")
            ident = m.group(1)
        p, comments = self.backend.thread(ident, comment_limit=comment_limit)
        out = {
            "post": self._post_view(p, 4000),
            "comments": [self._comment_view(c) for c in comments],
        }
        if not self.backend.has_metrics:
            out["note"] = "RSS mode: comment scores and nesting aren't available."
        return out

    # -- opportunities --------------------------------------------------------------------------
    def find_opportunities(
        self,
        campaign: str | None = None,
        keywords: list[str] | None = None,
        subreddits: list[str] | None = None,
        competitors: list[str] | None = None,
        time_filter: str = "week",
        min_score: int = 25,
        max_results: int = 15,
    ) -> dict[str, Any]:
        c = self._campaign(campaign)
        keywords = list(keywords or (c["keywords"] if c else []))
        competitors = list(competitors or (c["competitors"] if c else []))
        subreddits = [clean_subreddit(s) for s in (subreddits or (c["subreddits"] if c else []))]
        terms = keywords + competitors
        if not terms:
            raise ValueError("Give keywords (or a campaign with keywords) to search for.")
        query = build_search_query(terms)

        groups: list[str | None] = [None]
        if subreddits:
            size = 10
            groups = ["+".join(subreddits[i : i + size]) for i in range(0, len(subreddits), size)]
        sorts = ["new", "relevance"] if self.backend.has_metrics else ["new"]
        max_requests = 6 if self.backend.has_metrics else 2

        found: dict[str, dict[str, Any]] = {}
        notes: list[str] = []
        requests = 0
        for group in groups:
            for sort in sorts:
                if requests >= max_requests:
                    notes.append(
                        "Stopped early to stay under Reddit's rate limit; narrow the subreddit list for full coverage."
                    )
                    break
                requests += 1
                try:
                    for p in self.backend.search(query, subreddit=group, sort=sort, time_filter=time_filter, limit=100):
                        found.setdefault(p["fullname"], p)
                except RedditError as exc:
                    notes.append(f"Search in {group or 'all of Reddit'} failed: {exc}")
            else:
                continue
            break

        def rank(p: dict[str, Any], policy: str | None) -> dict[str, Any]:
            return score_post(
                p, keywords=keywords, competitors=competitors, own_username=self.settings.username,
                promo_policy=policy, now=self.clock(),
            )  # fmt: skip

        # Score everything first, then look up rules only for subreddits in the shortlist
        # (each lookup costs API requests).
        first_pass = sorted(found.values(), key=lambda p: rank(p, None)["score"], reverse=True)
        shortlist = first_pass[: max_results * 2]
        policies: dict[str, str | None] = {}
        if self.backend.capabilities().get("subreddit_rules"):
            for sub in {(p.get("subreddit") or "").lower() for p in shortlist} - {""}:
                if len(policies) >= 12:
                    break
                try:
                    pol = self._promo_policy(sub)
                    policies[sub] = pol["policy"] if pol else None
                except RedditError:
                    policies[sub] = None
        ranked = []
        for p in shortlist:
            policy = policies.get((p.get("subreddit") or "").lower())
            result = rank(p, policy)
            if result["score"] >= min_score:
                ranked.append({**result, "post": self._post_view(p, 400), "promo_policy": policy or "unknown"})
        ranked.sort(key=lambda r: r["score"], reverse=True)
        if not self.backend.has_metrics:
            notes.append(
                "RSS mode: scores/comment counts unavailable, so ranking uses intent, relevance and freshness only."
            )
        return {
            "query": query,
            "searched": groups if groups != [None] else ["all of Reddit"],
            "candidates_seen": len(found),
            "opportunities": ranked[:max_results],
            "notes": notes,
            "next_step": "Read the most promising threads with read_thread before drafting. Reply only where you can genuinely help.",
        }

    def track_mentions(
        self,
        terms: list[str] | None = None,
        campaign: str | None = None,
        subreddits: list[str] | None = None,
        include_competitors: bool = False,
        include_seen: bool = False,
        time_filter: str = "week",
    ) -> dict[str, Any]:
        c = self._campaign(campaign)
        terms = list(terms or [])
        if c and not terms:
            terms.append(c["product_name"])
            if c.get("product_url"):
                terms.append(re.sub(r"^https?://(www\.)?", "", c["product_url"]).split("/")[0])
            if include_competitors:
                terms += c["competitors"]
        if not terms:
            raise ValueError("Give terms to watch, or a campaign.")
        subs = [clean_subreddit(s) for s in subreddits or []]
        query = build_search_query(terms)
        posts = self.backend.search(
            query, subreddit="+".join(subs) if subs else None, sort="new", time_filter=time_filter, limit=100
        )
        key = hashlib.sha1(
            ("|".join(sorted(t.lower() for t in terms)) + "#" + "+".join(sorted(subs))).encode()
        ).hexdigest()[:16]
        new = self.store.filter_unseen(key, [p["fullname"] for p in posts])
        items = []
        for p in posts:
            is_new = p["fullname"] in new
            if is_new or include_seen:
                items.append({**self._post_view(p, 300), "new": is_new})
        return {
            "terms": terms,
            "new_mentions": sum(1 for i in items if i["new"]),
            "mentions": items,
            "note": "Covers post titles and bodies. Reddit search doesn't index comments.",
        }

    # -- campaigns ------------------------------------------------------------------------------
    def save_campaign(self, **fields: Any) -> dict[str, Any]:
        if not fields.get("name") or not fields.get("product_name") or not fields.get("description"):
            raise ValueError("A campaign needs name, product_name and description.")
        fields["subreddits"] = [clean_subreddit(s) for s in fields.get("subreddits") or []]
        return self.store.upsert_campaign(**fields)

    def list_campaigns(self) -> dict[str, Any]:
        return {"campaigns": self.store.list_campaigns()}

    # -- drafts ---------------------------------------------------------------------------------
    def _resolve_target(self, ref: str) -> dict[str, Any]:
        prefix, ident = parse_thing_ref(ref)
        fullname = prefix + ident
        target: dict[str, Any] = {"fullname": fullname}
        if m := _SUB_IN_URL.search(ref):
            target["subreddit"] = m.group(1)
        try:
            if self.backend.has_metrics:
                items = self.backend.info([fullname])
                if items:
                    item = items[0]
                    target.update(
                        subreddit=item.get("subreddit"),
                        title=item.get("title"),
                        permalink=item.get("permalink"),
                        locked=item.get("locked"),
                        archived=item.get("archived"),
                        removed=item.get("removed"),
                    )
                    if prefix == "t1_" and item.get("link_id"):
                        parent_items = self.backend.info([item["link_id"]])
                        if parent_items:
                            target["title"] = parent_items[0].get("title")
                            target["locked"] = parent_items[0].get("locked")
                            target["archived"] = parent_items[0].get("archived")
            elif prefix == "t3_":
                post, _ = self.backend.thread(ident, comment_limit=40)  # same URL as read_thread, so it's cached
                target.update(subreddit=post.get("subreddit"), title=post.get("title"), permalink=post.get("permalink"))
        except RedditError:
            pass
        if not target.get("subreddit"):
            raise ValueError("Couldn't look up that thread. Pass the full Reddit URL so the subreddit is known.")
        target.setdefault("permalink", ref if ref.startswith("http") else None)
        return target

    def _target_state(self, d: dict[str, Any]) -> dict[str, Any] | None:
        """Fresh lock/archive state of a comment draft's parent thread, if it can be looked up."""
        if d["kind"] != "comment" or not d.get("parent_fullname"):
            return None
        try:
            return self._resolve_target(d.get("parent_permalink") or d["parent_fullname"])
        except (ValueError, RedditError):
            return None

    def _recent_hashes(self) -> set[str]:
        return {a["body_hash"] for a in self.store.activity_since(self.clock() - 30 * DAY) if a.get("body_hash")}

    def _draft_view(self, d: dict[str, Any]) -> dict[str, Any]:
        view = {
            "draft_id": d["id"],
            "status": d["status"],
            "kind": d["kind"],
            "subreddit": d["subreddit"],
            "replying_to": d.get("parent_title"),
            "target": d.get("parent_permalink"),
            "title": d.get("title"),
            "body": d.get("body"),
            "url": d.get("url"),
            "promotional": bool(d.get("promotional")),
            "rationale": d.get("rationale"),
            "checks": d.get("checks") or [],
            "blocked": bool(gr.blocking(d.get("checks") or [])),
            "permalink": d.get("permalink"),
            "error": d.get("error"),
        }
        return _compact(view) | {"draft_id": d["id"], "checks": view["checks"], "blocked": view["blocked"]}

    def _next_step(self, draft: dict[str, Any]) -> str:
        if gr.blocking(draft.get("checks") or []):
            return "Fix the blocking checks with revise_draft before this can be published."
        if not (self.settings.posting_enabled and self.backend.can_write):
            return "Show the user this draft. Posting is off, so they can post it by hand, then call log_manual_post with the link."
        if self.settings.approval_mode == "terminal":
            return f"Show the user this draft. They approve it in a terminal with `reddit-marketing-agent approve {draft['id']}`, then you can publish."
        return (
            "Show the user the exact text. Call publish_draft with confirm=true only after they explicitly approve it."
        )

    def draft_reply(
        self, target: str, body: str, campaign: str | None = None, rationale: str | None = None
    ) -> dict[str, Any]:
        c = self._campaign(campaign)
        t = self._resolve_target(target)
        policy = self._promo_policy(t["subreddit"])
        checks, promotional = gr.check_draft(
            kind="comment",
            body=body,
            title=None,
            url=None,
            campaign=c,
            promo_policy=policy["policy"] if policy else None,
            target=t,
            recent_hashes=self._recent_hashes(),
        )
        draft = self.store.add_draft(
            campaign_id=c["id"] if c else None,
            kind="comment",
            subreddit=t["subreddit"],
            parent_fullname=t["fullname"],
            parent_title=t.get("title"),
            parent_permalink=t.get("permalink"),
            body=body,
            rationale=rationale,
            promotional=promotional,
            checks=checks,
        )
        return {"draft": self._draft_view(draft), "next_step": self._next_step(draft)}

    def draft_post(
        self,
        subreddit: str,
        title: str,
        body: str | None = None,
        url: str | None = None,
        campaign: str | None = None,
        flair_id: str | None = None,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        if not body and not url:
            raise ValueError("A post needs a body (text post) or a url (link post).")
        c = self._campaign(campaign)
        sub = clean_subreddit(subreddit)
        policy = self._promo_policy(sub)
        checks, promotional = gr.check_draft(
            kind="post",
            body=body or "",
            title=title,
            url=url,
            campaign=c,
            promo_policy=policy["policy"] if policy else None,
            recent_hashes=self._recent_hashes(),
        )
        draft = self.store.add_draft(
            campaign_id=c["id"] if c else None,
            kind="post",
            subreddit=sub,
            title=title,
            body=body,
            url=url,
            flair_id=flair_id,
            rationale=rationale,
            promotional=promotional,
            checks=checks,
        )
        return {"draft": self._draft_view(draft), "next_step": self._next_step(draft)}

    def list_drafts(self, status: str | None = "draft", campaign: str | None = None, limit: int = 20) -> dict[str, Any]:
        c = self._campaign(campaign)
        if status in (None, "", "all"):
            status = None
        drafts = self.store.list_drafts(status, c["id"] if c else None, limit)
        return {"drafts": [self._draft_view(d) for d in drafts]}

    def _get_draft(self, draft_id: int) -> dict[str, Any]:
        d = self.store.get_draft(int(draft_id))
        if d is None:
            raise ValueError(f"No draft with id {draft_id}")
        return d

    def revise_draft(
        self, draft_id: int, body: str | None = None, title: str | None = None, url: str | None = None
    ) -> dict[str, Any]:
        d = self._get_draft(draft_id)
        if d["status"] not in ("draft", "approved", "failed"):
            raise ValueError(f"Draft {draft_id} is {d['status']} and can't be edited.")
        c = self.store.get_campaign(d["campaign_id"]) if d.get("campaign_id") else None
        new_body = body if body is not None else d.get("body")
        new_title = title if title is not None else d.get("title")
        new_url = url if url is not None else d.get("url")
        policy = self._promo_policy(d["subreddit"])
        target = self._target_state(d)
        checks, promotional = gr.check_draft(
            kind=d["kind"],
            body=new_body or "",
            title=new_title,
            url=new_url,
            campaign=c,
            promo_policy=policy["policy"] if policy else None,
            target=target,
            recent_hashes=self._recent_hashes(),
        )
        d = self.store.update_draft(
            d["id"], body=new_body, title=new_title, url=new_url, checks=checks,
            promotional=promotional, status="draft", approved_at=None, error=None,
        )  # fmt: skip
        return {"draft": self._draft_view(d), "next_step": self._next_step(d)}

    def approve_draft(self, draft_id: int) -> dict[str, Any]:
        d = self._get_draft(draft_id)
        if d["status"] != "draft":
            raise ValueError(f"Draft {draft_id} is {d['status']}, not a pending draft.")
        if gr.blocking(d.get("checks") or []):
            raise ValueError("This draft has blocking checks; revise it first.")
        d = self.store.update_draft(d["id"], status="approved", approved_at=self.clock())
        return {"draft": self._draft_view(d)}

    def discard_draft(self, draft_id: int) -> dict[str, Any]:
        d = self._get_draft(draft_id)
        if d["status"] == "published":
            raise ValueError("Already published; delete it on Reddit if needed.")
        d = self.store.update_draft(d["id"], status="discarded")
        return {"draft": self._draft_view(d)}

    def _limits(self, d: dict[str, Any]) -> list[dict[str, str]]:
        s = self.settings
        return gr.check_publish_limits(
            activity=self.store.activity_since(self.clock() - 30 * DAY),
            subreddit=d["subreddit"],
            promotional=bool(d["promotional"]),
            max_per_day=s.max_actions_per_day,
            min_minutes_between=s.min_minutes_between_actions,
            promo_cooldown_hours=s.subreddit_promo_cooldown_hours,
            max_promo_ratio=s.max_promo_ratio,
            now=self.clock(),
        )

    def publish_draft(self, draft_id: int, confirm: bool = False) -> dict[str, Any]:
        s = self.settings
        d = self._get_draft(draft_id)
        if not s.posting_enabled:
            raise ValueError(
                "Publishing is turned off. Set RMA_POSTING_ENABLED=true in your config to allow it, "
                "or post the draft by hand and call log_manual_post."
            )
        if not self.backend.can_write:
            raise NotSupported(
                "Publishing needs a logged-in Reddit account (REDDIT_USERNAME/REDDIT_PASSWORD or "
                "REDDIT_REFRESH_TOKEN alongside the app credentials)."
            )
        if d["status"] == "published":
            raise ValueError(f"Draft {draft_id} is already published: {d.get('permalink')}")
        if d["status"] not in ("draft", "approved"):
            hint = " Use revise_draft to fix it and retry." if d["status"] == "failed" else ""
            raise ValueError(f"Draft {draft_id} is {d['status']}.{hint}")
        if s.approval_mode == "terminal" and d["status"] != "approved":
            raise ValueError(
                f"Approval mode is 'terminal': the user must run `reddit-marketing-agent approve {draft_id}` first."
            )

        content_checks = d.get("checks") or []
        t = self._target_state(d)
        if t:
            if t.get("locked") or t.get("archived") or t.get("removed"):
                content_checks = content_checks + [
                    {
                        "level": "block",
                        "code": "target_closed",
                        "message": "The thread is now locked, archived or removed.",
                    }
                ]
        limit_checks = self._limits(d)
        blockers = gr.blocking(content_checks + limit_checks)
        if blockers:
            return {"published": False, "blocked_by": blockers, "draft": self._draft_view(d)}
        if not confirm:
            return {
                "published": False,
                "needs_confirmation": True,
                "preview": self._draft_view(d),
                "warnings": [c for c in content_checks + limit_checks if c["level"] != "block"],
                "next_step": "Show the user this exact text. If they explicitly approve, call publish_draft again with confirm=true.",
            }

        try:
            if d["kind"] == "post":
                result = self.backend.submit_post(
                    d["subreddit"], d["title"], body=d.get("body"), url=d.get("url"), flair_id=d.get("flair_id")
                )
            else:
                result = self.backend.submit_comment(d["parent_fullname"], d["body"])
        except RedditError as exc:
            d = self.store.update_draft(d["id"], status="failed", error=str(exc))
            return {"published": False, "error": str(exc), "draft": self._draft_view(d)}

        d = self.store.update_draft(
            d["id"], status="published", reddit_fullname=result.get("fullname"),
            permalink=result.get("permalink"), published_at=self.clock(), error=None,
        )  # fmt: skip
        self.store.log_activity(
            draft_id=d["id"], campaign_id=d.get("campaign_id"), kind=d["kind"], subreddit=d["subreddit"],
            reddit_fullname=result.get("fullname"), permalink=result.get("permalink"),
            body_hash=gr.body_hash(d.get("body") or d.get("title") or ""), promotional=d["promotional"],
        )  # fmt: skip
        return {"published": True, "permalink": result.get("permalink"), "draft": self._draft_view(d)}

    def log_manual_post(self, draft_id: int, permalink: str) -> dict[str, Any]:
        d = self._get_draft(draft_id)
        if d["status"] == "published":
            raise ValueError("Already logged as published.")
        prefix, ident = parse_thing_ref(permalink)
        fullname = prefix + ident
        d = self.store.update_draft(
            d["id"], status="published", reddit_fullname=fullname, permalink=permalink, published_at=self.clock()
        )
        self.store.log_activity(
            draft_id=d["id"], campaign_id=d.get("campaign_id"), kind=d["kind"], subreddit=d["subreddit"],
            reddit_fullname=fullname, permalink=permalink,
            body_hash=gr.body_hash(d.get("body") or d.get("title") or ""), promotional=d["promotional"],
        )  # fmt: skip
        return {"logged": True, "draft": self._draft_view(d)}

    # -- results --------------------------------------------------------------------------------
    def performance_report(self, campaign: str | None = None, days: int = 30) -> dict[str, Any]:
        c = self._campaign(campaign)
        activity = self.store.activity_since(self.clock() - days * DAY, c["id"] if c else None)
        notes = []
        if self.backend.has_metrics and activity:
            fullnames = [a["reddit_fullname"] for a in activity if a.get("reddit_fullname")]
            try:
                for item in self.backend.info(fullnames):
                    self.store.update_activity_metrics(
                        item["fullname"], item.get("score"), item.get("num_comments"), bool(item.get("removed"))
                    )
                activity = self.store.activity_since(self.clock() - days * DAY, c["id"] if c else None)
            except RedditError as exc:
                notes.append(f"Couldn't refresh live metrics: {exc}")
        elif not self.backend.has_metrics:
            notes.append("RSS mode: live scores unavailable; showing your local log only.")

        by_sub: dict[str, dict[str, Any]] = {}
        for a in activity:
            b = by_sub.setdefault(
                a["subreddit"], {"actions": 0, "promotional": 0, "score": 0, "replies": 0, "removed": 0}
            )
            b["actions"] += 1
            b["promotional"] += a["promotional"]
            b["score"] += a.get("score") or 0
            b["replies"] += a.get("num_comments") or 0
            b["removed"] += a["removed"]
        scored = [a for a in activity if a.get("score") is not None]
        best = sorted(scored, key=lambda a: a["score"], reverse=True)[:5]
        removed = [a for a in activity if a["removed"]]
        if removed:
            notes.append(
                f"{len(removed)} item(s) were removed by moderators or filters. Check those subreddits' rules before posting there again."
            )
        return {
            "days": days,
            "campaign": c["name"] if c else None,
            "total_actions": len(activity),
            "promotional_actions": sum(a["promotional"] for a in activity),
            "by_subreddit": by_sub,
            "best": [
                {"permalink": a["permalink"], "score": a["score"], "kind": a["kind"], "subreddit": a["subreddit"]}
                for a in best
            ],
            "removed": [{"permalink": a["permalink"], "subreddit": a["subreddit"]} for a in removed],
            "notes": notes,
        }

    def inbox(self, unread_only: bool = True, limit: int = 25) -> dict[str, Any]:
        items = self.backend.inbox(unread_only=unread_only, limit=limit)
        for i in items:
            i["age"] = _age(i.get("created_utc"), self.clock())
            i["body"] = truncate(i.get("body"), 1500)
        return {"items": [_compact(i) for i in items]}
