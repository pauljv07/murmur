"""Speech pipeline: Parakeet ASR + Streaming Sortformer diarization, one or two channels.

Accuracy measures:
- Two channels when computer audio is captured: the mic is "me" (no guessing), the computer
  audio is diarized with Sortformer to tell remote speakers apart.
- Echo removal: remote speech that leaks from the speakers into the mic is detected (same words
  at the same time on the computer-audio channel) and dropped.
- Custom vocabulary: Parakeet phrase boosting (GPU-PB boosting tree) + a conservative spelling fix.
- ASR gets a few seconds of preceding audio as context, so words at cut points aren't lost.
- Word-level speaker smoothing; Refine re-runs everything over the full recording with the
  Sortformer high-accuracy (long chunk) configuration.
"""
import copy
import logging
import os
import re
import threading
import time

import numpy as np
import soundfile as sf
import torch

import models
import vocab
from diarize import FRAME_SEC, SR, StreamingDiarizer, configure, load_model as load_diar, LIVE_CFG

DIAR_DEVICE = os.environ.get("MURMUR_DIAR_DEVICE", "mps" if torch.backends.mps.is_available() else "cpu")
ASR_DEVICE = os.environ.get("MURMUR_ASR_DEVICE", "cpu")
BOOST_ALPHA = float(os.environ.get("MURMUR_BOOST_ALPHA", "1.0"))
CTX_SEC = 3.0            # left audio context given to ASR for each piece
ME = "me"

# Sortformer "very high latency" setting from the model card: best accuracy, used offline.
REFINE_CFG = dict(chunk_len=340, chunk_left_context=1, chunk_right_context=40,
                  fifo_len=40, spkcache_refresh_rate=300, spkcache_len=188)

log = logging.getLogger("murmur")

status = {"Speech recognition": "not loaded", "Speaker diarization": "not loaded", "Language model": "not loaded"}
_asr = None
_diar = None
_base_decoding = None
_boost_terms: tuple = ()
_load_lock = threading.Lock()
gpu_lock = threading.Lock()   # one recording / reprocess at a time


class ModelMissing(RuntimeError):
    pass


def _available(kind: str) -> bool:
    if os.environ.get({"asr": "MURMUR_ASR_MODEL", "diar": "MURMUR_DIAR_MODEL"}[kind]):
        return True
    key = models.settings().get("asr") if kind == "asr" else "diar"
    return key in models.CATALOG and models.installed(key)


def load_models():
    """Load the selected speech models (from the local cache only)."""
    global _asr, _diar, _base_decoding, _boost_terms
    with _load_lock:
        if _asr is None:
            if not _available("asr"):
                status["Speech recognition"] = "not installed"
                raise ModelMissing("No speech recognition model installed — open Models to download one.")
            status["Speech recognition"] = "loading"
            from nemo.collections.asr.models import ASRModel
            asr = ASRModel.from_pretrained(models.repo_for("asr"), map_location=ASR_DEVICE)
            asr.eval()
            _base_decoding = copy.deepcopy(asr.cfg.decoding)
            _boost_terms = ()
            _asr = asr
            status["Speech recognition"] = "ready"
        if _diar is None:
            if not _available("diar"):
                status["Speaker diarization"] = "not installed"
                raise ModelMissing("Speaker detection model not installed — open Models to download it.")
            status["Speaker diarization"] = "loading"
            try:
                _diar = load_diar(DIAR_DEVICE, models.repo_for("diar"))
            except Exception:
                log.exception("diarizer on %s failed, using cpu", DIAR_DEVICE)
                _diar = load_diar("cpu", models.repo_for("diar"))
            status["Speaker diarization"] = "ready"
    return _asr, _diar


def unload_asr():
    """Drop the speech model so the next load_models() picks up a newly selected one.
    Callers hold gpu_lock (no recording in progress)."""
    global _asr
    import gc
    with _load_lock:
        _asr = None
    gc.collect()
    status["Speech recognition"] = "not loaded"


# ------------------------------------------------------------------ ASR

def set_vocabulary(terms: list[str]):
    """Rebuild Parakeet's decoding with a phrase-boosting tree for these terms (~30 ms)."""
    global _boost_terms
    key = tuple(sorted(set(terms)))
    if key == _boost_terms:
        return
    from omegaconf import open_dict
    asr, _ = load_models()
    dc = copy.deepcopy(_base_decoding)
    with open_dict(dc):
        dc.strategy = "greedy_batch"
        if key:
            dc.greedy.boosting_tree = {"key_phrases_list": list(key), "context_score": 1.0,
                                       "depth_scaling": 2.0, "use_triton": False}
            dc.greedy.boosting_tree_alpha = BOOST_ALPHA
    try:
        asr.change_decoding_strategy(dc, verbose=False)
        _boost_terms = key
    except Exception:
        log.exception("phrase boosting unavailable; continuing without it")
        asr.change_decoding_strategy(copy.deepcopy(_base_decoding), verbose=False)
        _boost_terms = ()


