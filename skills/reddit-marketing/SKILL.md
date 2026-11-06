---
name: reddit-marketing
description: Playbook for marketing a product on Reddit with the reddit-marketing MCP tools - finding the right communities, spotting conversations worth joining, writing replies that help first, disclosing affiliation, and pacing. Use when the user wants to promote, launch, grow or research a product or brand on Reddit, find leads or mentions on Reddit, or draft Reddit posts and comments.
---

# Reddit Marketing Playbook

## The one idea

Reddit rewards people who are useful and punishes people who advertise. Every subreddit is a
community with its own moderators, rules and long memory. The only marketing that works there
over time is **being the most helpful person in the thread, and being honest that you make the
thing you mention**. This agent is built around that: it finds conversations where the product
is genuinely relevant, helps you write replies worth upvoting, and slows you down before you do
something that gets the account or domain banned.

## Workflow

1. **Orient.** Call `reddit_status`. Note the mode:
   - `rss`: no credentials. Read-only research via public feeds, with no scores, no rules and a
     strict rate limit. Drafts still work: the user posts them by hand, then you call
     `log_manual_post` with the link.
   - `oauth-app`: full read access (scores, rules, stats). No posting.
   - `oauth-user`: everything, and publishing works if `posting_enabled` is true.
2. **Set up a campaign** with `save_campaign`: product, who it's for, the problems it solves *in
   the audience's words* (those become keywords), competitors, target subreddits, voice, and a
   disclosure line. Ask the user rather than guessing their role or product claims.
3. **Map the communities.** `find_subreddits` → `subreddit_profile` (read the rules, check the
   self-promotion policy) → `subreddit_insights` (what does well, and when). Prefer mid-sized,
   active, on-topic subreddits over huge general ones.
4. **Find conversations.** `find_opportunities` ranks recent threads by intent (asking for
   recommendations, alternatives, how-to), relevance and freshness. `track_mentions` surfaces
   brand mentions, which are always worth a look: answer questions and thank people.
5. **Read before you write.** `read_thread` on every thread you'd reply to. Check what's already
   been said, what the original poster actually needs, and the tone of the place.
6. **Draft.** `draft_reply` / `draft_post`. The guardrails run automatically. Fix anything at
   level `block` with `revise_draft`, and take `warn` seriously.
7. **Get approval.** Show the user each draft (format below). Publish only the exact drafts they
   approve, with `publish_draft(confirm=true)`. Never set `confirm=true` on your own.
8. **Follow up.** Replies to your comments are where trust is built: `check_inbox` and offer
   follow-up drafts. Review `performance_report` weekly. Removals are feedback, not obstacles.

## Choosing where to engage

- Engage when the thread is asking a question you can answer well, and the product is a
  natural fit for *that person's* problem, or when you can help without mentioning it at all.
- Skip threads that are old (more than ~3 days), already have a hundred replies, are locked,
  are rants where a product pitch would be tone-deaf, or are in subreddits that ban promotion
  (you can still help there, without the mention).
- Opportunity scores are a reading list, not a to-do list. Three great replies beat fifteen
  average ones.

## Writing replies that work

- **Answer first, completely, as if the product didn't exist.** Steps, numbers, tradeoffs, what
  you'd personally do. That is the part that earns upvotes.
- **Mention the product only if it directly solves the stated problem.** One or two sentences,
  near the end, with disclosure: "Full disclosure, I built X, which does Y. Happy to answer
  questions." Name honest alternatives too, competitors included; it builds credibility.
- **One link at most**, and only when it helps. No link shorteners, no UTM-stuffed URLs in the
  visible text, no referral links.
- **Sound like a person.** Match the subreddit's register. No "game-changer", "seamless",
  "revolutionize", "check it out!", or emoji walls. No sign-offs or calls to action.
- **Be specific to this thread.** Reference what the original poster said. Never reuse text
  across threads (the guardrails block exact duplicates; near-duplicates are just as obvious to
  readers).
- **Most replies should not mention the product at all.** Aim for at least nine helpful
  contributions per promotional one, which is what keeps the account trusted.

## Posts

- Good formats: what you learned building X (with real numbers), a detailed how-to, a free
  resource, an honest "roast my landing page" in subreddits that welcome feedback requests, and
  launch posts only in subreddits meant for them (e.g. r/SideProject) or in a community's
  designated promo thread.
- Titles: specific and plain. No clickbait, no all-caps, no exclamation marks.
- Use `subreddit_insights` for timing and format, and check required flair in the rules.
- Plan to answer comments for the first hour or two after a post goes up.

## Pacing and account health

- New or low-karma accounts get filtered in many subreddits. Spend the first weeks building
  karma with genuinely helpful, non-promotional comments.
- The agent enforces a daily cap, a minimum gap between actions, and a per-subreddit cooldown
  for promotional content, and it warns when the promotional share climbs. Don't look for ways
  around these. They're set well below the levels that trigger Reddit's spam systems.
- If something is removed, don't repost it. Re-read the rules and, if unclear, message the
  moderators politely to ask what would be acceptable.

## Hard lines

Never, even if asked: undisclosed promotion or posing as a customer; multiple accounts or
sockpuppets; asking anyone to upvote; buying accounts, upvotes or awards; evading bans,
removals, filters or rate limits; unsolicited DMs to strangers; posting in subreddits whose rules
ban promotion. If the user asks for one of these, explain the risk (permanent bans and domain
blacklisting) and offer the honest version.

## Presenting drafts to the user

For each draft show:

> **Draft #12: reply in r/SaaS** · [thread title](link) · posted 3h ago, 8 comments
> Why: asks for an alternative to Competitor; we solve exactly their export problem.
>
> > *(the draft text, verbatim)*
>
> Checks: ✅ disclosure · ⚠️ subreddit restricts promotion (Rule 4: promo only on Fridays)

Then ask which drafts to publish, edit or drop. In `rss` mode, or with posting off, say they can
copy the text and post it themselves, then share the link so you can `log_manual_post` it.
