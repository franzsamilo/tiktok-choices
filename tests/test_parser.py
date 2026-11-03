import pytest

from choices.models import ChoiceOption
from choices.votes.parser import Vocabulary, parse_vote


@pytest.fixture
def vocab(pilot):
    return Vocabulary.from_script(pilot)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("A", "A"), ("B!!!", "B"), ("a", "A"), ("b po", "B"), ("a talaga", "A"), ("AAAA", "A"), ("bbbb", "B"),
        ("option b", "B"), ("Option A please", "A"), ("#teamA", "A"), ("team b all the way", "B"),
        ("🅰️", "A"), ("🅱️ obviously", "B"), ("1", "A"), ("2 po", "B"), ("vote 1", "A"),
        ("A because he needs help", "A"), ("B, mas safe", "B"), ("I think A is right", "A"),
        ("@friend look at this A", "A"),
        # keywords from the script (incl. Tagalog) and the full label phrase
        ("open it!!", "A"), ("buksan mo na", "A"), ("let him in please", "A"), ("save him!!", "A"),
        ("keep it locked", "B"), ("KEEP IT LOCKED", "B"), ("lock lock lock", "B"), ("open the door!!", "A"),
    ],
)
def test_clear_votes(vocab, text, expected):
    v, needs_llm = parse_vote(text, vocab)
    assert v.choice == expected, (text, v)
    assert not needs_llm


@pytest.mark.parametrize(
    "text,reason",
    [
        ("A or B?", "conflict"),
        ("A... wait no, B", "conflict"),
        ("don't open it", "negated"),
        ("wag mo buksan", "negated"),
        ("anything but opening that door", "negated"),
        ("A zombie would hear the bolt", "unclear"),  # article "A", not a vote
        ("this is so good who made this", "unclear"),
    ],
)
def test_ambiguous_goes_to_llm(vocab, text, reason):
    v, needs_llm = parse_vote(text, vocab)
    assert v.choice is None
    assert v.reason == reason
    assert needs_llm


@pytest.mark.parametrize("text", ["first!", "lol", "🔥🔥🔥", "pick a side lol", "", "   "])
def test_noise_is_not_a_vote_and_not_worth_an_llm_call(vocab, text):
    v, needs_llm = parse_vote(text, vocab)
    assert v.choice is None
    assert not needs_llm


def test_yes_no_mode(pilot):
    s = pilot.model_copy(deep=True)
    s.vote_style = "yes_no"
    s.choice_a = ChoiceOption(label="YES, OPEN IT", keywords=["open"], stakes="")
    s.choice_b = ChoiceOption(label="NO, STAY QUIET", keywords=["quiet"], stakes="")
    vocab = Vocabulary.from_script(s)
    for text, want in [("yes", "A"), ("oo naman", "A"), ("TRUE", "A"), ("no", "B"), ("hindi po", "B"), ("false", "B"), ("nope", "B")]:
        v, _ = parse_vote(text, vocab)
        assert v.choice == want, text
    v, needs = parse_vote("yes... actually no", vocab)
    assert v.choice is None and needs


def test_shared_keywords_are_ignored(pilot):
    s = pilot.model_copy(deep=True)
    s.choice_a.keywords = ["door", "open"]
    s.choice_b.keywords = ["door", "lock"]
    vocab = Vocabulary.from_script(s)
    v, needs = parse_vote("the door!", vocab)
    assert v.choice is None
