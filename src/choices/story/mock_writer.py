"""Deterministic offline writer.

Lets the whole pipeline (render, voting, state machine) run with no API key,
in CI and in demos. Scripts are hand-written for the first two days of the
TULOG bible (both branches of the pilot's vote), then fall back to a generic
template that still threads the audience's choice through.
"""

from __future__ import annotations

from ..models import CanonUpdate, ChoiceOption, Companion, EpisodeScript, LLMCall, Scene
from .writer import WriteRequest

TAGS = ["tulog", "zombieapocalypse", "choosewisely", "manila", "interactivestory"]


def _pilot() -> EpisodeScript:
    return EpisodeScript(
        title="The Still City",
        hook="Nobody on your street is moving.",
        scenes=[
            Scene(
                narration="Six in the morning. Sampaloc should be loud by now. Instead, the whole alley is standing perfectly still.",
                visual="POV from a second-floor window looking down a narrow wet Manila alley at dawn, a dozen people standing motionless mid-step, a tricycle stopped in the middle, tangled power lines overhead",
                motion="slow push-in toward the frozen street",
                characters=[],
            ),
            Scene(
                narration='Lola Cora grips your wrist. Her voice is barely air. "Apo. Huwag kang maingay." Nobody out there has blinked in an hour.',
                visual="close-up of Lola Cora in a dim kitchen, one finger pressed to her lips, grey dawn light through old jalousie windows",
                motion="subtle handheld drift",
                characters=["Lola Cora"],
            ),
            Scene(
                narration="Then the neighbour's rooster crows. Every head in the alley snaps toward the sound. And they run.",
                visual="wide shot of the rainy alley, motion-blurred silhouettes sprinting toward a rooster on a rusted corrugated tin roof",
                motion="fast whip pan",
                characters=[],
                hero=True,
            ),
            Scene(
                narration="A knock. Soft. Three times. Through the gap in the door you see a boy in a yellow rider jacket, soaked and shaking, a finger to his lips.",
                visual="POV through the narrow gap of a wooden front door, Migs pressed against the doorframe outside in heavy rain, finger to his lips",
                motion="slow push toward the gap",
                characters=["Migs"],
            ),
            Scene(
                narration="Behind him, at the end of the alley, one of them turns toward your door. It hasn't heard anything yet. The old bolt always clicks.",
                visual="over-the-shoulder POV of your hand on an old iron door bolt, beyond the gap a dark silhouette standing still at the far end of the rainy alley",
                motion="slow rack focus from the bolt to the silhouette",
                characters=[],
            ),
        ],
        question="Do you open the door for the boy, or keep it locked?",
        vote_style="ab",
        choice_a=ChoiceOption(
            label="OPEN THE DOOR",
            keywords=["open", "unlock", "buksan", "let him in", "save him"],
            stakes="The bolt clicks and the Tulog at the end of the alley may charge. Gain: Migs, who knows the streets and has a motorbike.",
        ),
        choice_b=ChoiceOption(
            label="KEEP IT LOCKED",
            keywords=["locked", "lock", "keep", "stay quiet", "wag"],
            stakes="Silence and safety for now, but Migs is left outside and Lola Cora will not forgive it easily.",
        ),
        canon=CanonUpdate(
            location="Lola Cora's house in Sampaloc, at the front door",
            inventory=[
                "your phone (31% battery, on silent)",
                "Lola Cora's first-aid kit",
                "a bolo knife",
                "two litres of water",
                "a pack of crackers",
            ],
            companions=[
                Companion(name="Lola Cora", status="with_you", note="calm on the outside, praying under her breath"),
                Companion(name="Migs", status="separated", note="outside your door in the rain, waiting"),
            ],
            new_facts=[
                "A rooster's crow woke every Tulog in the alley.",
                "A Tulog stands at the end of the alley, facing your door.",
            ],
            open_threads=["Who is the boy in the yellow jacket?", "Where did the neighbours run to?"],
        ),
        caption="Day 1 of TULOG. Would you open the door? Comment A or B 👇",
        hashtags=TAGS,
    )


