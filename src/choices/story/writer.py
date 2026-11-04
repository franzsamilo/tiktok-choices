"""Episode writing: the writer model plus the write -> check -> revise loop."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from ..config import SeriesBible, WritingConfig
from ..llm import Claude
from ..models import EpisodeRecord, EpisodeScript, Issue, LLMCall, Outcome, QualityReport, SeriesState
from .canon import render_canon, render_history
from .critic import Critic, normalize, rule_issues, verdict_issues
from .prompts import writer_system, writer_user

log = logging.getLogger(__name__)


@dataclass
class WriteRequest:
    day: int
    canon: str
    history: str
    outcome: Outcome | None
    feedback: list[str] = field(default_factory=list)
    previous_draft: EpisodeScript | None = None
    state: SeriesState | None = None  # structured canon, for writers that don't read prose
    performance: str = ""  # measured results of past episodes (analytics.performance_digest)


class Writer(Protocol):
    def write(self, req: WriteRequest) -> tuple[EpisodeScript, LLMCall | None]: ...


class ClaudeWriter:
    def __init__(self, claude: Claude, bible: SeriesBible, cfg: WritingConfig, model: str, effort: str):
        self.claude, self.bible, self.cfg, self.model, self.effort = claude, bible, cfg, model, effort
        self._system = writer_system(bible, cfg)  # built once: byte-stable => cacheable

    def write(self, req: WriteRequest) -> tuple[EpisodeScript, LLMCall]:
        user = writer_user(
            day=req.day,
            canon=req.canon,
            history=req.history,
            outcome=req.outcome,
            feedback=req.feedback,
            previous_draft=req.previous_draft,
            performance=req.performance,
        )
        return self.claude.structured(
            purpose="writer" if req.previous_draft is None else "writer-revision",
            model=self.model,
            effort=self.effort,
            system=self._system,
            user=user,
            output=EpisodeScript,
        )


@dataclass
class DraftResult:
    script: EpisodeScript
    quality: QualityReport
    calls: list[LLMCall]


def draft_episode(
    *,
    writer: Writer,
    critic: Critic | None,
    bible: SeriesBible,
    cfg: WritingConfig,
    state: SeriesState,
    recent: list[EpisodeRecord],
    day: int,
    outcome: Outcome | None,
    reviewer_notes: list[str] | None = None,
    previous_draft: EpisodeScript | None = None,
    performance: str = "",
) -> DraftResult:
    canon = render_canon(state)
    history = render_history(state, recent)
    feedback = list(reviewer_notes or [])
    draft = previous_draft
    calls: list[LLMCall] = []
    script: EpisodeScript | None = None
    issues: list[Issue] = []
    verdict = None

    for attempt in range(1, cfg.max_revisions + 2):
        script, call = writer.write(
            WriteRequest(
                day=day, canon=canon, history=history, outcome=outcome,
                feedback=feedback, previous_draft=draft, state=state, performance=performance,
            )
        )
        if call:
            calls.append(call)
        script, issues = normalize(script, bible)
        issues += rule_issues(script, bible, cfg, state)
        verdict = None

        # Only pay for the LLM critic once the cheap checks pass.
        if critic is not None and not any(i.severity == "blocker" for i in issues):
            verdict, ccall = critic.review(script, canon, outcome)
            if ccall:
                calls.append(ccall)
            issues += verdict_issues(verdict)

        blockers = [i for i in issues if i.severity == "blocker"]
        if not blockers:
            log.info("day %s: draft %s passed review", day, attempt)
            return DraftResult(script, QualityReport(passed=True, issues=issues, critic=verdict, attempts=attempt), calls)

        log.warning("day %s: draft %s blocked: %s", day, attempt, "; ".join(b.message for b in blockers))
        feedback = [b.message for b in blockers]
        if verdict:
            feedback += verdict.notes
        draft = script

    assert script is not None
    # Out of revisions: ship to the human reviewer clearly flagged rather than silently.
    return DraftResult(script, QualityReport(passed=False, issues=issues, critic=verdict, attempts=cfg.max_revisions + 1), calls)
