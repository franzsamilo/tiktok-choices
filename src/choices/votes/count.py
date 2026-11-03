"""Glue: parse every comment, send the unreadable ones to the LLM, tally."""

from __future__ import annotations

import logging
from datetime import datetime

from ..config import VotingConfig
from ..models import Comment, EpisodeScript, LLMCall, ParsedVote, TallyResult, VoteMethod
from .classifier import LLMVoteClassifier
from .parser import Vocabulary, parse_vote
from .tally import AuditRow, tally

log = logging.getLogger(__name__)


def count_votes(
    script: EpisodeScript,
    comments: list[Comment],
    cfg: VotingConfig,
    *,
    cutoff: datetime,
    seed: str,
    classifier: LLMVoteClassifier | None = None,
) -> tuple[TallyResult, list[AuditRow], list[LLMCall]]:
    vocab = Vocabulary.from_script(script)
    votes: dict[str, ParsedVote] = {}
    pending: list[Comment] = []
    for c in comments:
        v, needs_llm = parse_vote(c.text, vocab)
        votes[c.key] = v
        if needs_llm and not c.is_owner and c.created_at <= cutoff:
            pending.append(c)

    calls: list[LLMCall] = []
    if classifier and pending:
        if len(pending) > cfg.max_llm_comments:
            # Prefer the comments other viewers engaged with; drop the long tail.
            log.warning("%d ambiguous comments; classifying the %d most-liked", len(pending), cfg.max_llm_comments)
            pending = sorted(pending, key=lambda c: c.likes, reverse=True)[: cfg.max_llm_comments]
        decided, calls = classifier.classify(script, pending)
        for key, choice in decided.items():
            prev = votes[key]
            votes[key] = ParsedVote(
                choice=choice,
                method=VoteMethod.LLM if choice else VoteMethod.NONE,
                reason=f"llm ({prev.reason})" if choice else f"llm: no vote ({prev.reason})",
                confidence=0.7 if choice else 0.0,
            )

    result, audit = tally(comments, votes, cfg, cutoff=cutoff, seed=seed)
    return result, audit, calls
