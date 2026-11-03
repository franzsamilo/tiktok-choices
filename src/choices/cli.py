"""Command-line interface.

    choices run                      advance the series one step (cron this hourly)
    choices status                   where every episode stands
    choices approve --day N          publish a reviewed episode
    choices reject  --day N --note   send it back to the writer with notes
    choices reroll  --day N --scene  regenerate one shot and re-render
    choices link    --day N --tiktok ID --instagram ID
    choices tally   --day N [--comments export.csv] [--force]
    choices demo    --days 3         full offline loop with a simulated audience
"""

from __future__ import annotations

import argparse
import logging
import random
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from .config import load_pipeline
from .models import Comment
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


def cmd_demo(args) -> int:
    cfg = load_pipeline(args.config).offline()
    cfg.data_dir = args.data_dir or "runs/demo"
    cfg.publish.require_review = False
    if args.fast:
        cfg.render.width, cfg.render.height = 540, 960
    root = Path(cfg.data_dir)
    if root.exists():
        shutil.rmtree(root)
    pipe = Pipeline(cfg, sources={})
    pipe._sources = {
        "tiktok": SimulatedAudience(pipe.store, "tiktok", 160, cfg.voting.window_hours),
        "instagram": SimulatedAudience(pipe.store, "instagram", 70, cfg.voting.window_hours),
    }
    now = datetime.now().astimezone()
    for _ in range(args.days):
        res = pipe.tick(now)
        print(res.message)
        pipe.link(res.day, "tiktok", f"sim-tiktok-{res.day}")
        pipe.link(res.day, "instagram", f"sim-instagram-{res.day}")
        now += timedelta(hours=cfg.voting.window_hours, minutes=5)
    last = pipe.store.episode_days()[-1]
    t = pipe.tally(last, now)
    print(f"day {last}: audience picked {t.winner} ({t.unique_voters} voters)")
    print(f"\nOpen {pipe.store.root / 'index.html'} to browse the episodes.")
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

    p = sub.add_parser("demo", help="offline end-to-end run with a simulated audience")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--fast", action="store_true", help="render at 540x960")
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
