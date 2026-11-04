"""Command-line interface.

    choices run                      advance the series one step (cron this hourly)
    choices status                   where every episode stands
    choices approve --day N          publish a reviewed episode
    choices reject  --day N --note   send it back to the writer with notes
    choices reroll  --day N --scene  regenerate one shot and re-render
    choices link    --day N --tiktok ID --instagram ID
    choices tally   --day N [--comments export.csv] [--force]
    choices metrics                  pull TikTok/Instagram analytics now
    choices variants --day N         hooks x formats for ad testing, no new media spend
    choices bakeoff --day N --models a,b   compare image models on our own shots
    choices cast                     (re)generate character reference sheets
    choices estimate                 projected cost per episode under each setting
    choices loop                     run forever (Docker/VM), ticking every hour
    choices demo    --days 3         full offline loop with a simulated audience
"""

from __future__ import annotations

import argparse
import logging
import random
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from .analytics import SimulatedInsights
from .config import load_pipeline
from .costs import estimate
from .models import Comment
from .notify import notify
from .pipeline import Pipeline
from .social.simulate import simulate_comments
from .store import Store


def _pipeline(args) -> Pipeline:
    cfg = load_pipeline(args.config)
    if args.offline:
        cfg = cfg.offline()
    if args.data_dir:
        cfg.data_dir = args.data_dir
    return Pipeline(cfg)


def cmd_run(args) -> int:
    print(_pipeline(args).tick().message)
    return 0


def cmd_status(args) -> int:
    pipe = _pipeline(args)
    days = pipe.store.episode_days()
    if not days:
        print("No episodes yet. `choices run` writes day 1.")
        return 0
    for d in days:
        ep = pipe.store.load_episode(d)
        if ep is None:
            continue
        line = f"day {d:>3}  {ep.status.value:<16} {ep.script.title}"
        if ep.tally:
            line += f"  -> {ep.tally.winner}: {ep.script.choice(ep.tally.winner).label} ({ep.tally.unique_voters} voters, {ep.tally.decided_by})"
        elif ep.voting_closes_at:
            line += f"  voting closes {ep.voting_closes_at:%Y-%m-%d %H:%M} UTC"
        if not ep.quality.passed:
            line += "  [quality checks failed]"
        print(line)
    print(f"\nSeries page: {pipe.store.root / 'index.html'}")
    return 0


def cmd_approve(args) -> int:
    ep = _pipeline(args).approve(args.day)
    print(f"day {ep.day} published. Voting closes {ep.voting_closes_at:%Y-%m-%d %H:%M} UTC.")
    if not ep.posts:
        print(f"Post the bundle in day-{ep.day:03d}/post/, then run: choices link --day {ep.day} --tiktok <id> --instagram <id>")
    return 0


def cmd_reject(args) -> int:
    ep = _pipeline(args).reject(args.day, args.note)
    print(f"day {ep.day} rewritten ({ep.quality.attempts} draft(s)); status {ep.status.value}")
    return 0


def cmd_reroll(args) -> int:
    ep = _pipeline(args).reroll(args.day, args.scene)
    print(f"day {ep.day} scene {args.scene} regenerated; status {ep.status.value}")
    return 0


def cmd_link(args) -> int:
    pipe = _pipeline(args)
    if not (args.tiktok or args.instagram):
        print("give --tiktok and/or --instagram", file=sys.stderr)
        return 2
    if args.tiktok:
        pipe.link(args.day, "tiktok", args.tiktok)
    if args.instagram:
        pipe.link(args.day, "instagram", args.instagram)
    print(f"day {args.day} linked")
    return 0


def cmd_tally(args) -> int:
    pipe = _pipeline(args)
    t = pipe.tally(args.day, extra_files=[Path(p) for p in args.comments], force=args.force)
    ep = pipe.store.load_episode(args.day)
    assert ep is not None
    print(
        f"day {args.day}: {t.winner} ({ep.script.choice(t.winner).label}) wins {t.decided_by} | "
        f"A {t.weighted_a:g} vs B {t.weighted_b:g} | {t.unique_voters} voters / {t.comments_seen} comments "
        f"| {t.llm_classified} via LLM"
    )
    return 0


