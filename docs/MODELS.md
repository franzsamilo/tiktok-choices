# Model notes (checked 2026-11-04)

Which model does each job, why, and what would make me switch. Prices are list
prices on fal / ElevenLabs / Anthropic as of the date above. Rankings come
from public leaderboards and vendor pages, **not yet from this repo's own
bake-off**. The point of `choices bakeoff` is to replace these second-hand
opinions with first-hand ones on our own shots.

## Video: Kling v3 Pro, native audio off, hero shots only

| Model (fal) | $/s | Why it's here |
|---|---|---|
| **`fal-ai/kling-video/v3/pro/image-to-video`** (default) | $0.112 (audio off) | Top of the LLM Stats text-to-video arena on 2026-11-04. Takes `duration` as any whole second from 3 to 15, so a 7.4s shot bills 8s, not a padded 10s. |
| `fal-ai/veo3.1/fast/image-to-video` | $0.10 (audio off) | Cheaper per second, but durations are fixed at 4/6/8s and capped at 8s, so it overpays on odd-length shots and can't cover long ones. |
| `bytedance/seedance-2.0/image-to-video` | ~$0.30 at 720p | Strong arena results, ~3x the price. Worth a bake-off for shots with complex motion, not as a default. |
| `fal-ai/kling-video/v2.6/pro/image-to-video` | $0.07 | Budget fallback; 5s or 10s only. |
| Sora 2 | n/a | Reported removed from the API in September 2026. Not considered. |

Opinions:

- **Native audio is the wrong feature for this format.** It's impressive in a
  single clip, but the series needs one narrator voice across six shots, and
  word timings for captions. Generating speech separately is cheaper (Kling's
  audio-on rate is 1.5x) and gives better control.
- **Animating every shot is mostly wasted money.** On a 50-second episode, two
  hero shots (the hook and the climax) carry the motion; the rest read fine as
  slow camera moves on a still. That's the default, and it's why an episode
  projects to about $3.19 instead of $9.35 (`choices estimate`).
- **4K output is wasted on TikTok and Reels.** Both re-encode to about 1080p.

## Images: Nano Banana 2 with reference sheets

| Model (fal) | $/image | Why it's here |
|---|---|---|
| **`fal-ai/nano-banana-2/edit`** (default, shots with cast) | ~$0.08 at 1K | Takes up to 14 `image_urls`, so every shot of Lola Cora is conditioned on her reference sheet, not on re-reading a text description. |
| **`fal-ai/nano-banana-2`** (default, shots without cast) | ~$0.08 | Same family and look, no references needed. |
| `fal-ai/nano-banana-pro/edit` | ~$0.15 | Candidate for hero frames only, if the bake-off shows a real jump in identity retention. |
| Seedream 5.0 Lite (fal) | ~$0.035 | Cheapest multi-reference option (up to 10). First thing to bake off for cost. |
| `fal-ai/flux/dev` | – | Legacy, kept for compatibility. No reference images, so characters drift. |

Opinion: **consistency comes from data, not prompts.** A locked text
description plus a reference image beats any amount of prompt wording. The
frame judge then checks identity against the same reference before a frame
is allowed to become an expensive clip.

## Voice: ElevenLabs Multilingual v2 with timestamps

`eleven_multilingual_v2` at $0.10 per 1K characters, via `/with-timestamps`
for exact caption timing. It's about $0.07 of narration per episode, so voice
isn't where the money goes. Worth A/B-testing: `eleven_v3` (same price, more
expressive) and `eleven_flash_v2_5` (half price). A reported Eleven v4 at
$0.08 per 1K characters is from a single source and unverified.

## Language: Claude Opus 5.5 on every route, effort tuned per route

| Route | Effort | Why |
|---|---|---|
| Writer | high | The creative core: continuity, dilemma design, pacing. |
| Critic | medium | A narrower job, with the rule checks already done in code. |
| Frame judge, vote classifier, hook writer | low | Short, well-specified jobs. |

All calls use structured outputs, adaptive thinking, a cached system prompt,
and server-side refusal fallback. A cheaper model for the low-effort routes
(for example Claude Haiku 5.5) is the obvious next cost lever, but only after
measuring it on real comments and frames. The judge and the classifier are
where a quiet accuracy drop would hurt most.

## How to switch a model

1. Add its price to `pricing:` in `config/pipeline.yaml`.
2. Run `choices bakeoff --day N --models current,candidate` on a few real episodes.
3. Compare **$ per usable frame**, not $ per image. It prices in re-rolls.
4. Switch `models.*` in the config. No code changes.

## Sources

- [LLM Stats video leaderboard](https://llm-stats.com/leaderboards/best-ai-for-video-creation)
- [fal: best image-to-video APIs 2026](https://fal.ai/learn/tools/best-image-to-video-apis-2026)
- [fal: Kling v3 Pro API schema](https://fal.ai/models/fal-ai/kling-video/v3/pro/image-to-video/api)
- [fal: Nano Banana 2 edit API schema](https://fal.ai/models/fal-ai/nano-banana-2/edit/api)
- [fal: character consistency guide](https://fal.ai/learn/tools/ai-character-consistency)
- [fal: Nano Banana API pricing](https://fal.ai/learn/tools/nano-banana-api)
- [Kling pricing tracker](https://costgoat.com/pricing/kling)
- [ElevenLabs API pricing breakdown](https://developer.puter.com/tutorials/elevenlabs-api-pricing/)
- [Instagram media insights reference](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights)
- [python-tiktok (TikTok Business API fields)](https://github.com/sns-sdks/python-tiktok/)
