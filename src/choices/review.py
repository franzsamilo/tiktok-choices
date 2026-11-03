"""Static HTML pages for the human in the loop.

``day-NNN/review.html`` puts everything needed for an approve/reject call on
one screen: the render, the script, the compiled prompts, every automated
check and what the critic said, the vote that led here, and the exact
commands to run. ``index.html`` is the series at a glance.
"""

from __future__ import annotations

import json
from html import escape
from typing import TYPE_CHECKING

from .models import EpisodeRecord, EpisodeStatus

if TYPE_CHECKING:
    from .pipeline import Pipeline

CSS = """
:root{--bg:#0d0f12;--panel:#15181d;--line:#262b33;--text:#e8eaed;--muted:#9aa3ad;--amber:#ffc45c;--a:#e5484d;--b:#12a594;--ok:#3dd68c;--warn:#f5a524;--bad:#ff6369}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f6f7f9;--panel:#fff;--line:#e3e6ea;--text:#16191d;--muted:#5d6670}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 64px}
a{color:var(--amber)}h1{font-size:28px;margin:4px 0 6px;letter-spacing:-.01em}h2{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.eyebrow{color:var(--amber);font-weight:700;letter-spacing:.14em;font-size:12px;text-transform:uppercase}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 22px}.pill{border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;font-weight:600}
.pill.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}.pill.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}.pill.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
.grid{display:grid;grid-template-columns:minmax(0,360px) minmax(0,1fr);gap:20px}@media (max-width:820px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:20px}
video,.poster{width:100%;aspect-ratio:9/16;border-radius:12px;background:#000;display:block;object-fit:cover}
.q{font-size:20px;font-weight:700;margin:0 0 14px}
.opt{display:flex;gap:12px;align-items:flex-start;padding:12px;border-radius:10px;border:1px solid var(--line);margin-bottom:10px}
.badge{flex:none;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:800;color:#fff}.A .badge{background:var(--a)}.B .badge{background:var(--b)}
.opt b{display:block}.muted{color:var(--muted)}.small{font-size:13px}
ul.issues{list-style:none;padding:0;margin:0}ul.issues li{padding:8px 0;border-top:1px solid var(--line)}ul.issues li:first-child{border-top:0}
.sev{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-right:8px}.sev.blocker{color:var(--bad)}.sev.warning{color:var(--warn)}
pre,code{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}pre{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;overflow-x:auto;white-space:pre-wrap}
.scenes{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px}
.scene{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}.scene img{width:100%;aspect-ratio:9/16;object-fit:cover;display:block}
.scene .body{padding:12px 14px}details summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:8px}
.bar{display:flex;height:34px;border-radius:8px;overflow:hidden;font-weight:700;font-size:13px;color:#fff;margin:8px 0}.bar div{display:flex;align-items:center;padding:0 10px;min-width:44px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px 8px;border-top:1px solid var(--line)}th{color:var(--muted);font-weight:600}
.days{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}.day{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;text-decoration:none;color:inherit}
.day img{width:100%;aspect-ratio:9/16;object-fit:cover;display:block}.day .body{padding:10px 12px}
"""


def _page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>'
        f"<style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


def _status_pill(ep: EpisodeRecord) -> str:
    cls = {EpisodeStatus.AWAITING_REVIEW: "warn", EpisodeStatus.PUBLISHED: "ok", EpisodeStatus.RESOLVED: "ok"}.get(ep.status, "")
    return f'<span class="pill {cls}">{escape(ep.status.value.replace("_", " "))}</span>'


