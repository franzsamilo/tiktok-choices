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
- **Ken Burns by default, image-to-video opt-in.** Motion on stills is far cheaper and good enough for most shots. Turn on `providers.video: fal` when the episode earns it.
- **Comment voting, not poll stickers.** Poll-sticker results aren't available through either platform's API.
- **TikTok-safe layout.** Nothing important goes in the bottom ~20% (caption UI) or the right-hand action rail.
