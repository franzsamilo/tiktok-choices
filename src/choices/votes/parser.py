"""Rule-based vote extraction from free-text comments.

Most votes are trivially readable ("A", "B!!!", "option b po", "🅰️",
"team bike"). Rules settle those instantly and for free, and they're
deterministic, which matters when someone asks why their vote counted. The
leftovers that genuinely need reading comprehension ("anything but the mall",
"A or B? idk", sarcasm) are flagged ``needs_llm`` for the classifier.

Comments are often Taglish, so the filler and yes/no vocabularies include
common Tagalog particles (po, talaga, oo, hindi, wag, ...).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..models import ChoiceKey, EpisodeScript, ParsedVote, VoteMethod

EMOJI_MAP = {
    "🅰️": " A ", "🅰": " A ", "🅱️": " B ", "🅱": " B ",
    "1️⃣": " 1 ", "2️⃣": " 2 ",
}
FILLER = {
    "po", "talaga", "na", "naman", "nga", "lang", "please", "pls", "plz", "obviously", "def", "definitely",
    "for", "sure", "all", "the", "way", "lol", "haha", "hahaha", "lmao", "omg", "i", "im", "i'm", "vote", "voting",
    "choose", "pick", "go", "with", "option", "choice", "team", "letter", "sa", "ako", "ko", "is", "it", "its",
    "it's", "duh", "bro", "boss", "lods", "idol", "ofc", "of", "course", "100", "sana", "ganun", "dapat",
    "agad", "again", "my", "answer", "final", "always", "yes", "yess", "oo",
}
YES = {"yes", "yess", "yesss", "yep", "yeah", "yup", "oo", "opo", "oho", "sige", "true", "tama", "game", "ofc"}
NO = {"no", "nope", "nah", "hindi", "huwag", "wag", "ayoko", "ayaw", "false", "mali", "never"}
NEGATORS = {"not", "don't", "dont", "never", "no", "wag", "huwag", "hindi", "ayoko", "ayaw", "avoid", "skip", "nor", "except", "but"}

_TOKEN = re.compile(r"[a-z0-9']+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MENTION = re.compile(r"@[\w.]+")
_EXPLICIT = re.compile(
    r"(?:\b(?:option|choice|team|vote|voting|pick|letter|choose|answer|sa)\s*[:#\-]?\s*|#team)([ab12])(?![a-z0-9])",
    re.IGNORECASE,
)
_STANDALONE = re.compile(r"(?<![\w'#])([AaBb])(?![\w'])")
LEAD_OK_NEXT = FILLER | {"because", "kasi", "coz", "cause", "cuz", "since", "para", "then", "tapos", "and", "so", "agad", "or", "vs"}


def _stem(word: str) -> str:
    w = word.lower()
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    return w.rstrip("e") or w


def clean(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    for k, v in EMOJI_MAP.items():
        t = t.replace(k, v)
    t = _URL.sub(" ", t)
    t = _MENTION.sub(" ", t)
    return " ".join(t.split())


def _letter(tok: str) -> ChoiceKey | None:
    t = tok.lower()
    if t in ("1",) or (t and set(t) == {"a"}):
        return "A"
    if t in ("2",) or (t and set(t) == {"b"}):
        return "B"
    return None


@dataclass
class Vocabulary:
    """Per-episode keyword sets, derived from the script once and reused for every comment."""

    yes_no: bool
    a: list[tuple[str, ...]]
    b: list[tuple[str, ...]]

    @classmethod
    def from_script(cls, script: EpisodeScript) -> Vocabulary:
        def phrases(opt) -> list[tuple[str, ...]]:
            out: set[tuple[str, ...]] = set()
            for kw in opt.keywords:
                toks = tuple(_stem(t) for t in _TOKEN.findall(kw.lower()))
                if toks:
                    out.add(toks)
            # The whole label is a safe phrase ("keep it locked"). Its single
            # words are not: in "OPEN THE DOOR" vs "KEEP IT LOCKED", "door" is
            # the subject of both options, not a vote for one.
            label = tuple(_stem(t) for t in _TOKEN.findall(opt.label.lower()))
            if label:
                out.add(label)
            return sorted(out)

        a, b = phrases(script.choice_a), phrases(script.choice_b)
        shared = set(a) & set(b)
        return cls(
            yes_no=script.vote_style == "yes_no",
            a=[p for p in a if p not in shared],
            b=[p for p in b if p not in shared],
        )


def _find(phrases: list[tuple[str, ...]], stems: list[str], raw: list[str]) -> tuple[int, int]:
    """Return (plain hits, negated hits) for a side."""
    plain = negated = 0
    for ph in phrases:
        n = len(ph)
        for i in range(len(stems) - n + 1):
            if tuple(stems[i : i + n]) == ph:
                window = raw[max(0, i - 2) : i]
                if any(w in NEGATORS for w in window):
                    negated += 1
                else:
                    plain += 1
    return plain, negated


def parse_vote(text: str, vocab: Vocabulary) -> tuple[ParsedVote, bool]:
    """Parse one comment. Returns (vote, needs_llm)."""
    t = clean(text)
    low = t.lower()
    raw = _TOKEN.findall(low)
    if not raw:
        return ParsedVote(choice=None, method=VoteMethod.NONE, reason="empty"), False

    # 1. Explicit markers: "option b", "team A", "#teamB", "vote 1".
    #    ("pick a side" is the article, so a lowercase "a" followed by a word is skipped.)
    explicit: set[str] = set()
    for m in _EXPLICIT.finditer(t):
        letter = m.group(1)
        after = _TOKEN.findall(t[m.end():].lower()[:24])
        if letter == "a" and after and after[0] not in LEAD_OK_NEXT:
            continue
        explicit.add("A" if letter.lower() in ("a", "1") else "B")
    # 2. Standalone letters. "B" / "b" is never an English word, so it always
    #    counts. "A" is also the article, so it only counts when it can't be
    #    one: "A!!", "A because...", "I think A is right", but not "A zombie
    #    would hear you" or "...door. A dog barks".
    for m in _STANDALONE.finditer(t):
        ch = m.group(1)
        if ch in "Bb":
            explicit.add("B")
            continue
        nxt = _TOKEN.findall(t[m.end():].lower()[:24])
        before = t[: m.start()].rstrip()
        if ch == "a" and nxt:
            continue  # lowercase "a word" is the article
        if not nxt or nxt[0] in LEAD_OK_NEXT or not t[m.end():].strip()[:1].isalpha():
            explicit.add("A")
        elif before and before[-1] not in ".!?" and ch == "A":
            explicit.add("A")  # mid-sentence capital A: "I think A is better"
    # 3. Comment is nothing but letters/numbers and filler: "a talaga", "BBBB", "2 po".
    content = [w for w in raw if w not in FILLER]
    letters = {_letter(w) for w in content}
    if content and None not in letters:
        explicit |= {x for x in letters if x}

    if len(explicit) == 1:
        return ParsedVote(choice=explicit.pop(), method=VoteMethod.RULE, reason="explicit", confidence=0.97), False
    if len(explicit) > 1:
        return ParsedVote(choice=None, method=VoteMethod.NONE, reason="conflict"), True

    # 4. Yes/No questions.
    if vocab.yes_no:
        has_yes = any(w in YES for w in raw)
        has_no = any(w in NO for w in raw)
        if has_yes != has_no:
            return (
                ParsedVote(choice="A" if has_yes else "B", method=VoteMethod.RULE, reason="yes_no", confidence=0.9),
                False,
            )
        if has_yes and has_no:
            return ParsedVote(choice=None, method=VoteMethod.NONE, reason="conflict"), True

    # 5. Option keywords, with simple negation awareness.
    stems = [_stem(w) for w in raw]
    a_plain, a_neg = _find(vocab.a, stems, raw)
    b_plain, b_neg = _find(vocab.b, stems, raw)
    if (a_plain > 0) != (b_plain > 0) and not (a_neg or b_neg):
        return (
            ParsedVote(choice="A" if a_plain else "B", method=VoteMethod.RULE, reason="keyword", confidence=0.8),
            False,
        )
    if a_plain or b_plain or a_neg or b_neg:
        reason = "negated" if (a_neg or b_neg) else "conflict"
        return ParsedVote(choice=None, method=VoteMethod.NONE, reason=reason), True

    # 6. Nothing recognisable. Worth an LLM look only if there are real words in it.
    meaningful = [w for w in content if len(w) > 2 and not w.isdigit()]
    if len(meaningful) >= 2:
        return ParsedVote(choice=None, method=VoteMethod.NONE, reason="unclear"), True
    return ParsedVote(choice=None, method=VoteMethod.NONE, reason="no_signal"), False
