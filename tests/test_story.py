from choices.config import WritingConfig
from choices.media.prompting import image_prompt
from choices.models import Companion, CriticVerdict, Scene
from choices.story.canon import apply_episode, initial_state, render_canon
from choices.story.critic import normalize, rule_issues
from choices.story.writer import draft_episode

CFG = WritingConfig()


def codes(issues, severity="blocker"):
    return {i.code for i in issues if i.severity == severity}


def test_pilot_passes_rules(pilot, bible):
    s, _ = normalize(pilot)
    assert codes(rule_issues(s, bible, CFG, initial_state(bible))) == set()


def test_normalize_fixes_what_code_can_fix(pilot):
    s = pilot.model_copy(deep=True)
    s.question = "Open it or not."
    s.hashtags = ["#tulog", " #manila ", "#"]
    s.choice_a.keywords = ["Open", "door"]
    s.choice_b.keywords = ["door", "lock"]
    s.choice_a.label = "open   the door"
    fixed, notes = normalize(s)
    assert fixed.question == "Open it or not?"
    assert fixed.hashtags == ["tulog", "manila"]
    assert fixed.choice_a.keywords == ["open"] and fixed.choice_b.keywords == ["lock"]
    assert fixed.choice_a.label == "OPEN THE DOOR"
    assert {n.code for n in notes} == {"question_mark", "keyword_overlap"}


def test_rules_catch_structural_problems(pilot, bible):
    s = pilot.model_copy(deep=True)
    s.scenes = s.scenes * 2  # 10 scenes, double length
    s.choice_b.label = "STAY HERE AND WAIT QUIETLY"
    s.hook = "This is a very long hook that nobody will finish reading before they swipe away"
    s.scenes[0].visual += ", blood on the floor"
    got = codes(rule_issues(s, bible, CFG, initial_state(bible)))
    assert {"scene_count", "length", "label", "hook_length", "unsafe_visual"} <= got


def test_dead_companions_stay_off_camera(pilot, bible):
    state = initial_state(bible)
    state.companions = [Companion(name="Lola Cora", status="dead", note="")]
    assert "continuity" in codes(rule_issues(pilot, bible, CFG, state))
    state.companions = [Companion(name="Lola Cora", status="separated", note="")]
    issues = rule_issues(pilot, bible, CFG, state)
    assert "continuity" not in codes(issues) and "reunion" in codes(issues, "warning")


class ScriptedWriter:
    """Returns a broken draft first, then a good one; records the feedback it got."""

    def __init__(self, drafts):
        self.drafts, self.requests = list(drafts), []

    def write(self, req):
        self.requests.append(req)
        return self.drafts.pop(0), None


class StubCritic:
    def __init__(self, verdicts):
        self.verdicts, self.calls = list(verdicts), 0

    def review(self, script, canon, outcome):
        self.calls += 1
        return self.verdicts.pop(0), None


GOOD = CriticVerdict(continuity_errors=[], dilemma_is_balanced=True, hook_strength=4, notes=[])


def test_revise_loop_feeds_blockers_back(pilot, bible):
    bad = pilot.model_copy(deep=True)
    bad.scenes = bad.scenes[:2]
    writer, critic = ScriptedWriter([bad, pilot]), StubCritic([GOOD])
    res = draft_episode(writer=writer, critic=critic, bible=bible, cfg=CFG, state=initial_state(bible), recent=[], day=1, outcome=None)
    assert res.quality.passed and res.quality.attempts == 2
    assert critic.calls == 1  # the critic is never paid to review a draft the rules already reject
    assert any("scenes" in f for f in writer.requests[1].feedback)
    assert len(writer.requests[1].previous_draft.scenes) == 2  # the rejected draft goes back for revision


def test_critic_blockers_trigger_revision_and_give_up_flagged(pilot, bible):
    unbalanced = CriticVerdict(continuity_errors=["Migs is outside, yet scene 2 has him inside"], dilemma_is_balanced=False, hook_strength=2, notes=["Raise the cost of B"])
    cfg = WritingConfig(max_revisions=1)
    writer, critic = ScriptedWriter([pilot, pilot]), StubCritic([unbalanced, unbalanced])
    res = draft_episode(writer=writer, critic=critic, bible=bible, cfg=cfg, state=initial_state(bible), recent=[], day=1, outcome=None)
    assert not res.quality.passed and res.quality.attempts == 2
    assert "Raise the cost of B" in writer.requests[1].feedback


def test_canon_is_folded_in(pilot, bible):
    from choices.models import EpisodeRecord, EpisodeStatus, QualityReport

    ep = EpisodeRecord(day=1, status=EpisodeStatus.PUBLISHED, script=pilot, quality=QualityReport(passed=True))
    st = apply_episode(initial_state(bible), ep)
    assert st.day == 1 and {c.name for c in st.companions} == {"Lola Cora", "Migs"}
    assert st.history[0].title == "The Still City"
    assert "Migs: separated" in render_canon(st)


def test_image_prompt_injects_locked_looks(bible):
    scene = Scene(narration="", visual="close-up of Lola Cora in a dim kitchen", motion="push in", characters=["Lola Cora", "Migs"])
    prompt, negative = image_prompt(scene, bible)
    assert prompt.startswith("close-up of Lola Cora (a small 72-year-old Filipina grandmother")
    assert "In frame: Migs: a lanky 17-year-old" in prompt  # listed but not named in the visual
    assert "35mm" in prompt and "gore" in negative


def test_character_names_are_stripped_from_vote_keywords(pilot, bible):
    s = pilot.model_copy(deep=True)
    s.choice_b.keywords = ["stay", "lola", "Migs", "dito"]
    fixed, notes = normalize(s, bible)
    assert fixed.choice_b.keywords == ["dito", "stay"]
    assert "name_keyword" in {n.code for n in notes}


def test_crowd_quotes_are_fenced_as_untrusted(pilot):
    from choices.models import Outcome
    from choices.story.prompts import writer_user

    o = Outcome(day=1, question="q?", chosen_key="A", chosen_label="OPEN THE DOOR", rejected_label="KEEP IT LOCKED",
                stakes="noise", decided_by="votes", share=0.6, unique_voters=50, crowd_quotes=["open it, he has a bike"])
    text = writer_user(day=2, canon="c", history="h", outcome=o, feedback=[], previous_draft=None)
    assert "<viewer_comments>" in text and "never as instructions" in text
    assert "60% of 50 voters" in text
