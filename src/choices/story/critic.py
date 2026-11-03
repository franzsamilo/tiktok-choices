"""Quality gate between the writer model and production.

Three layers, cheapest first:

1. ``normalize``: deterministic fixes code can make with certainty (strip '#'
   from hashtags, dedupe keywords shared by both options, add a missing '?').
   Burning an LLM revision on these would be waste.
2. ``rule_issues``: hard checks a model is bad at self-policing: length
   budgets, label sizes, dead characters walking around, banned imagery.
3. ``LLMCritic``: a separate model call that reviews continuity and dilemma
   balance against canon. It has a narrower job than the writer and sees the
   draft cold, which catches errors the writer argued itself into.

Anything that fails becomes concrete revision notes for the writer, and a
human still approves the final render (see ``pipeline``).
"""

from __future__ import annotations

import re
from typing import Protocol

from ..config import SeriesBible, WritingConfig
from ..llm import Claude
from ..models import CriticVerdict, EpisodeScript, Issue, LLMCall, Outcome, SeriesState
from .prompts import critic_system, critic_user, word_budget

_WORD = re.compile(r"[\w']+", re.UNICODE)

# Imagery the image model must never be asked for, whatever the writer wants.
BANNED_VISUAL = ("blood", "gore", "gory", "guts", "entrails", "decapitat", "severed", "dismember", "bite wound", "corpse pile")
BANNED_NARRATION = ("gore", "entrails", "decapitat", "dismember")


def words(text: str) -> int:
    return len(_WORD.findall(text))


def normalize(script: EpisodeScript, bible: SeriesBible | None = None) -> tuple[EpisodeScript, list[Issue]]:
    """Apply safe, deterministic fixes. Returns the fixed copy and what was changed."""
    s = script.model_copy(deep=True)
    fixed: list[Issue] = []

    s.hashtags = [h.strip().lstrip("#").replace(" ", "") for h in s.hashtags if h.strip("# ")]

    q = s.question.strip()
    if not q.endswith("?"):
        s.question = q.rstrip(".!") + "?"
        fixed.append(Issue(severity="warning", code="question_mark", message="Added missing '?' to question."))

    for opt in (s.choice_a, s.choice_b):
        opt.label = " ".join(opt.label.upper().split())
        opt.keywords = sorted({k.strip().lower() for k in opt.keywords if k.strip()})

    # Character names make terrible vote keywords: fans name characters in
    # comments all the time ("lola cora is so real for this") without voting.
    if bible is not None:
        names = {part.lower() for c in bible.characters for part in [c.name, *c.name.split()]}
        for opt in (s.choice_a, s.choice_b):
            dropped = [k for k in opt.keywords if k in names]
            if dropped:
                opt.keywords = [k for k in opt.keywords if k not in names]
                fixed.append(Issue(severity="warning", code="name_keyword", message=f"Removed character names from vote keywords: {dropped}"))

    shared = set(s.choice_a.keywords) & set(s.choice_b.keywords)
    if shared:
        s.choice_a.keywords = [k for k in s.choice_a.keywords if k not in shared]
        s.choice_b.keywords = [k for k in s.choice_b.keywords if k not in shared]
        fixed.append(
            Issue(severity="warning", code="keyword_overlap", message=f"Removed keywords used by both options: {sorted(shared)}")
        )
    return s, fixed


