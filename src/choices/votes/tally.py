"""Turn a pile of comments from several platforms into one decision.

Policy, all configurable in ``VotingConfig``:

* one voter, one vote per platform: a viewer who changes their mind keeps
  their latest (or first) vote, so spamming "A A A" counts once;
* the account's own comments, comments after the cutoff, and link spam are
  excluded;
* optional like-weighting lets a liked "B, obviously" count a little more,
  capped so one viral comment can't swing the result;
* per-platform weights in case one platform's audience dwarfs the other's;
* too few voters or a near-tie is decided by a seeded coin flip that the next
  episode admits to on-screen. Seeded means a re-run gives the same answer.

The full per-comment decision trail is returned too, for the review page.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime

from ..config import VotingConfig
from ..models import ChoiceKey, Comment, ParsedVote, PlatformCount, TallyResult, VoteMethod

_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
_MENTION = re.compile(r"@[\w.]+")
# Top comments get quoted back into tomorrow's writer prompt, so anything that
# smells like an instruction to a model is never quoted (it can still vote).
_INSTRUCTION = re.compile(
    r"\b(ignore|disregard|instruction|prompt|system|assistant|you are now|pretend|jailbreak|override)\b", re.IGNORECASE
)


@dataclass
class AuditRow:
    comment: Comment
    vote: ParsedVote
    counted: bool
    note: str = ""


def _quote(text: str) -> str:
    t = " ".join(_MENTION.sub("", text).split())
    return t if len(t) <= 120 else t[:117] + "..."


def tally(
    comments: list[Comment],
    votes: dict[str, ParsedVote],
    cfg: VotingConfig,
    *,
    cutoff: datetime,
    seed: str,
) -> tuple[TallyResult, list[AuditRow]]:
    audit: list[AuditRow] = []
    excluded = 0
    # (platform, author) -> (comment, vote)
    best: dict[tuple[str, str], tuple[Comment, ParsedVote]] = {}

    for c in sorted(comments, key=lambda c: c.created_at):
        v = votes.get(c.key) or ParsedVote(choice=None, method=VoteMethod.NONE, reason="unparsed")
        if c.is_owner:
            excluded += 1
            audit.append(AuditRow(c, v, False, "own account"))
            continue
        if c.created_at > cutoff:
            excluded += 1
            audit.append(AuditRow(c, v, False, "after voting closed"))
            continue
        if _LINK.search(c.text):
            excluded += 1
            audit.append(AuditRow(c, v, False, "contains a link"))
            continue
        if v.choice is None:
            audit.append(AuditRow(c, v, False, v.reason))
            continue
        key = (c.platform, c.author_id or c.id)
        if key in best and cfg.keep == "first":
            audit.append(AuditRow(c, v, False, "voter already counted (keep=first)"))
            continue
        best[key] = (c, v)
        audit.append(AuditRow(c, v, True))

    counted_keys = {cv[0].key for cv in best.values()}
    for row in audit:
        if row.counted and row.comment.key not in counted_keys:
            row.counted = False
            row.note = "superseded by this voter's later vote"

    by_platform: dict[str, PlatformCount] = {}
    wa = wb = 0.0
    side_comments: dict[str, list[Comment]] = {"A": [], "B": []}
    for (platform, _author), (c, v) in best.items():
        w = (1.0 + cfg.like_weight * min(c.likes, cfg.like_cap)) * cfg.platform_weights.get(platform, 1.0)
        pc = by_platform.setdefault(platform, PlatformCount())
        pc.voters += 1
        if v.choice == "A":
            wa += w
            pc.a += w
        else:
            wb += w
            pc.b += w
        side_comments[v.choice].append(c)

    voters = len(best)
    total = wa + wb
    decided_by = "votes"
    if voters < cfg.min_votes:
        decided_by = "low_turnout"
    elif total == 0 or abs(wa - wb) / total < cfg.tie_margin:
        decided_by = "tiebreak"

    if decided_by == "votes":
        winner: ChoiceKey = "A" if wa > wb else "B"
    elif cfg.tie_break in ("A", "B"):
        winner = cfg.tie_break  # type: ignore[assignment]
    else:
        winner = random.Random(seed).choice(["A", "B"])  # deterministic per episode

    top: dict[str, list[str]] = {}
    for side, cs in side_comments.items():
        wordy = [c for c in cs if len(c.text.split()) >= 4 and not _INSTRUCTION.search(c.text) and not _LINK.search(c.text)]
        wordy.sort(key=lambda c: (c.likes, len(c.text)), reverse=True)
        quotes: list[str] = []
        for c in wordy:  # copy-pasted comments shouldn't fill every quote slot
            q = _quote(c.text)
            if q.lower() not in {x.lower() for x in quotes}:
                quotes.append(q)
            if len(quotes) == 3:
                break
        top[side] = quotes

    ambiguous = sum(1 for r in audit if r.vote.choice is None and r.vote.reason in ("conflict", "negated", "unclear"))
    llm_classified = sum(1 for _, v in best.values() if v.method == VoteMethod.LLM)

    result = TallyResult(
        winner=winner,
        decided_by=decided_by,
        weighted_a=round(wa, 3),
        weighted_b=round(wb, 3),
        unique_voters=voters,
        comments_seen=len(comments),
        comments_excluded=excluded,
        ambiguous=ambiguous,
        llm_classified=llm_classified,
        by_platform=by_platform,
        top_comments=top,
        closed_at=cutoff,
    )
    return result, audit
