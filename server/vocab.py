"""Custom vocabulary: gathered per meeting, used for Parakeet phrase boosting and a
conservative post-ASR spelling fix for terms the decoder still missed."""
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import store

GLOBAL_VOCAB = store.DATA / "vocabulary.txt"


@lru_cache(maxsize=1)
def _dictionary() -> frozenset:
    p = Path("/usr/share/dict/words")
    # lowercase entries only: the macOS list also contains proper names (Aoife, Nguyen...)
    return frozenset(w for w in p.read_text().split() if w.islower()) if p.exists() else frozenset()


def is_common_word(w: str) -> bool:
    return w.lower() in _dictionary()


def read_global() -> list[str]:
    if not GLOBAL_VOCAB.exists():
        return []
    return split_terms(GLOBAL_VOCAB.read_text())


def write_global(text: str):
    GLOBAL_VOCAB.write_text("\n".join(split_terms(text)) + "\n")


def split_terms(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n;]", text or "") if t.strip()]


def auto_terms(text: str) -> list[str]:
    """Pull likely proper nouns / jargon out of free text (title, notes): capitalized runs that
    aren't sentence-initial dictionary words, CamelCase, and unknown words with digits."""
    out = []
    for line in (text or "").splitlines():
        toks = re.findall(r"[A-Za-z][\w'.\-]*[\w]|[A-Za-z]", line)
        run = []
        for i, t in enumerate(toks):
            cap = t[0].isupper()
            special = bool(re.search(r"[a-z][A-Z]|\d", t))
            unknown = not is_common_word(t.strip(".'-"))
            if (cap and (unknown or i > 0)) or special:
                run.append(t)
            else:
                if run:
                    out.append(" ".join(run))
                run = []
        if run:
            out.append(" ".join(run))
    # keep runs that contain at least one non-dictionary word (drops "The", "Monday" etc.)
    calendar = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december", "i", "ok", "okay"}
    out = [r for r in out if r.lower() not in calendar]
    keep = []
    for r in out:
        words = r.split()
        if len(words) > 4:
            continue
        if any(not is_common_word(w.strip(".'-")) or re.search(r"[a-z][A-Z]|\d", w) for w in words):
            keep.append(r.strip(".-'"))
    return keep


def meeting_terms(m: dict) -> list[str]:
    terms = split_terms(m.get("vocabulary", "")) + read_global()
    terms += [n for n in (m.get("speakers") or {}).values() if n and n.lower() != "me"]
    terms += auto_terms(m.get("title", "")) + auto_terms(m.get("notes", ""))
    seen, out = set(), []
    for t in terms:
        k = t.lower()
        if len(k) >= 2 and k not in seen:
            seen.add(k)
            out.append(t)
    return out[:300]


# ------------------------------------------------------------------ spelling fix

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _sound(s: str) -> str:
    """Very rough phonetic key so 'Cuba Flow' ~ 'Kubeflow'."""
    s = _norm(s)
    for a, b in (("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("x", "ks"), ("z", "s"),
                 ("y", "i"), ("w", "v"), ("dg", "j"), ("gh", "g")):
        s = s.replace(a, b)
    s = re.sub(r"(.)\1+", r"\1", s)
    return s[0] + re.sub(r"[aeiou]", "", s[1:]) if s else s


def _similar(a: str, b: str) -> float:
    return max(SequenceMatcher(None, _norm(a), _norm(b)).ratio(),
               SequenceMatcher(None, _sound(a), _sound(b)).ratio() * 0.97)


def fix_words(words: list[dict], terms: list[str]) -> list[dict]:
    """Replace 1-3 word windows that closely match a vocabulary term. Conservative: the window
    must not already be ordinary words that differ from the term, and similarity must be high."""
    if not terms or not words:
        return words
    targets = [(t, len(t.split())) for t in terms if len(_norm(t)) >= 4]
    out, i = [], 0
    while i < len(words):
        best = None
        for t, tn in targets:
            for n in sorted({tn, tn + 1, max(1, tn - 1)}):
                if i + n > len(words):
                    continue
                win = words[i:i + n]
                raw = " ".join(re.sub(r"[^\w'\-]", "", w["word"]) for w in win)
                # a wider window must not swallow neighbours of a word that already matches
                # ("The Zephyrine" must not become "Zephyrine")
                if n > tn and any(_similar(w["word"], t) >= 0.9 or _similar(" ".join(x["word"] for x in win[k:k + tn]), t) >= 0.9
                                  for k, w in enumerate(win[:n - tn + 1])):
                    continue
                if _norm(raw) == _norm(t):
                    if raw != t:
                        best = max(best or (0,), (1.0, t, n, win))
                    continue
                if _norm(t) in _norm(raw) and n == 1 and len(_norm(raw)) - len(_norm(t)) <= 1:
                    continue
                # truncated multi-word term: earlier words exact, last word a clear prefix
                # ("Siobhan N" -> "Siobhan Nguyen")
                tw = t.split()
                if n == tn > 1:
                    rw = [_norm(x) for x in raw.split()]
                    if (len(rw) == tn and rw[:-1] == [_norm(x) for x in tw[:-1]] and rw[-1]
                            and _norm(tw[-1]).startswith(rw[-1]) and rw[-1] != _norm(tw[-1])):
                        best = max(best or (0,), (0.99, t, n, win))
                        continue
                lt = len(_norm(t))
                need = 0.86 if lt <= 6 else 0.8
                # a single ordinary dictionary word is never rewritten into a different term
                if n == 1 and is_common_word(raw) and not is_common_word(t):
                    need = 0.95
                if abs(len(_norm(raw)) - lt) > max(2, lt // 3):
                    continue
                sc = _similar(raw, t)
                if sc >= need and (best is None or sc > best[0]):
                    best = (sc, t, n, win)
        if best:
            _, t, n, win = best
            trail = re.search(r"[^\w'\-]+$", win[-1]["word"])
            out.append({**win[0], "word": t + (trail.group(0) if trail else ""), "end": win[-1]["end"]})
            i += n
        else:
            out.append(words[i])
            i += 1
    return out