class SimulatedAudience:
    """Comment source for the demo: a messy, opinionated crowd per platform."""

    def __init__(self, store: Store, platform: str, size: int, window_hours: float):
        self.store, self.platform, self.size, self.window = store, platform, size, window_hours

    def fetch(self, post_id: str) -> list[Comment]:
        day = int(post_id.rsplit("-", 1)[1])
        ep = self.store.load_episode(day)
        assert ep is not None and ep.voting_closes_at is not None
        rng = random.Random(f"{self.platform}:{day}")
        return simulate_comments(
            ep.script,
            post_id=post_id,
            platform=self.platform,
            opened_at=ep.voting_closes_at - timedelta(hours=self.window),
            closes_at=ep.voting_closes_at,
            n=self.size,
            lean_a=rng.uniform(0.3, 0.7),
            seed=day * 31 + len(self.platform),
        )


def cmd_metrics(args) -> int:
    pipe = _pipeline(args)
    n = pipe.refresh_metrics(force=True)
    print(f"analytics refreshed for {n} episode(s)")
    for k in pipe.kpis():
        if k.views:
            hold = f"{k.hold_3s:.0%}" if k.hold_3s is not None else "n/a"
            cpm = f"${k.cost_per_1k_views:.2f}" if k.cost_per_1k_views is not None else "n/a"
            print(f"day {k.day:>3}  {k.views:>8,} views  hold@3s {hold:>4}  cost/1k views {cpm}")
    return 0


def cmd_variants(args) -> int:
    pipe = _pipeline(args)
    vs = pipe.make_variants(args.day, hooks=args.hooks, formats=args.formats.split(","))
    for v in vs:
        print(f"{v.format:>5}  {v.angle:<16} {v.hook!r:44} -> {v.path}")
    print(f"\n{len(vs)} variants. Media cost: $0.00 (reused); only the hook-writing call was new.")
    return 0


def cmd_bakeoff(args) -> int:
    pipe = _pipeline(args)
    scenes = [int(x) for x in args.scenes.split(",")] if args.scenes else None
    report = pipe.bakeoff(args.day, [m.strip() for m in args.models.split(",") if m.strip()], scenes)
    print(f"report: {report}")
    return 0


def cmd_cast(args) -> int:
    made = _pipeline(args).ensure_cast(force=args.force)
    print(f"{len(made)} reference sheet(s) generated" if made else "cast already has reference sheets (use --force)")
    return 0


def cmd_estimate(args) -> int:
    cfg = load_pipeline(args.config)
    rows = estimate(cfg, scenes=args.scenes, reroll_rate=args.reroll_rate)
    print(f"Projected cost per episode. Prices checked {cfg.pricing.checked}; {args.scenes} shots, "
          f"{cfg.writing.target_seconds}s, {args.reroll_rate:.0%} of frames re-rolled by QA.\n")
    width = max(len(r.name) for r in rows)
    print(f"{'':{width}}  per episode   per month (30)")
    for r in rows:
        print(f"{r.name:{width}}  {'$' + format(r.per_episode, '.2f'):>11}   {'$' + format(r.per_month, '.2f'):>14}")
    default = rows[1]
    print("\nDefault breakdown: " + " · ".join(f"{k} ${v:.2f}" for k, v in sorted(default.lines.items(), key=lambda kv: -kv[1])))
    return 0


def cmd_loop(args) -> int:
    """Long-running mode for a container or VM: tick, sleep, repeat; never die on one bad run."""
    pipe = _pipeline(args)
    log = logging.getLogger("choices.loop")
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
        log.info("stopping after the current tick")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop["flag"]:
        try:
            log.info(pipe.tick().message)
        except Exception as e:
            log.exception("tick failed")
            notify(pipe.cfg.notify.webhook_url_env, f"{pipe.bible.title}: pipeline tick failed: {e}")
        deadline = time.monotonic() + args.interval + random.uniform(0, args.interval * 0.05)
        while not stop["flag"] and time.monotonic() < deadline:
            time.sleep(1)
    return 0