def _tally_html(ep: EpisodeRecord) -> str:
    t = ep.tally
    if t is None:
        return ""
    total = t.weighted_a + t.weighted_b or 1
    pa = round(100 * t.weighted_a / total)
    rows = "".join(
        f"<tr><td>{escape(p)}</td><td>{c.a:g}</td><td>{c.b:g}</td><td>{c.voters}</td></tr>" for p, c in t.by_platform.items()
    )
    quotes = ""
    for side in ("A", "B"):
        qs = "".join(f"<li>“{escape(q)}”</li>" for q in t.top_comments.get(side, []))
        if qs:
            quotes += f'<p class="small muted">Top {side} comments</p><ul class="small">{qs}</ul>'
    decided = {"votes": "by the vote", "tiebreak": "by coin flip (tie)", "low_turnout": "by coin flip (low turnout)"}[t.decided_by]
    return f"""<section class="panel"><h2>The vote</h2>
<p><b>{escape(t.winner)}: {escape(ep.script.choice(t.winner).label)}</b> wins, {decided}.</p>
<div class="bar"><div style="width:{pa}%;background:var(--a)">A {pa}%</div><div style="width:{100 - pa}%;background:var(--b)">B {100 - pa}%</div></div>
<p class="small muted">{t.unique_voters} unique voters from {t.comments_seen} comments. {t.comments_excluded} excluded,
{t.ambiguous} unreadable, {t.llm_classified} read by the LLM classifier. Full trail: <a href="votes_audit.json">votes_audit.json</a></p>
<table><tr><th>Platform</th><th>A</th><th>B</th><th>Voters</th></tr>{rows}</table>{quotes}</section>"""


def write_review_page(pipe: Pipeline, ep: EpisodeRecord) -> None:
    s = ep.script
    d = pipe.store.day_dir(ep.day)
    prompts = {}
    pfile = d / "prompts.json"
    if pfile.exists():
        prompts = {p["scene"]: p for p in json.loads(pfile.read_text(encoding="utf-8"))}

    q = ep.quality
    qpill = '<span class="pill ok">checks passed</span>' if q.passed else '<span class="pill bad">needs attention</span>'
    issues = "".join(
        f'<li><span class="sev {i.severity}">{i.severity}</span>{escape(i.message)}</li>' for i in q.issues
    ) or '<li class="muted">No issues found.</li>'
    critic = ""
    if q.critic:
        c = q.critic
        critic = (
            f'<p class="small">Hook strength <b>{c.hook_strength}/5</b> · '
            f'dilemma {"balanced" if c.dilemma_is_balanced else "<b>unbalanced</b>"}</p>'
        )

    video = (
        '<video controls playsinline preload="metadata" poster="cover.jpg" src="final.mp4"></video>'
        if (d / "final.mp4").exists()
        else '<div class="poster"></div>'
    )

    prev = ""
    if ep.outcome:
        o = ep.outcome
        how = {"votes": f"{o.share:.0%} of {o.unique_voters} voters", "tiebreak": "tie, coin flip", "low_turnout": "low turnout, coin flip"}[o.decided_by]
        prev = (
            f'<section class="panel"><h2>Previously</h2><p class="small muted">Day {o.day}: {escape(o.question)}</p>'
            f"<p>Audience chose <b>{escape(o.chosen_key)}: {escape(o.chosen_label)}</b> ({how}).</p></section>"
        )

    opts = ""
    for key in ("A", "B"):
        o = s.choice(key)  # type: ignore[arg-type]
        opts += (
            f'<div class="opt {key}"><div class="badge">{key}</div><div><b>{escape(o.label)}</b>'
            f'<span class="small muted">votes on: {escape(", ".join(o.keywords))}</span>'
            f'<div class="small">{escape(o.stakes)}</div></div></div>'
        )

    cmds = ""
    if ep.status == EpisodeStatus.AWAITING_REVIEW:
        cmds = (
            f'<section class="panel"><h2>Decide</h2><pre>choices approve --day {ep.day}\n'
            f'choices reject  --day {ep.day} --note "what to change"\n'
            f"choices reroll  --day {ep.day} --scene N   # regenerate one shot</pre></section>"
        )
    elif ep.status == EpisodeStatus.PUBLISHED and not ep.posts:
        cmds = (
            f'<section class="panel"><h2>Link the posts</h2><p class="small">Post the bundle in <code>post/</code>, then:</p>'
            f"<pre>choices link --day {ep.day} --tiktok VIDEO_ID --instagram MEDIA_ID</pre></section>"
        )

    timings = {t.index: t for t in ep.timings}
    scenes = ""
    for i, sc in enumerate(s.scenes):
        p = prompts.get(i + 1, {})
        tm = timings.get(i)
        when = f"{tm.start:.1f}s, {tm.duration:.1f}s long" if tm else ""
        chars = f' · {escape(", ".join(sc.characters))}' if sc.characters else ""
        scenes += (
            f'<article class="scene"><img loading="lazy" src="scenes/{i + 1:02d}.png" alt="Scene {i + 1}">'
            f'<div class="body"><div class="small muted">Scene {i + 1} · {when}{chars}</div><p>{escape(sc.narration)}</p>'
            f'<details><summary>Image prompt</summary><pre>{escape(p.get("image", sc.visual))}</pre></details>'
            f'<details><summary>Motion</summary><pre>{escape(p.get("motion", sc.motion))}</pre></details></div></article>'
        )

    calls = "".join(
        f"<tr><td>{escape(c.purpose)}</td><td>{escape(c.served_by or c.model)}</td><td>{c.input_tokens:,}</td>"
        f"<td>{c.cache_read_tokens:,}</td><td>{c.output_tokens:,}</td></tr>"
        for c in ep.llm_calls
    )
    calls_html = (
        f'<section class="panel"><h2>Model calls</h2><table><tr><th>Purpose</th><th>Model</th><th>Input</th>'
        f"<th>Cache read</th><th>Output</th></tr>{calls}</table></section>"
        if calls
        else ""
    )
    notes = "".join(f"<li>{escape(n)}</li>" for n in ep.reviewer_notes)
    notes_html = f'<section class="panel"><h2>Reviewer notes applied</h2><ul>{notes}</ul></section>' if notes else ""

    body = f"""<div class="eyebrow">{escape(pipe.bible.title)} · Day {ep.day}</div>
<h1>{escape(s.title)}</h1>
<div class="pills">{_status_pill(ep)}{qpill}<span class="pill">{q.attempts} draft{'s' if q.attempts != 1 else ''}</span><a class="pill" href="../index.html">All days</a></div>
<div class="grid"><div>{video}<p class="small muted">Hook: “{escape(s.hook)}”</p></div>
<div>{prev}<section class="panel"><h2>The choice</h2><p class="q">{escape(s.question)}</p>{opts}</section>
{_tally_html(ep)}{cmds}
<section class="panel"><h2>Automated review</h2>{critic}<ul class="issues">{issues}</ul></section>{notes_html}</div></div>
<h2 style="margin-top:12px">Scenes</h2><div class="scenes">{scenes}</div>
<section class="panel" style="margin-top:20px"><h2>Caption</h2><p>{escape(s.caption)}</p><p class="small muted">{escape(" ".join("#" + h for h in s.hashtags))}</p></section>
{calls_html}"""
    (d / "review.html").write_text(_page(f"{pipe.bible.title} · Day {ep.day}", body), encoding="utf-8")