def transcribe_words(audio: np.ndarray, offset: float = 0.0, context: np.ndarray | None = None) -> list[dict]:
    """Transcribe float32 16 kHz audio -> [{word, start, end}] in absolute seconds.
    `context` (audio immediately before `audio`) is decoded too but its words are discarded."""
    ctx = context[-int(CTX_SEC * SR):] if context is not None and len(context) else np.zeros(0, np.float32)
    x = np.concatenate([ctx, audio]).astype(np.float32)
    if len(audio) < SR * 0.3:
        return []
    asr, _ = load_models()
    with torch.inference_mode():
        hyp = asr.transcribe([x], timestamps=True, verbose=False)[0]
    shift = len(ctx) / SR
    words = (getattr(hyp, "timestamp", None) or {}).get("word", [])
    out = []
    for w in words:
        if not w.get("word"):
            continue
        s, e = float(w["start"]), float(w["end"])
        if (s + e) / 2 < shift - 0.02:     # belongs to the context
            continue
        out.append({"word": w["word"], "start": offset + max(0.0, s - shift), "end": offset + max(0.0, e - shift)})
    if not out and not words and getattr(hyp, "text", "").strip():
        out = [{"word": hyp.text.strip(), "start": offset, "end": offset + len(audio) / SR}]
    return out


# ------------------------------------------------------------------ speakers / segments

def assign_speakers(words: list[dict], probs: np.ndarray, last_spk: int = 0, base: float = 0.0):
    """Label each word with the speaker whose Sortformer activity is highest over its span.
    `probs[k]` covers time base + k*FRAME_SEC."""
    for w in words:
        a = int((w["start"] - base) / FRAME_SEC)
        b = max(a + 1, int(np.ceil((w["end"] - base) / FRAME_SEC)))
        win = probs[max(0, a):max(0, b)]
        if len(win) and win.sum() > 0.2:
            last_spk = int(win.sum(0).argmax())
        w["speaker"] = last_spk
    return words, last_spk


def smooth_speakers(words: list[dict]) -> list[dict]:
    """A single short word whose label differs from identical neighbours is almost always a
    diarization flicker, not a one-word interjection inside someone else's sentence."""
    for i in range(1, len(words) - 1):
        p, w, n = words[i - 1], words[i], words[i + 1]
        if (w["speaker"] != p["speaker"] and p["speaker"] == n["speaker"]
                and w["end"] - w["start"] < 0.6 and w["start"] - p["end"] < 0.3 and n["start"] - w["end"] < 0.3):
            w["speaker"] = p["speaker"]
    # Diarization reacts a beat late at a change of speaker, so the first word or two of the new
    # speaker's sentence can stick to the previous one ("...last week. The | Zephyrine team").
    # If 1-2 words follow a sentence end and the next speaker takes over right after, move them.
    for i in range(1, len(words)):
        if not re.search(r"[.?!]$", words[i - 1]["word"]):
            continue
        for k in (1, 2):
            j = i + k
            if j >= len(words):
                break
            head = words[i:j]
            if (all(h["speaker"] == words[i - 1]["speaker"] for h in head)
                    and words[j]["speaker"] != words[i - 1]["speaker"]
                    and words[j]["start"] - head[-1]["end"] < 0.5
                    and not re.search(r"[.?!]$", head[-1]["word"])):
                for h in head:
                    h["speaker"] = words[j]["speaker"]
                break
    return words


def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def _span_rms(audio, base: float, start: float, end: float) -> float:
    if audio is None:
        return 0.0
    a, b = int((start - base) * SR), int((end - base) * SR)
    return _rms(audio[max(0, a):max(0, b)])


