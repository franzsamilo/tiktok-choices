# Design notes

This system has to run every day, unattended, on content that goes in front of strangers. Most of the interesting decisions are about where *not* to trust a model, and what to do instead.

## 1. Models write; code enforces

The writer returns a typed `EpisodeScript` through structured outputs, so there's no parsing JSON out of prose. Constraints that a model is unreliable at self-policing are enforced afterwards, by the cheapest layer that can do the job:

| Problem | Who handles it | Why |
|---|---|---|
| `#` in hashtags, missing `?`, keyword shared by both options, character names used as vote keywords | `normalize()`: fixed silently in code | Certain fixes shouldn't cost an LLM round trip. |
| Too many or too few scenes, narration over the time budget, 7-word option labels, gore in an image prompt, a dead character on camera | `rule_issues()`: blockers fed back as revision notes | Counting and lookups are code's job. Models routinely miss word counts. |
| Continuity with canon, whether the dilemma is balanced, hook strength | `LLMCritic`: a separate call with a narrower brief | It reads the draft cold, so it catches things the writer argued itself into. |
| Taste, brand risk, "this frame looks wrong" | A human on the review page | The final call on what goes out. |

The critic only runs once the rule checks pass, so it never reviews a draft that's already rejected. After `max_revisions` the episode still goes to review, clearly flagged as failing, rather than silently shipping or silently stalling. Auto-publish (`require_review: false`) never applies to a flagged episode.

## 2. Character consistency is a data problem, not a prompting problem

The biggest quality problem in serialized AI video is a character who looks different every day. The writer is forbidden from describing recurring characters. It only names them. `media/prompting.py` injects a fixed appearance string from the bible into every image prompt. No model gets to paraphrase Lola Cora's glasses.

The protagonist is never shown on camera: the story is told in second person with POV framing. That removes the hardest consistency problem entirely, and it helps the format, because viewers project themselves into "you".

## 3. Votes: rules first, LLM for the leftovers

Most comments are trivial ("A", "b po", "🅰️"). Sending thousands of them to a model would be slow, costly, and non-deterministic: a re-run could change the winner. The rule parser settles those and records *why* (`explicit`, `keyword`, `yes_no`).

Only comments with conflicting, negated or unrecognised signals go to the LLM, batched, with:

- comments referenced by local ids (`c0`, `c1`) and hallucinated ids dropped;
- text fenced in tags and the system prompt stating that instructions inside comments are just comments.

The voting policy (one vote per person per platform, latest vote wins, owner and late comments excluded, capped like-weighting, a seeded coin flip on ties) is plain code with an audit file. If someone asks why their vote didn't count, there's a row that answers it.

Top comments are quoted back to the writer so the audience sees themselves in the story. That makes them a prompt-injection channel, so anything that reads like an instruction to a model is never quoted, and the quotes are fenced as untrusted in the writer prompt.

## 4. Picture follows voice

Each scene's duration comes from its narration (TTS with word timestamps), not the other way round, so audio and picture can't drift. Word timings drive the karaoke captions, and most viewers watch muted, so for them the captions are the narration.

## 5. Resumable by construction

Every stage writes its artifact to disk and the state machine skips anything that already exists. A provider timeout during scene 4 of 6 resumes at scene 4. Re-rolling one bad shot regenerates one image, not the episode. Seeds are derived from `(series, day, scene)`, so re-runs are reproducible.

## 6. Prompt layout for caching