def write_index(pipe: Pipeline) -> None:
    cards = ""
    for day in reversed(pipe.store.episode_days()):
        ep = pipe.store.load_episode(day)
        if ep is None:
            continue
        result = ""
        if ep.tally:
            t = ep.tally
            total = t.weighted_a + t.weighted_b or 1
            share = (t.weighted_a if t.winner == "A" else t.weighted_b) / total
            result = f'<div class="small"><b>{t.winner}: {escape(ep.script.choice(t.winner).label)}</b> · {share:.0%} of {t.unique_voters}</div>'
        cards += (
            f'<a class="day" href="day-{day:03d}/review.html"><img loading="lazy" src="day-{day:03d}/cover.jpg" alt="">'
            f'<div class="body"><div class="small muted">Day {day} · {escape(ep.status.value.replace("_", " "))}</div>'
            f"<b>{escape(ep.script.title)}</b><div class=\"small muted\">{escape(ep.script.question)}</div>{result}</div></a>"
        )
    st = pipe.state()
    body = (
        f'<div class="eyebrow">Series</div><h1>{escape(pipe.bible.title)}</h1>'
        f'<p class="muted">{escape(pipe.bible.logline)}</p>'
        f'<div class="pills"><span class="pill">{len(pipe.store.episode_days())} episodes</span>'
        f'<span class="pill">Now: {escape(st.location)}</span></div><div class="days">{cards}</div>'
    )
    (pipe.store.root / "index.html").write_text(_page(pipe.bible.title, body), encoding="utf-8")
