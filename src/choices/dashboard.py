"""Series dashboard: KPI tiles, cost-by-stage and views-per-episode charts, and the table behind them.

Static SVG rendered server-side plus a small hover layer. The colours are the
first five slots of a validated categorical palette, in fixed order, stepped
separately for light and dark surfaces. Every number a tooltip shows is also
in the table, so hover is never the only way to read a value.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from .analytics import EpisodeKPI, SeriesKPI, series_kpi
from .costs import breakdown

if TYPE_CHECKING:
    from .pipeline import Pipeline

# Fixed order: identity follows the stage, never its rank.
STAGES = [
    ("video", "Video", ("video",)),
    ("images", "Images", ("image",)),
    ("writing", "Writing", ("writer", "critic", "hooks")),
    ("qa", "Frame QA", ("frame-judge",)),
    ("voice", "Voice & votes", ("voice", "vote-classifier")),
]

CSS = """
.viz-root{--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--grid:#2a2f37;--tip-bg:#0b0d10}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]) .viz-root{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--grid:#e6e8eb;--tip-bg:#ffffff}}
:root[data-theme="light"] .viz-root{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--grid:#e6e8eb;--tip-bg:#ffffff}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 20px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .label{font-size:13px;color:var(--muted)}.kpi .value{font-size:26px;font-weight:600;letter-spacing:-.01em;margin-top:2px}
.kpi .sub{font-size:12px;color:var(--muted)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);margin:0 0 8px}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg.chart text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
svg.chart .mark{cursor:default;outline:none}svg.chart .mark:hover,svg.chart .mark:focus{filter:brightness(1.15)}
.tip{position:fixed;pointer-events:none;background:var(--tip-bg);color:var(--text);border:1px solid var(--line);border-radius:8px;
 padding:8px 10px;font-size:12px;box-shadow:0 6px 20px rgba(0,0,0,.25);display:none;z-index:10;min-width:140px}
.tip b{display:block;font-size:14px}.tip .k{display:inline-block;width:12px;height:2px;margin-right:6px;vertical-align:middle}
.tablewrap{overflow-x:auto}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.sim{font-size:12px;color:var(--warn)}
"""

JS = """
(() => {
  const root = document.querySelector('.viz-root') || document.body;  // inside it, so series colours resolve
  const tip = document.createElement('div'); tip.className = 'tip'; root.appendChild(tip);
  const show = (el, x, y) => {
    tip.replaceChildren();
    const v = document.createElement('b'); v.textContent = el.dataset.value; tip.appendChild(v);
    const l = document.createElement('div');
    if (el.dataset.color) { const k = document.createElement('span'); k.className = 'k'; k.style.background = el.dataset.color; l.appendChild(k); }
    l.appendChild(document.createTextNode(el.dataset.label)); tip.appendChild(l);
    if (el.dataset.extra) { const e = document.createElement('div'); e.className = 'muted'; e.textContent = el.dataset.extra; tip.appendChild(e); }
    tip.style.display = 'block';
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - r.width - 8, x + 12) + 'px';
    tip.style.top = Math.max(8, y - r.height - 12) + 'px';
  };
  document.querySelectorAll('svg.chart .hit').forEach(el => {
    el.addEventListener('pointermove', e => show(el, e.clientX, e.clientY));
    el.addEventListener('pointerleave', () => tip.style.display = 'none');
    el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); show(el, r.right, r.top); });
    el.addEventListener('blur', () => tip.style.display = 'none');
  });
})();
"""


def _compact(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return f"{n:.0f}"


def _pct(x: float | None) -> str:
    return f"{x:.0%}" if x is not None else "–"


def _usd(x: float | None, digits: int = 2) -> str:
    return f"${x:.{digits}f}" if x is not None else "–"


def _stage_costs(ep, pricing) -> dict[str, float]:
    b = breakdown(ep, pricing)
    return {key: sum(b.get(k, 0.0) for k in keys) for key, _, keys in STAGES}


def _nice_max(v: float, ticks: int = 4) -> float:
    """Smallest round axis top >= v with `ticks` evenly spaced round gridlines."""
    if v <= 0:
        return 1.0
    for mag in (0.01, 0.1, 1, 10, 100, 1000, 10_000, 100_000, 1_000_000, 10_000_000):
        for step in (1, 2, 2.5, 4, 5):
            if step * mag * ticks >= v:
                return step * mag * ticks
    return v


def _bars_svg(days: list[int], stacks: list[list[tuple[str, str, float, str]]], fmt, title: str, extras: list[str]) -> str:
    """Vertical bars (optionally stacked). Each segment: (css color var, label, value, extra)."""
    W, H, L, B, T = 560, 220, 44, 26, 18
    n = max(1, len(days))
    slot = (W - L - 8) / n
    bw = min(24.0, slot * 0.6)
    top = _nice_max(max((sum(s[2] for s in st) for st in stacks), default=0))
    y = lambda v: T + (H - T - B) * (1 - v / top)  # noqa: E731
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}">']
    for i in range(5):
        v = top * i / 4
        out.append(f'<line x1="{L}" x2="{W}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{escape(fmt(v))}</text>')
    for i, (day, stack) in enumerate(zip(days, stacks, strict=True)):
        cx = L + slot * i + slot / 2
        x = cx - bw / 2
        base = 0.0
        segs = [s for s in stack if s[2] > 0]
        for j, (color, label, value, extra) in enumerate(segs):
            y0, y1 = y(base), y(base + value)
            h = max(0.0, y0 - y1 - (2 if j < len(segs) - 1 else 0))  # 2px surface gap between segments
            last = j == len(segs) - 1
            r = min(4, h / 2, bw / 2) if last else 0
            if last and r:
                # rounded data-end (top), square at the baseline side
                path = (f"M{x:.1f},{y0:.1f} V{y1 + r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} H{x + bw - r:.1f} "
                        f"Q{x + bw:.1f},{y1:.1f} {x + bw:.1f},{y1 + r:.1f} V{y0:.1f} Z")
                out.append(f'<path d="{path}" fill="var({color})"/>')
            else:
                out.append(f'<rect x="{x:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="var({color})"/>')
            # hit target is the full slot column for this segment, bigger than the paint
            out.append(
                f'<rect class="mark hit" tabindex="0" x="{cx - slot / 2:.1f}" y="{y1:.1f}" width="{slot:.1f}" '
                f'height="{max(6.0, y0 - y1):.1f}" fill="transparent" data-value="{escape(fmt(value))}" '
                f'data-label="{escape(f"Day {day} · {label}")}" data-color="var({color})" data-extra="{escape(extra)}"/>'
            )
            base += value
        out.append(f'<text x="{cx:.1f}" y="{H - 8}" text-anchor="middle">{day}</text>')
        if stacks and len(days) <= 14:
            total = sum(s[2] for s in stack)
            out.append(f'<text x="{cx:.1f}" y="{y(total) - 5:.1f}" text-anchor="middle">{escape(extras[i] or fmt(total))}</text>')
    out.append("</svg>")
    return "".join(out)


def dashboard_html(pipe: Pipeline, kpis: list[EpisodeKPI]) -> str:
    if not kpis:
        return ""
    s: SeriesKPI = series_kpi(kpis)
    tiles = [
        ("Episodes", str(s.episodes), ""),
        ("Avg cost per episode", _usd(s.avg_cost), f"{_usd(s.spend)} total"),
        ("Views", _compact(s.views) if s.views else "–", "TikTok + Reels"),
        ("Still watching at 3s", _pct(s.hold_3s), "hook strength"),
        ("Votes per 1K views", f"{s.votes_per_1k:.1f}" if s.votes_per_1k is not None else "–", "comment engagement"),
        ("Cost per 1K views", _usd(s.cost_per_1k_views), "organic CPM"),
    ]
    tiles_html = "".join(
        f'<div class="kpi"><div class="label">{escape(a)}</div><div class="value">{escape(b)}</div>'
        f'<div class="sub">{escape(c)}</div></div>'
        for a, b, c in tiles
    )
    sim = '<p class="sim">Simulated: offline run, so costs are priced as if live and analytics are synthetic.</p>' if s.simulated else ""

    eps = [pipe.store.load_episode(k.day) for k in kpis]
    days = [k.day for k in kpis]
    cost_stacks, cost_labels = [], []
    for ep, k in zip(eps, kpis, strict=True):
        sc = _stage_costs(ep, pipe.cfg.pricing) if ep else {}
        cost_stacks.append([(f"--s{i + 1}", name, sc.get(key, 0.0), "") for i, (key, name, _) in enumerate(STAGES)])
        cost_labels.append(_usd(k.cost_usd))
    cost_svg = _bars_svg(days, cost_stacks, lambda v: f"${v:.2f}" if v < 10 else f"${v:.0f}", "Cost per episode by stage", cost_labels)
    legend = "".join(f'<span><i style="background:var(--s{i + 1})"></i>{escape(name)}</span>' for i, (_, name, _) in enumerate(STAGES))

    view_stacks = [[("--s1", "views", float(k.views), f"hold@3s {_pct(k.hold_3s)} · completion {_pct(k.completion)}")] for k in kpis]
    views_svg = _bars_svg(days, view_stacks, _compact, "Views per episode", ["" for _ in kpis])

    rows = ""
    for ep, k in zip(eps, kpis, strict=True):
        sc = _stage_costs(ep, pipe.cfg.pricing) if ep else {}
        rows += (
            f'<tr><td><a href="day-{k.day:03d}/review.html">{k.day}</a></td><td>{escape(k.title)}</td>'
            f'<td class="num">{k.views:,}</td><td class="num">{_pct(k.hold_3s)}</td><td class="num">{_pct(k.completion)}</td>'
            f'<td class="num">{f"{k.votes_per_1k:.1f}" if k.votes_per_1k is not None else "–"}</td>'
            + "".join(f'<td class="num">{_usd(sc.get(key, 0.0))}</td>' for key, _, _ in STAGES)
            + f'<td class="num"><b>{_usd(k.cost_usd)}</b></td><td class="num">{_usd(k.cost_per_1k_views, 3)}</td></tr>'
        )
    head = (
        "<tr><th>Day</th><th>Episode</th><th class=num>Views</th><th class=num>Hold@3s</th><th class=num>Completion</th>"
        "<th class=num>Votes/1K</th>" + "".join(f"<th class=num>{escape(n)}</th>" for _, n, _ in STAGES)
        + "<th class=num>Total</th><th class=num>$/1K views</th></tr>"
    )
    return f"""<div class="viz-root"><section><h2>Performance &amp; cost</h2>{sim}<div class="kpis">{tiles_html}</div>
<div class="charts"><section class="panel"><h2>Cost per episode by stage</h2><div class="legend">{legend}</div>{cost_svg}</section>
<section class="panel"><h2>Views per episode</h2><div class="legend"><span>Hover a bar for hook hold and completion</span></div>{views_svg}</section></div>
<section class="panel tablewrap"><h2>By episode</h2><table>{head}{rows}</table></section></section></div>
<style>{CSS}</style><script>{JS}</script>"""