def cmd_demo(args) -> int:
    cfg = load_pipeline(args.config).offline()
    cfg.data_dir = args.data_dir or "runs/demo"
    cfg.publish.require_review = False
    if args.fast:
        cfg.render.width, cfg.render.height = 540, 960
    root = Path(cfg.data_dir)
    if root.exists():
        shutil.rmtree(root)
    pipe = Pipeline(cfg, sources={}, insights={})
    pipe._sources = {
        "tiktok": SimulatedAudience(pipe.store, "tiktok", 160, cfg.voting.window_hours),
        "instagram": SimulatedAudience(pipe.store, "instagram", 70, cfg.voting.window_hours),
    }
    sim = SimulatedInsights(pipe.store)
    pipe._insights = {"tiktok": sim, "instagram": sim}
    now = datetime.now().astimezone()
    for _ in range(args.days):
        res = pipe.tick(now)
        print(res.message)
        pipe.link(res.day, "tiktok", f"sim-tiktok-{res.day}")
        pipe.link(res.day, "instagram", f"sim-instagram-{res.day}")
        now += timedelta(hours=cfg.voting.window_hours, minutes=5)
    last = pipe.store.episode_days()[-1]
    t = pipe.tally(last, now)
    pipe.refresh_metrics(now, force=True)
    print(f"day {last}: audience picked {t.winner} ({t.unique_voters} voters)")
    if args.variants:
        vs = pipe.make_variants(1, hooks=2, formats=["9:16", "4:5", "1:1"])
        print(f"day 1: {len(vs)} ad variants (hooks x formats) at $0.00 extra media cost")
    print(f"\nOpen {pipe.store.root / 'index.html'} to browse the episodes. All costs and analytics here are simulated.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="choices", description="Daily audience-steered story series pipeline.")
    ap.add_argument("--config", default="config/pipeline.yaml")
    ap.add_argument("--offline", action="store_true", help="use local stand-ins for every paid provider")
    ap.add_argument("--data-dir", help="override data_dir from the config")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="advance the series one step").set_defaults(fn=cmd_run)
    sub.add_parser("status", help="list episodes").set_defaults(fn=cmd_status)

    p = sub.add_parser("approve", help="publish a reviewed episode")
    p.add_argument("--day", type=int, required=True)
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("reject", help="rewrite an episode with reviewer notes")
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--note", action="append", required=True, help="repeatable")
    p.set_defaults(fn=cmd_reject)

    p = sub.add_parser("reroll", help="regenerate one scene's image and re-render")
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--scene", type=int, required=True)
    p.set_defaults(fn=cmd_reroll)

    p = sub.add_parser("link", help="register post ids after posting manually")
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--tiktok")
    p.add_argument("--instagram")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("tally", help="collect comments and decide the vote")
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--comments", action="append", default=[], help="CSV/JSON comment export (repeatable)")
    p.add_argument("--force", action="store_true", help="tally before the voting window closes")
    p.set_defaults(fn=cmd_tally)

    sub.add_parser("metrics", help="pull TikTok/Instagram analytics now").set_defaults(fn=cmd_metrics)

    p = sub.add_parser("variants", help="hook x format variants for ad testing (reuses all media)")
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--hooks", type=int, default=3)
    p.add_argument("--formats", default="9:16,4:5,1:1")
    p.set_defaults(fn=cmd_variants)

    p = sub.add_parser("bakeoff", help="compare image models on this series' own shots")
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--models", required=True, help="comma-separated model ids (fal ids, or mock)")
    p.add_argument("--scenes", help="comma-separated scene numbers (default 1,2,3)")
    p.set_defaults(fn=cmd_bakeoff)

    p = sub.add_parser("cast", help="generate character reference sheets")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_cast)

    p = sub.add_parser("estimate", help="projected cost per episode under each setting")
    p.add_argument("--scenes", type=int, default=5)
    p.add_argument("--reroll-rate", type=float, default=0.3)
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("loop", help="run forever, ticking every --interval seconds")
    p.add_argument("--interval", type=int, default=3600)
    p.set_defaults(fn=cmd_loop)

    p = sub.add_parser("demo", help="offline end-to-end run with a simulated audience")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--fast", action="store_true", help="render at 540x960")
    p.add_argument("--variants", action="store_true", help="also cut ad variants of day 1")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return args.fn(args)
    except (ValueError, RuntimeError, anthropic.APIError) as e:
        logging.getLogger("choices").error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