def _day2_opened() -> EpisodeScript:
    return EpisodeScript(
        title="The Click",
        hook="The lock was louder than you thought.",
        scenes=[
            Scene(
                narration="The bolt clicks. At the end of the alley, the Tulog's head jerks toward your door.",
                visual="POV of a hand sliding an old iron bolt open, a dark figure at the far end of a rainy alley turning its head",
                motion="slow push-in",
                characters=[],
            ),
            Scene(
                narration="You yank the boy inside. Lola Cora clamps a hand over his mouth before he can gasp. Outside, bare feet slap the wet concrete. Closer. Then they stop.",
                visual="dark narrow hallway, Lola Cora's hand over Migs's mouth, both pressed against a wall, grey rain light spilling from the doorway",
                motion="handheld tremble",
                characters=["Migs", "Lola Cora"],
                hero=True,
            ),
            Scene(
                narration="Something stands on the other side of the door. The rain covers your heartbeat. After a long minute, it goes still.",
                visual="low angle on the bottom of a wooden door, the shadow of two bare feet in the strip of light underneath, rainwater running",
                motion="very slow push-in",
                characters=[],
            ),
            Scene(
                narration='The boy breathes out. "Migs po. Rider. Alam ko ang daan palabas ng Sampaloc." His bike is one street away, parked beside a frozen crowd.',
                visual="Migs crouched on a dusty floor in candlelight, tracing a map of streets with one finger",
                motion="slow orbit",
                characters=["Migs"],
            ),
            Scene(
                narration="Lola Cora holds up an empty pill bottle. Her heart medicine is at the health centre, the other way. You can reach one before the rain stops. Not both.",
                visual="Lola Cora by a rain-streaked window holding up an empty orange pill bottle, worried expression, cool blue light",
                motion="slow push-in on the bottle",
                characters=["Lola Cora"],
            ),
        ],
        question="Do you go for Migs's motorbike, or Lola Cora's medicine?",
        vote_style="ab",
        choice_a=ChoiceOption(
            label="GET THE BIKE",
            keywords=["bike", "motor", "motorbike", "ride", "migs"],
            stakes="A way out of Sampaloc, but the bike sits next to a frozen crowd and engines are loud to start.",
        ),
        choice_b=ChoiceOption(
            label="GET THE MEDICINE",
            keywords=["medicine", "gamot", "meds", "health centre", "pills"],
            stakes="Keeps Lola Cora alive for the long road, but costs the rain cover and maybe Migs's patience.",
        ),
        canon=CanonUpdate(
            location="Lola Cora's house in Sampaloc, ground floor",
            inventory=["your phone (27% battery, on silent)", "Lola Cora's first-aid kit", "a bolo knife", "one litre of water", "a pack of crackers"],
            companions=[
                Companion(name="Lola Cora", status="with_you", note="out of heart medicine"),
                Companion(name="Migs", status="with_you", note="grateful, restless, wants his bike"),
            ],
            new_facts=["Migs knows a route out of Sampaloc.", "Lola Cora's heart medicine is at the barangay health centre."],
            open_threads=["Where does Migs's route lead?", "How long does the rain last?"],
        ),
        caption="Day 2. Bike or medicine? You only get one. Comment A or B 👇",
        hashtags=TAGS,
    )


def _day2_locked() -> EpisodeScript:
    return EpisodeScript(
        title="The Boy Outside",
        hook="You left him out there.",
        scenes=[
            Scene(
                narration="You keep your hand off the bolt. Through the gap, the boy's eyes find yours. He understands. He doesn't knock again.",
                visual="POV through a narrow door gap, Migs outside in the rain looking straight back, slowly lowering his raised hand",
                motion="slow push-in",
                characters=["Migs"],
            ),
            Scene(
                narration="Lola Cora won't look at you. She lights a candle and whispers a prayer for him. Then, quietly, one for you.",
                visual="Lola Cora kneeling at a small home altar lit by candles, rain on the window behind her",
                motion="slow drift",
                characters=["Lola Cora"],
            ),
            Scene(
                narration="At noon, a phone rings somewhere down the alley. Every Tulog runs toward it. When the noise dies, the boy is gone.",
                visual="wide shot of an empty rainy alley at midday, a lone phone glowing face-up in a puddle",
                motion="slow tilt down to the phone",
                characters=[],
            ),
            Scene(
                narration="All that's left is his cracked white helmet on your step. Inside, on a strip of tape, a name. Migs. And: barangay hall, may tubig, may pagkain.",
                visual="close-up of a cracked white motorcycle helmet resting on a wet concrete doorstep",
                motion="slow push-in",
                characters=[],
            ),
            Scene(
                narration="The hall is six streets away and your water is almost gone. But Lola Cora's knees won't make six streets.",
                visual="POV looking down at a nearly empty plastic water bottle in your hand, Lola Cora blurred in the background rubbing her knee",
                motion="rack focus to Lola Cora",
                characters=["Lola Cora"],
            ),
        ],
        question="Do you go to the barangay hall alone, or stay with Lola?",
        vote_style="ab",
        choice_a=ChoiceOption(
            label="GO TO THE HALL",
            keywords=["hall", "barangay", "go", "alis", "alone"],
            stakes="Water, food and maybe Migs, but Lola Cora stays alone behind an unguarded door.",
        ),
        choice_b=ChoiceOption(
            label="STAY WITH LOLA",
            keywords=["stay", "lola", "dito", "wait", "home"],
            stakes="You protect Lola Cora, but the water runs out tomorrow and the hall may not wait.",
        ),
        canon=CanonUpdate(
            location="Lola Cora's house in Sampaloc",
            inventory=["your phone (28% battery, on silent)", "Lola Cora's first-aid kit", "a bolo knife", "half a litre of water", "a pack of crackers", "Migs's cracked helmet"],
            companions=[
                Companion(name="Lola Cora", status="with_you", note="hurt by your choice, knees failing"),
                Companion(name="Migs", status="missing", note="vanished after the phone rang; left his helmet"),
            ],
            new_facts=["Someone is gathering survivors at the barangay hall.", "Migs left his helmet on your doorstep."],
            open_threads=["Is Migs alive?", "Who is at the barangay hall?"],
        ),
        caption="Day 2. Go alone or stay with Lola? Comment A or B 👇",
        hashtags=TAGS,
    )