def remove_bleed(mic_words: list[dict], sys_words: list[dict], mic_audio=None, sys_audio=None,
                 base: float = 0.0, window: float = 0.6) -> list[dict]:
    """Drop mic words that are the computer audio leaking through the speakers into the mic.

    Decided per word, with two independent cues:
    - text: the same word is on the computer-audio channel at (almost) the same moment;
    - level: computer audio is clearly louder than the mic over the word's span. The user's own
      voice is loud on the mic and absent from the computer channel; leaked audio is the opposite.
    Unmatched words sandwiched between echo words (mis-recognised leak) are dropped too.
    `*_audio` arrays start at time `base` (seconds)."""
    if not mic_words or not sys_words:
        return mic_words
    sys_idx: dict[str, list[float]] = {}
    for w in sys_words:
        sys_idx.setdefault(_norm_word(w["word"]), []).append(w["start"])
    sys_spans = sorted((w["start"], w["end"]) for w in sys_words)

    def sys_active(w):
        return any(s < w["end"] + 0.2 and e > w["start"] - 0.2 for s, e in sys_spans)

    echo = []
    for w in mic_words:
        text_hit = any(abs(t - w["start"]) <= window for t in sys_idx.get(_norm_word(w["word"]), []))
        level_hit = False
        if mic_audio is not None and sys_audio is not None and sys_active(w):
            m = _span_rms(mic_audio, base, w["start"], w["end"])
            s = _span_rms(sys_audio, base, w["start"], w["end"])
            level_hit = s > 1.5 * m
        echo.append(text_hit or level_hit)
    # fill gaps: a non-matching word inside an echo stretch (while the far end is talking)
    for i, w in enumerate(mic_words):
        if echo[i] or not sys_active(w):
            continue
        prev_e = next((echo[j] for j in range(i - 1, -1, -1) if mic_words[i]["start"] - mic_words[j]["end"] < 1.0), False)
        next_e = next((echo[j] for j in range(i + 1, len(mic_words)) if mic_words[j]["start"] - mic_words[i]["end"] < 1.0), False)
        if prev_e and next_e:
            echo[i] = True
    return [w for w, e in zip(mic_words, echo) if not e]