Everything stable (role, story bible, craft rules) is in the system prompt, built once and byte-identical across days. Everything daily (canon, yesterday's vote, revision notes) is in the user turn. Token usage, including cache reads, is recorded per call on the episode and shown on the review page, so cost per episode is visible rather than guessed.

## 7. Defaults chosen for the real world

- **Export mode by default.** Both platforms gate API publishing behind app review, so the pipeline produces a ready-to-post bundle, and `choices link` connects the posts afterwards.
- **Image-to-video only where it earns its cost.** Hero shots get real motion; the rest get a slow camera move on the still (see §8).
- **Comment voting, not poll stickers.** Poll-sticker results aren't available through either platform's API.
- **TikTok-safe layout.** Nothing important goes in the bottom ~20% (caption UI) or the right-hand action rail.

## 8. Cost is a first-class output

Every paid call writes a `CostEntry`, and LLM spend is derived from the token usage already on each call, so every episode has an itemised bill on its review page and the series dashboard shows cost per 1K views. Three levers do most of the work (`choices estimate` prints the comparison from the live price table):

| Setting | Projected $/episode |
|---|---|
| Stills + Ken Burns only | $0.95 |
| **Hero shots animated (2 of 5), native audio off (default)** | **$3.19** |
| Every shot animated, audio off | $6.55 |
| Every shot animated, audio on | $9.35 |

- **Hero shots.** The writer flags at most two shots where motion carries the moment; only those get image-to-video.
- **Native audio off.** Narration is generated separately anyway, and it's what the captions are timed to.
- **Clips sized to the shot.** Kling v3 bills whole seconds from 3 to 15, so a 7.4s shot costs 8s, not a padded 10s.

A per-episode budget backs this up. The ledger is shared by the parallel workers and reserves money atomically before each optional spend (a re-roll, a clip). When the budget runs out, the pipeline *degrades*: it skips the remaining animation, keeps the episode, and writes the trade-off on the review page. The minimum episode (stills, voice, script) is never blocked.

Offline runs price the mock calls as if they were the configured real models, so the dashboard, budget and estimates behave like production. They're always labelled *simulated*.

## 9. A vision model checks every frame before it gets expensive

Image models fail in ways no text check can see: six fingers, a garbled sign, the wrong face, a "safe" prompt that came back gory. The frame judge (Claude, vision, low effort, on a downscaled 768px JPEG) checks each still against the shot description, the character reference sheets and the content rules, *before* the still can become a ~$1 clip. A failed frame is re-rolled with a new seed plus the judge's one-line fix. If nothing passes, the best attempt is kept and flagged for the human, and it isn't animated. One check costs about $0.02; one skipped bad clip pays for roughly fifty.

## 10. Reference images, not adjectives, for consistency

`choices cast` generates one reference sheet per character, once. Every shot with that character goes to a multi-reference edit model (Nano Banana 2, up to 14 images) with the sheet attached, and the prompt maps "image 1 is Lola Cora". The locked text description stays as a second anchor, and the judge checks identity against the same sheet.

## 11. Closing the loop with analytics

Comments decide the story; analytics decide the craft. The pipeline pulls TikTok's per-second retention curve and Instagram's Reels skip rate and computes four KPIs per episode:

- 3-second hold (the hook's report card),
- completion (did they reach the choice card),
- votes per 1K views (the format's engagement rate),
- cost per 1K views.

The best and worst hooks and the highest-voted dilemmas go into the next writer prompt as a `<performance_data>` block: measured data, labelled as data, never overriding the bible. It's deterministic and needs no extra LLM call. The review page shows exactly what the writer was told.

## 12. One episode, many test creatives

For paid distribution, the expensive assets (stills, clips, narration) don't change between creatives; only the hook text and aspect ratio do. `choices variants` writes alternative hooks in one low-effort call, each with a different angle (curiosity, threat, stakes, ...), written for cold audiences who don't know the characters and required to be true to the episode. It then re-renders 9:16, 4:5 and 1:1 cuts. Each format shares one picture and sound pass, and every hook only re-burns the overlay. Marginal media cost: $0.

## 13. Why a workflow, not an autonomous agent

The daily loop is a deterministic state machine with LLM calls at specific steps, not an open-ended agent with tools. For this job that's the better harness:

- **Cost is bounded and predictable.** Every model call has a known purpose, effort level and budget line. An agent deciding for itself how many images to try has neither.
- **It's auditable.** Each step leaves an artifact on disk, so "why did day 6 look like that?" has a file-level answer.
- **Failures are local.** A timeout resumes at the failed step instead of re-planning the whole episode.
- **Judgment goes where it's needed.** Models write, critique and judge frames, where language and vision matter; code counts, dedupes, prices and schedules, where it's exact.

An agent would earn its place on open-ended work, like researching a new niche or exploring a series format from scratch. Deciding when an episode is good enough to publish stays with a human.
