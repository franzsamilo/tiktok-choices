"""Canon bookkeeping: initial state, applying an episode, rendering it for prompts."""

from __future__ import annotations

from ..config import SeriesBible
from ..models import Companion, EpisodeRecord, EpisodeSummary, Outcome, SeriesState, TallyResult


def initial_state(bible: SeriesBible) -> SeriesState:
    return SeriesState(
        series_id=bible.series_id,
        day=0,
        location=bible.starting_location,
        inventory=list(bible.starting_inventory),
        companions=[
            Companion(name=c.name, status="with_you" if c.starts_with_you else "missing", note=c.role)
            for c in bible.characters
            if c.starts_with_you
        ],
    )


def apply_episode(state: SeriesState, ep: EpisodeRecord) -> SeriesState:
    """Fold an approved episode's canon update into the running state."""
    c = ep.script.canon
    new = state.model_copy(deep=True)
    new.day = ep.day
    new.location = c.location
    new.inventory = list(c.inventory)
    new.companions = list(c.companions)
    # Facts accumulate; dedupe while keeping order. Threads are replaced (the writer owns them).
    seen = set(f.lower() for f in new.facts)
    for f in c.new_facts:
        if f.lower() not in seen:
            new.facts.append(f)
            seen.add(f.lower())
    new.open_threads = list(c.open_threads)
    new.history = [h for h in new.history if h.day != ep.day]
    new.history.append(
        EpisodeSummary(
            day=ep.day,
            title=ep.script.title,
            summary=summarise(ep),
            question=ep.script.question,
        )
    )
    new.history.sort(key=lambda h: h.day)
    return new


def record_decision(state: SeriesState, day: int, outcome: Outcome) -> SeriesState:
    new = state.model_copy(deep=True)
    for h in new.history:
        if h.day == day:
            h.chosen = outcome.chosen_label
            h.decided_by = outcome.decided_by
    return new


def outcome_from_tally(ep: EpisodeRecord, tally: TallyResult) -> Outcome:
    chosen = ep.script.choice(tally.winner)
    rejected = ep.script.choice("B" if tally.winner == "A" else "A")
    total = tally.weighted_a + tally.weighted_b
    share = (tally.weighted_a if tally.winner == "A" else tally.weighted_b) / total if total else 0.5
    return Outcome(
        day=ep.day,
        question=ep.script.question,
        chosen_key=tally.winner,
        chosen_label=chosen.label,
        rejected_label=rejected.label,
        stakes=chosen.stakes,
        decided_by=tally.decided_by,
        share=round(share, 3),
        unique_voters=tally.unique_voters,
        crowd_quotes=tally.top_comments.get(tally.winner, [])[:3],
    )


def summarise(ep: EpisodeRecord) -> str:
    """A cheap, deterministic one-paragraph summary. Good enough for long-range memory."""
    s = ep.script
    first = s.scenes[0].narration if s.scenes else ""
    last = s.scenes[-1].narration if s.scenes else ""
    text = f"{first} ... {last}"
    return text if len(text) <= 420 else text[:417] + "..."


def render_canon(state: SeriesState) -> str:
    comp = "\n".join(f"- {c.name}: {c.status.replace('_', ' ')} ({c.note})" for c in state.companions) or "- none"
    inv = "\n".join(f"- {i}" for i in state.inventory) or "- nothing"
    facts = "\n".join(f"- {f}" for f in state.facts[-25:]) or "- none yet"
    threads = "\n".join(f"- {t}" for t in state.open_threads) or "- none"
    return (
        f"Location right now: {state.location}\n"
        f"Inventory:\n{inv}\n"
        f"Companions:\n{comp}\n"
        f"Established facts (most recent last):\n{facts}\n"
        f"Open threads:\n{threads}"
    )


def render_history(state: SeriesState, recent: list[EpisodeRecord]) -> str:
    """Older days as one-liners, the most recent few in full narration."""
    full_days = {e.day for e in recent}
    lines: list[str] = []
    for h in state.history:
        if h.day in full_days:
            continue
        chosen = f" Audience chose: {h.chosen}." if h.chosen else ""
        lines.append(f"Day {h.day}, \"{h.title}\": {h.summary}{chosen}")
    for e in sorted(recent, key=lambda e: e.day):
        chosen = ""
        if e.tally:
            label = e.script.choice(e.tally.winner).label
            chosen = f"\nAudience chose: {e.tally.winner} ({label})."
        lines.append(
            f"Day {e.day}, \"{e.script.title}\" (full narration):\n{e.script.full_narration()}\n"
            f"Question: {e.script.question}  A = {e.script.choice_a.label} / B = {e.script.choice_b.label}{chosen}"
        )
    return "\n\n".join(lines) if lines else "(nothing yet: this is the pilot)"