def words_to_segments(words: list[dict], max_gap: float = 1.5) -> list[dict]:
    """Group words into speaker turns. A turn may continue past a short interjection by
    someone else (e.g. 'yeah' from the mic while the remote speaker keeps talking)."""
    segs: list[dict] = []
    for w in sorted(words, key=lambda x: x["start"]):
        target = None
        for s in reversed(segs[-3:]):
            if s["speaker"] == w["speaker"]:
                if w["start"] - s["end"] <= max_gap:
                    target = s
                break
            if s["end"] - s["start"] > 2.0:   # only hop over short interjections
                break
        if target:
            target["text"] += " " + w["word"]
            target["end"] = max(target["end"], w["end"])
        else:
            segs.append({"speaker": w["speaker"], "start": w["start"], "end": w["end"], "text": w["word"]})
    for s in segs:
        s["start"], s["end"] = round(s["start"], 2), round(s["end"], 2)
    return segs


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def find_cut(audio: np.ndarray, search_sec: float = 2.0, win: int = 1600) -> int:
    """Index of the quietest 100 ms window in the last `search_sec` seconds."""
    lo = max(0, len(audio) - int(search_sec * SR))
    best, best_i = None, len(audio)
    for i in range(lo, len(audio) - win, win // 2):
        e = _rms(audio[i:i + win])
        if best is None or e < best:
            best, best_i = e, i + win // 2
    return best_i


# ------------------------------------------------------------------ live

class Channel:
    """One audio channel: pause-based utterance cutting + ASR (+ optional streaming diarization)."""
    MIN_UTT = 2.0
    MAX_UTT = 10.0
    PAUSE = 0.35

    def __init__(self, diarize: bool, fixed_speaker=None):
        _, diar = load_models()
        self.diar = StreamingDiarizer(diar) if diarize else None
        self.fixed = fixed_speaker
        self.probs = np.zeros((0, 4), np.float32)
        self.pending = np.zeros(0, np.float32)
        self.pending_start = 0.0
        self.context = np.zeros(0, np.float32)   # audio just before `pending`
        self.noise = 0.003
        self.last_spk = 0
        self.done_until = 0.0                    # session time transcribed so far

    def _label(self, words):
        if self.diar is None:
            for w in words:
                w["speaker"] = self.fixed
            return words
        words, self.last_spk = assign_speakers(words, self.probs, self.last_spk)
        return words

    def _take(self, cut: int) -> list[dict]:
        piece, self.pending = self.pending[:cut], self.pending[cut:]
        start = self.pending_start
        words = []
        if _rms(piece) >= self.noise * 1.5:
            words = self._label(transcribe_words(piece, start, self.context))
        self.context = np.concatenate([self.context, piece])[-int(CTX_SEC * SR):]
        self.pending_start += cut / SR
        self.done_until = self.pending_start
        return words

    def feed(self, chunk: np.ndarray) -> list[dict]:
        if self.diar is not None:
            p = self.diar.push(chunk)
            if len(p):
                self.probs = np.concatenate([self.probs, p])
        self.pending = np.concatenate([self.pending, chunk])
        e = _rms(chunk)
        if e < self.noise * 3:
            self.noise = 0.95 * self.noise + 0.05 * max(e, 1e-4)
        dur = len(self.pending) / SR
        tail = self.pending[-int(self.PAUSE * SR):]
        paused = dur >= self.MIN_UTT and _rms(tail) < max(self.noise * 2.5, 0.004)
        if paused:
            return self._take(len(self.pending) - len(tail) // 2)
        if dur >= self.MAX_UTT:
            return self._take(find_cut(self.pending))
        return []

    def speaking(self) -> float:
        return _rms(self.pending[-SR:]) / max(self.noise, 1e-4) if len(self.pending) >= SR else 0.0

    def partial(self) -> str:
        return " ".join(w["word"] for w in transcribe_words(self.pending, 0.0, self.context))

    def finish(self) -> list[dict]:
        if self.diar is not None:
            p = self.diar.flush()
            if len(p):
                self.probs = np.concatenate([self.probs, p])
        return self._take(len(self.pending)) if len(self.pending) else []


class LiveSession:
    """Mono (mic only): the mic is diarized.  Dual (mic + computer audio): mic = "me",
    computer audio is diarized, and mic words echoing the computer audio are dropped."""

    def __init__(self, dual: bool, time_offset: float = 0.0, terms: list[str] | None = None):
        self.dual = dual
        self.offset = time_offset
        self.terms = list(terms or [])
        set_vocabulary(self.terms)
        self.mic = Channel(diarize=not dual, fixed_speaker=ME if dual else None)
        self.sys = Channel(diarize=True) if dual else None
        self.mic_audio: list[np.ndarray] = []
        self.sys_audio: list[np.ndarray] = []
        self.words: list[dict] = []          # finalized, session-relative times
        self.mic_hold: list[dict] = []       # mic words waiting for the sys channel to catch up
        self.sys_recent: list[dict] = []
        self.last_partial = 0.0
        # rolling copies of both channels (last ROLL seconds) for the echo level check
        self.roll_mic = np.zeros(0, np.float32)
        self.roll_sys = np.zeros(0, np.float32)
        self.roll_base = 0.0

    ROLL = 60.0

    def update_terms(self, terms: list[str]):
        if terms != self.terms:
            self.terms = list(terms)
            set_vocabulary(self.terms)

    def _accept(self, words):
        words = vocab.fix_words(words, self.terms)
        self.words.extend(words)

    def _release_mic(self, force=False):
        if not self.dual:
            return
        ready = [w for w in self.mic_hold if force or w["end"] + 0.5 <= self.sys.done_until
                 or self.sys.done_until - w["end"] < -20]   # don't hold forever if sys lags
        if not ready:
            return
        self.mic_hold = [w for w in self.mic_hold if w not in ready]
        self._accept(remove_bleed(ready, self.sys_recent, self.roll_mic, self.roll_sys, self.roll_base))
        horizon = (ready[-1]["end"] if ready else 0) - 30
        self.sys_recent = [w for w in self.sys_recent if w["end"] > horizon]

    def feed(self, mic: np.ndarray, sys_chunk: np.ndarray | None = None) -> tuple[bool, str | None]:
        """Returns (transcript changed, partial text or None)."""
        changed = False
        self.mic_audio.append(mic)
        mw = self.mic.feed(mic)
        if self.dual:
            sc = sys_chunk if sys_chunk is not None else np.zeros_like(mic)
            self.sys_audio.append(sc)
            self.roll_mic = np.concatenate([self.roll_mic, mic])
            self.roll_sys = np.concatenate([self.roll_sys, sc])
            extra = len(self.roll_mic) - int(self.ROLL * SR)
            if extra > SR * 5:
                self.roll_mic, self.roll_sys = self.roll_mic[extra:], self.roll_sys[extra:]
                self.roll_base += extra / SR
            sw = self.sys.feed(sc)
            if sw:
                self.sys_recent.extend(sw)
                self._accept(sw)
                changed = True
            self.mic_hold.extend(mw)
            before = len(self.words)
            self._release_mic()
            changed |= len(self.words) != before
        elif mw:
            self._accept(mw)
            changed = True

        partial = None
        now = time.time()
        if now - self.last_partial > 1.5:
            chans = [c for c in (self.mic, self.sys) if c is not None]
            loud = max(chans, key=lambda c: c.speaking())
            if loud.speaking() > 2 and len(loud.pending) >= SR:
                self.last_partial = now
                partial = loud.partial()
        return changed, partial

    def finish(self):
        mw = self.mic.finish()
        if self.dual:
            sw = self.sys.finish()
            self.sys_recent.extend(sw)
            self._accept(sw)
            self.mic_hold.extend(mw)
            self._release_mic(force=True)
        else:
            self._accept(mw)

    def segments(self) -> list[dict]:
        segs = words_to_segments(smooth_speakers(sorted(self.words, key=lambda w: w["start"])))
        for s in segs:
            s["start"] = round(s["start"] + self.offset, 2)
            s["end"] = round(s["end"] + self.offset, 2)
        return segs

    def audio(self) -> np.ndarray:
        """[N, 2] float32: column 0 mic, column 1 computer audio (zeros when not captured)."""
        mic = np.concatenate(self.mic_audio) if self.mic_audio else np.zeros(0, np.float32)
        sysa = np.concatenate(self.sys_audio) if self.sys_audio else np.zeros_like(mic)
        n = min(len(mic), len(sysa)) if self.dual else len(mic)
        return np.stack([mic[:n], sysa[:n] if self.dual else np.zeros(n, np.float32)], axis=1)


def append_wav(path, audio2: np.ndarray) -> float:
    """Append a [N, 2] session to the meeting's stereo WAV (older mono files are upgraded)."""
    if path.exists():
        old, _ = sf.read(path, dtype="float32", always_2d=True)
        if old.shape[1] == 1:
            old = np.concatenate([old, np.zeros_like(old)], axis=1)
        audio2 = np.concatenate([old[:, :2], audio2])
    sf.write(path, audio2, SR, subtype="PCM_16")
    return len(audio2) / SR


# ------------------------------------------------------------------ offline (refine)

def _offline_diar_probs(audio: np.ndarray, progress=None, label="") -> np.ndarray:
    _, diar = load_models()
    configure(diar, REFINE_CFG)
    try:
        sd = StreamingDiarizer(diar)
        probs, step = [], SR * 30
        for i in range(0, len(audio), step):
            probs.append(sd.push(audio[i:i + step]))
            if progress:
                progress(f"Diarizing {label}{i / max(1, len(audio)) * 100:.0f}%")
        probs.append(sd.flush())
    finally:
        configure(diar, LIVE_CFG)
    return np.concatenate(probs)


def _offline_words(audio: np.ndarray, progress=None, label="") -> list[dict]:
    words, pos = [], 0
    while pos < len(audio):
        end = min(len(audio), pos + SR * 40)
        if end < len(audio):
            end = pos + find_cut(audio[pos:end], search_sec=8.0)
        piece = audio[pos:end]
        if _rms(piece) > 0.002:
            words += transcribe_words(piece, pos / SR, audio[max(0, pos - int(CTX_SEC * SR)):pos])
        pos = end
        if progress:
            progress(f"Transcribing {label}{pos / len(audio) * 100:.0f}%")
    return words


def reprocess(path, sessions: list[dict] | None, terms: list[str], progress=None) -> tuple[list[dict], float]:
    """Full-recording pass with the high-accuracy Sortformer setting, full-file speaker
    identities (one diarization run per channel across all sessions), long ASR windows with
    context, vocabulary boosting/fixing, echo removal and smoothing."""
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    assert sr == SR
    mic = audio[:, 0]
    sysa = audio[:, 1] if audio.shape[1] > 1 else np.zeros_like(mic)
    total = len(mic) / SR
    if not sessions:
        sessions = [{"start": 0.0, "end": total, "dual": _rms(sysa) > 1e-3}]
    set_vocabulary(terms)

    any_dual = any(s.get("dual") for s in sessions)
    any_mono = any(not s.get("dual") for s in sessions)
    mic_words = _offline_words(mic, progress, "mic ")
    sys_words = _offline_words(sysa, progress, "computer audio ") if any_dual else []
    mic_probs = _offline_diar_probs(mic, progress, "mic ") if any_mono else None
    sys_probs = _offline_diar_probs(sysa, progress, "computer audio ") if any_dual else None

    def in_sess(w, s):
        mid = (w["start"] + w["end"]) / 2
        return s["start"] <= mid < s["end"] + 1e-6

    out = []
    for s in sessions:
        mw = [dict(w) for w in mic_words if in_sess(w, s)]
        if s.get("dual"):
            sw = [dict(w) for w in sys_words if in_sess(w, s)]
            sw, _ = assign_speakers(sw, sys_probs)
            for w in mw:
                w["speaker"] = ME
            out += sw + remove_bleed(mw, sw, mic, sysa, 0.0)
        else:
            mw, _ = assign_speakers(mw, mic_probs)
            out += mw
    out = vocab.fix_words(sorted(out, key=lambda w: w["start"]), terms)
    return words_to_segments(smooth_speakers(out)), total