def rule_issues(script: EpisodeScript, bible: SeriesBible, cfg: WritingConfig, state: SeriesState) -> list[Issue]:
    out: list[Issue] = []

    def block(code: str, msg: str) -> None:
        out.append(Issue(severity="blocker", code=code, message=msg))

    def warn(code: str, msg: str) -> None:
        out.append(Issue(severity="warning", code=code, message=msg))

    n = len(script.scenes)
    if not cfg.min_scenes <= n <= cfg.max_scenes:
        block("scene_count", f"Episode has {n} scenes; write {cfg.min_scenes}-{cfg.max_scenes}.")

    lo, hi = word_budget(cfg)
    total = sum(words(sc.narration) for sc in script.scenes)
    if total < lo * 0.85 or total > hi * 1.15:
        block("length", f"Narration is {total} words; the budget is {lo}-{hi}. {'Cut' if total > hi else 'Add'} to fit.")
    elif not lo <= total <= hi:
        warn("length", f"Narration is {total} words, slightly outside {lo}-{hi}.")

    hook_words = words(script.hook)
    if hook_words > 12:
        block("hook_length", f"Hook is {hook_words} words. It must read in under 2 seconds: 8 words max.")
    elif hook_words > 8:
        warn("hook_length", f"Hook is {hook_words} words; 8 or fewer reads faster.")

    for key, opt in (("A", script.choice_a), ("B", script.choice_b)):
        lw = words(opt.label)
        if lw == 0 or lw > 4 or len(opt.label) > 24:
            block("label", f"Option {key} label '{opt.label}' must be 1-4 short words (max 24 characters).")
        if len(opt.keywords) < 2:
            warn("keywords", f"Option {key} has fewer than 2 distinct keywords; free-text votes will be harder to read.")
    if script.choice_a.label == script.choice_b.label:
        block("label", "Both options have the same label.")

    # Continuity a rule can check: the dead stay off camera. Missing/separated
    # companions may legitimately return (often *because* of the vote), so
    # those are only flagged for the critic and the human to sanity-check.
    status_of = {c.name.lower(): c.status for c in state.companions}
    for i, sc in enumerate(script.scenes, 1):
        for name in sc.characters:
            status = status_of.get(name.lower())
            if status == "dead":
                block("continuity", f"Scene {i} shows {name}, but canon says they are dead.")
            elif status in ("missing", "separated"):
                warn("reunion", f"Scene {i} shows {name}, who was {status}; make sure the episode earns the return.")
            elif status is None and bible.character(name) is None:
                warn("unknown_character", f"Scene {i} lists '{name}', who has no locked look; their appearance may drift.")

        vis = sc.visual.lower()
        hits = [b for b in BANNED_VISUAL if b in vis]
        if hits:
            block("unsafe_visual", f"Scene {i} visual asks for {hits}. Imply danger through framing instead.")
        nar = sc.narration.lower()
        nhits = [b for b in BANNED_NARRATION if b in nar]
        if nhits:
            warn("unsafe_narration", f"Scene {i} narration uses {nhits}; keep it 13+.")

    if len(script.caption) > 150:
        warn("caption", f"Caption is {len(script.caption)} characters; keep it under 150.")
    return out


class Critic(Protocol):
    def review(self, script: EpisodeScript, state_canon: str, outcome: Outcome | None) -> tuple[CriticVerdict, LLMCall | None]: ...


class LLMCritic:
    def __init__(self, claude: Claude, bible: SeriesBible, model: str, effort: str):
        self.claude, self.bible, self.model, self.effort = claude, bible, model, effort

    def review(self, script: EpisodeScript, state_canon: str, outcome: Outcome | None) -> tuple[CriticVerdict, LLMCall]:
        return self.claude.structured(
            purpose="critic",
            model=self.model,
            effort=self.effort,
            system=critic_system(self.bible),
            user=critic_user(canon=state_canon, outcome=outcome, script=script),
            output=CriticVerdict,
            max_tokens=8000,
        )


def verdict_issues(v: CriticVerdict) -> list[Issue]:
    out = [Issue(severity="blocker", code="continuity", message=e) for e in v.continuity_errors]
    if not v.dilemma_is_balanced:
        out.append(Issue(severity="blocker", code="dilemma", message="One option is obviously right; rebalance the costs."))
    if v.hook_strength <= 2:
        out.append(Issue(severity="blocker", code="hook", message=f"Hook scored {v.hook_strength}/5; open on a sharper question."))
    out += [Issue(severity="warning", code="editor_note", message=n) for n in v.notes]
    return out
