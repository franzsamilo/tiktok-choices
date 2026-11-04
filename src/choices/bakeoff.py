"""Model bake-off: same shots, several image models, scored the same way.

New image models ship every few weeks, and leaderboards measure someone
else's prompts. This runs *our* shots (with our character references) through
each candidate, has the frame judge score every result, and reports what
matters for a production decision:

    $/image · seconds/image · mean judge score · pass rate · $ per usable frame

"$ per usable frame" is the deciding number. A cheap model that fails QA
half the time costs more than it looks once re-rolls are counted.

    choices bakeoff --day 3 --models fal-ai/nano-banana-2/edit,fal-ai/nano-banana-pro/edit
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from .costs import image_cost
from .media.mock import MockImages, stable_seed
from .media.prompting import image_prompt
from .media.qa import passes
from .models import EpisodeRecord, utcnow

if TYPE_CHECKING:
    from .pipeline import Pipeline

log = logging.getLogger(__name__)


@dataclass
class Shot:
    scene: int
    path: str
    seconds: float
    usd: float
    score: int | None
    passed: bool
    notes: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ModelResult:
    model: str
    shots: list[Shot]

    @property
    def ok(self) -> list[Shot]:
        return [s for s in self.shots if not s.error]

    def summary(self) -> dict:
        ok = self.ok
        scored = [s.score for s in ok if s.score is not None]
        passed = sum(1 for s in ok if s.passed)
        spend = sum(s.usd for s in ok)
        return {
            "model": self.model,
            "images": len(ok),
            "errors": len(self.shots) - len(ok),
            "usd_per_image": round(spend / len(ok), 4) if ok else None,
            "seconds_per_image": round(sum(s.seconds for s in ok) / len(ok), 1) if ok else None,
            "mean_score": round(sum(scored) / len(scored), 2) if scored else None,
            "pass_rate": round(passed / len(ok), 2) if ok else None,
            "usd_per_usable_frame": round(spend / passed, 4) if passed else None,
        }


def _provider(pipe: Pipeline, model: str):
    if model.startswith("mock"):
        return MockImages(model, model)
    from .media.fal import FalImages

    return FalImages(model, model if "/edit" in model else "")


def run_bakeoff(pipe: Pipeline, ep: EpisodeRecord, models: list[str], scenes: list[int]) -> Path:
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    root = pipe.store.root / "bakeoff" / f"day-{ep.day:03d}-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    r = pipe.cfg.render
    results: list[ModelResult] = []

    for model in models:
        provider = _provider(pipe, model)
        slug = model.replace("/", "_")
        shots: list[Shot] = []
        for n in scenes:
            if not 1 <= n <= len(ep.script.scenes):
                continue
            scene = ep.script.scenes[n - 1]
            refs = pipe.cast_refs(scene.characters) if "/edit" in model or model.startswith("mock") else {}
            prompt, negative = image_prompt(scene, pipe.bible, list(refs))
            out = root / slug / f"scene-{n:02d}.png"
            t0 = time.monotonic()
            try:
                provider.generate(prompt, negative, out, seed=stable_seed(pipe.bible.series_id, ep.day, n - 1),
                                  width=r.width, height=r.height, refs=list(refs.values()))
            except Exception as e:  # one model failing must not sink the comparison
                log.warning("bakeoff %s scene %d: %s", model, n, e)
                shots.append(Shot(n, "", 0.0, 0.0, None, False, error=str(e)[:200]))
                continue
            secs = time.monotonic() - t0
            verdict = None
            if pipe.judge is not None:
                verdict, _ = pipe.judge.judge(out, scene, refs)
            notes = (verdict.artifacts if verdict else []) or []
            shots.append(Shot(
                scene=n, path=str(out.relative_to(root)), seconds=round(secs, 2),
                usd=image_cost(provider.model_id(bool(refs)), pipe.cfg.pricing), score=verdict.score if verdict else None,
                passed=passes(verdict, pipe.cfg.qa.min_score), notes=notes,
            ))
        results.append(ModelResult(model, shots))

    summaries = [m.summary() for m in results]
    (root / "results.json").write_text(json.dumps(
        {"day": ep.day, "scenes": scenes, "judge": type(pipe.judge).__name__ if pipe.judge else None,
         "summary": summaries, "shots": {m.model: [asdict(s) for s in m.shots] for m in results}},
        indent=2), encoding="utf-8")
    _write_report(root, ep, scenes, results, summaries, judged=pipe.judge is not None,
                  simulated=bool(getattr(pipe.judge, "simulated", False)))
    log.info("bakeoff: %s", root / "report.html")
    return root / "report.html"


def _fmt(v, kind: str) -> str:
    if v is None:
        return "–"
    return {"usd": f"${v:.3f}", "s": f"{v:.1f}s", "pct": f"{v:.0%}", "score": f"{v:.2f}"}.get(kind, str(v))


def _write_report(root: Path, ep: EpisodeRecord, scenes: list[int], results: list[ModelResult], summaries: list[dict],
                  judged: bool, simulated: bool) -> None:
    from .review import _page

    rows = "".join(
        f"<tr><td><code>{escape(s['model'])}</code></td><td>{_fmt(s['usd_per_image'], 'usd')}</td>"
        f"<td>{_fmt(s['seconds_per_image'], 's')}</td><td>{_fmt(s['mean_score'], 'score')}</td>"
        f"<td>{_fmt(s['pass_rate'], 'pct')}</td><td><b>{_fmt(s['usd_per_usable_frame'], 'usd')}</b></td>"
        f"<td>{s['errors']}</td></tr>"
        for s in summaries
    )
    head = "".join(f"<th>{escape(m.model)}</th>" for m in results)
    grid = ""
    for n in scenes:
        cells = ""
        for m in results:
            shot = next((s for s in m.shots if s.scene == n), None)
            if shot is None or shot.error:
                cells += f'<td class="small muted">{escape(shot.error if shot else "–")}</td>'
                continue
            badge = "" if shot.score is None else f'<span class="pill {"ok" if shot.passed else "bad"}">{shot.score}/5</span>'
            note = f'<div class="small muted">{escape("; ".join(shot.notes))}</div>' if shot.notes else ""
            cells += f'<td><img src="{escape(shot.path)}" alt="" style="width:100%;border-radius:8px">{badge}{note}</td>'
        grid += f'<tr><th class="small">Scene {n}<div class="muted">{escape(ep.script.scenes[n - 1].visual[:120])}</div></th>{cells}</tr>'
    judge_note = ("Scores from the simulated offline judge." if simulated else "Scores from the frame judge.") if judged else \
        "No judge configured, so cost and speed only."
    body = f"""<div class="eyebrow">Model bake-off · Day {ep.day}</div><h1>{escape(ep.script.title)}</h1>
<p class="muted">{escape(judge_note)} "$ per usable frame" divides spend by frames that passed QA, so it prices in re-rolls.</p>
<section class="panel"><table><tr><th>Model</th><th>$/image</th><th>Time</th><th>Mean score</th><th>Pass rate</th><th>$ per usable frame</th><th>Errors</th></tr>{rows}</table></section>
<section class="panel" style="overflow-x:auto"><table style="table-layout:fixed;min-width:{220 + 220 * len(results)}px"><tr><th style="width:220px">Shot</th>{head}</tr>{grid}</table></section>"""
    (root / "report.html").write_text(_page(f"Bake-off · Day {ep.day}", body), encoding="utf-8")