def _generic(req: WriteRequest) -> EpisodeScript:
    label = req.outcome.chosen_label.lower() if req.outcome else "move"
    rejected = req.outcome.rejected_label.lower() if req.outcome else "wait"
    return EpisodeScript(
        title=f"Day {req.day}",
        hook="Something heard you.",
        scenes=[
            Scene(
                narration=f"You chose to {label}. For one long breath, it looks like it worked.",
                visual="POV of your hands gripping a rusted gate in heavy rain, an empty flooded Manila street beyond",
                motion="slow push-in",
                characters=[],
            ),
            Scene(
                narration="Then a car alarm starts howling two streets over. Every shape in the rain turns its head at once.",
                visual="wide shot of a flooded intersection at dusk, dozens of dark silhouettes standing in ankle-deep water turning toward a blinking car",
                motion="slow pan right",
                characters=[],
            ),
            Scene(
                narration=f"You think about the other way, the one you didn't take. {rejected.capitalize()}. Too late for that now.",
                visual="POV looking back over your shoulder down a dark alley you just left, a single lamp flickering",
                motion="handheld drift",
                characters=[],
            ),
            Scene(
                narration="The alarm dies. In the silence you hear something closer. Breathing, on the other side of the wall you are leaning on.",
                visual="close-up of your hand pressed flat against a wet cinderblock wall, rain dripping, deep shadow",
                motion="very slow push-in",
                characters=[],
            ),
            Scene(
                narration="To your left, a jeepney with its door open. To your right, a ladder up to a rooftop water tank. You have about three seconds.",
                visual="POV at a narrow junction: an abandoned jeepney with an open rear door on the left, a rusted ladder up to a rooftop water tank on the right, rain",
                motion="quick whip between left and right",
                characters=[],
            ),
        ],
        question="Do you hide in the jeepney, or climb to the roof?",
        vote_style="ab",
        choice_a=ChoiceOption(label="HIDE IN THE JEEPNEY", keywords=["jeep", "jeepney", "hide", "tago", "inside"], stakes="Fast and close, but a dead end if anything follows."),
        choice_b=ChoiceOption(label="CLIMB TO THE ROOF", keywords=["roof", "climb", "ladder", "akyat", "tank"], stakes="A view and a way across the rooftops, but the ladder is loud and slick."),
        canon=CanonUpdate(
            location="a flooded junction in Sampaloc",
            inventory=["your phone (low battery, on silent)", "a bolo knife"],
            companions=[],
            new_facts=[f"On day {req.day} you chose to {label}."],
            open_threads=["What is breathing behind the wall?"],
        ),
        caption=f"Day {req.day}. Jeepney or roof? Comment A or B 👇",
        hashtags=TAGS,
    )


class MockWriter:
    """Offline stand-in for ClaudeWriter with the same interface.

    With ``simulate_model`` set, each draft reports the token usage a real
    writer call typically has, so offline cost dashboards aren't all zeros.
    """

    def __init__(self, simulate_model: str | None = None):
        self.simulate_model = simulate_model

    def _call(self, revision: bool) -> LLMCall | None:
        if not self.simulate_model:
            return None
        return LLMCall(purpose="writer-revision" if revision else "writer", model=self.simulate_model,
                       input_tokens=4200, output_tokens=6500, cache_read_tokens=3100,
                       served_by="simulated", simulated=True)

    def write(self, req: WriteRequest) -> tuple[EpisodeScript, LLMCall | None]:
        script, _ = self._script(req)
        return script, self._call(req.previous_draft is not None)

    def _script(self, req: WriteRequest) -> tuple[EpisodeScript, None]:
        if req.day == 1 or req.outcome is None:
            script = _pilot()
        elif req.day == 2 and req.outcome.chosen_label == "OPEN THE DOOR":
            script = _day2_opened()
        elif req.day == 2:
            script = _day2_locked()
        else:
            script = _generic(req)
            if req.state is not None:
                # Carry canon forward so long offline demos stay consistent.
                script.canon.companions = list(req.state.companions)
                script.canon.inventory = list(req.state.inventory) or script.canon.inventory
        return script, None
