"""Live + offline speech pipeline: Parakeet ASR + Streaming Sortformer diarization."""
import logging
import os
import threading
import time

import numpy as np
import soundfile as sf
import torch

from diarize import FRAME_SEC, SR, StreamingDiarizer, load_model as load_diar

ASR_MODEL = os.environ.get("MURMUR_ASR_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
DIAR_DEVICE = os.environ.get("MURMUR_DIAR_DEVICE", "mps" if torch.backends.mps.is_available() else "cpu")
ASR_DEVICE = os.environ.get("MURMUR_ASR_DEVICE", "cpu")

log = logging.getLogger("murmur")

status = {"Speech recognition": "not loaded", "Speaker diarization": "not loaded", "Language model": "not loaded"}
_asr = None
_diar = None
_load_lock = threading.Lock()
gpu_lock = threading.Lock()   # one recording / reprocess at a time


def load_models():
    global _asr, _diar
    with _load_lock:
        if _asr is None:
            status["Speech recognition"] = "loading"
            from nemo.collections.asr.models import ASRModel
            _asr = ASRModel.from_pretrained(ASR_MODEL, map_location=ASR_DEVICE)
            _asr.eval()
            status["Speech recognition"] = "ready"
        if _diar is None:
            status["Speaker diarization"] = "loading"
            try:
                _diar = load_diar(DIAR_DEVICE)
            except Exception:
                log.exception("diarizer on %s failed, using cpu", DIAR_DEVICE)
                _diar = load_diar("cpu")
            status["Speaker diarization"] = "ready"
    return _asr, _diar


def transcribe_words(audio: np.ndarray, offset: float = 0.0) -> list[dict]:
    """Transcribe float32 16 kHz audio -> [{word, start, end}] with absolute times."""
    if len(audio) < SR * 0.3:
        return []
    asr, _ = load_models()
    with torch.inference_mode():
        hyp = asr.transcribe([audio], timestamps=True, verbose=False)[0]
    words = (hyp.timestamp or {}).get("word", []) if hasattr(hyp, "timestamp") else []
    out = [{"word": w["word"], "start": offset + float(w["start"]), "end": offset + float(w["end"])}
           for w in words if w.get("word")]
    if not out and getattr(hyp, "text", "").strip():  # no timestamps: spread text over the span
        out = [{"word": hyp.text.strip(), "start": offset, "end": offset + len(audio) / SR}]
    return out


def assign_speakers(words: list[dict], probs: np.ndarray, last_spk: int = 0, base: float = 0.0):
    """Label each word with the speaker whose Sortformer activity is highest over the word span.
    `probs[k]` covers time base + k*FRAME_SEC."""
    for w in words:
        a = int((w["start"] - base) / FRAME_SEC)
        b = max(a + 1, int(np.ceil((w["end"] - base) / FRAME_SEC)))
        win = probs[max(0, a):max(0, b)]
        if len(win) and win.sum() > 0.2:
            last_spk = int(win.sum(0).argmax())
        w["speaker"] = last_spk
    return words, last_spk


def words_to_segments(words: list[dict], max_gap: float = 1.5) -> list[dict]:
    segs = []
    for w in words:
        s = segs[-1] if segs else None
        if s and s["speaker"] == w["speaker"] and w["start"] - s["end"] <= max_gap:
            s["text"] += " " + w["word"]
            s["end"] = w["end"]
        else:
            segs.append({"speaker": w["speaker"], "start": round(w["start"], 2),
                         "end": round(w["end"], 2), "text": w["word"]})
    for s in segs:
        s["end"] = round(s["end"], 2)
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


class LiveSession:
    """Consumes audio chunks, emits finalized transcript segments with speaker labels.

    ASR runs on utterance-sized pieces cut at pauses (or forced every MAX_UTT seconds at the
    quietest point); diarization runs continuously on the stream and words are matched to
    the speaker activity frames by time.
    """
    MIN_UTT = 2.0
    MAX_UTT = 10.0
    PAUSE = 0.35

    def __init__(self, time_offset: float = 0.0):
        _, diar = load_models()
        self.diar = StreamingDiarizer(diar)
        self.offset = time_offset              # seconds already recorded in previous sessions
        self.audio: list[np.ndarray] = []      # whole session, for saving
        self.pending = np.zeros(0, np.float32)  # not yet transcribed
        self.pending_start = 0.0               # session time of pending[0]
        self.probs = np.zeros((0, self.diar.n_spk), np.float32)
        self.last_spk = 0
        self.noise = 0.003                     # adaptive noise floor
        self.last_partial = 0.0

    @property
    def elapsed(self) -> float:
        return sum(len(a) for a in self.audio) / SR

    def _emit(self, audio: np.ndarray, start: float) -> list[dict]:
        words = transcribe_words(audio, start)
        words, self.last_spk = assign_speakers(words, self.probs, self.last_spk)
        segs = words_to_segments(words)
        for s in segs:
            s["start"] = round(s["start"] + self.offset, 2)
            s["end"] = round(s["end"] + self.offset, 2)
        return segs

    def feed(self, chunk: np.ndarray) -> tuple[list[dict], str | None]:
        """Returns (finalized segments, partial text or None)."""
        self.audio.append(chunk)
        self.probs = np.concatenate([self.probs, self.diar.push(chunk)])
        self.pending = np.concatenate([self.pending, chunk])
        e = _rms(chunk)
        if e < self.noise * 3:
            self.noise = 0.95 * self.noise + 0.05 * max(e, 1e-4)

        dur = len(self.pending) / SR
        tail = self.pending[-int(self.PAUSE * SR):]
        paused = dur >= self.MIN_UTT and _rms(tail) < max(self.noise * 2.5, 0.004)
        if paused or dur >= self.MAX_UTT:
            cut = len(self.pending) - len(tail) // 2 if paused else find_cut(self.pending)
            piece, self.pending = self.pending[:cut], self.pending[cut:]
            start = self.pending_start
            self.pending_start += cut / SR
            if _rms(piece) < self.noise * 1.5:      # silence: skip ASR
                return [], ""
            return self._emit(piece, start), ""

        partial = None
        now = time.time()
        if dur >= 1.0 and now - self.last_partial > 1.5 and _rms(self.pending) > self.noise * 2:
            self.last_partial = now
            partial = " ".join(w["word"] for w in transcribe_words(self.pending))
        return [], partial

    def finish(self) -> list[dict]:
        self.probs = np.concatenate([self.probs, self.diar.flush()])
        segs = self._emit(self.pending, self.pending_start) if _rms(self.pending) > self.noise * 1.5 else []
        self.pending = np.zeros(0, np.float32)
        return segs

    def session_audio(self) -> np.ndarray:
        return np.concatenate(self.audio) if self.audio else np.zeros(0, np.float32)


def append_wav(path, audio: np.ndarray):
    if path.exists():
        old, _ = sf.read(path, dtype="float32")
        audio = np.concatenate([old, audio])
    sf.write(path, audio, SR, subtype="PCM_16")
    return len(audio) / SR


# ------------------------------------------------------------------ offline (refine)

def reprocess(path, progress=None) -> tuple[list[dict], float]:
    """Full-file pass: streaming Sortformer over the whole recording (one consistent speaker
    cache) and Parakeet ASR over pause-aligned ~30 s pieces."""
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(1)
    assert sr == SR
    _, diar = load_models()
    sd = StreamingDiarizer(diar)
    probs = []
    step = SR * 10
    for i in range(0, len(audio), step):
        probs.append(sd.push(audio[i:i + step]))
        if progress:
            progress(f"Diarizing {i / max(1, len(audio)) * 100:.0f}%")
    probs.append(sd.flush())
    probs = np.concatenate(probs)

    words, pos = [], 0
    while pos < len(audio):
        end = min(len(audio), pos + SR * 30)
        if end < len(audio):
            end = pos + find_cut(audio[pos:end], search_sec=5.0)
        piece = audio[pos:end]
        if _rms(piece) > 0.002:
            words += transcribe_words(piece, pos / SR)
        pos = end
        if progress:
            progress(f"Transcribing {pos / len(audio) * 100:.0f}%")
    words, _ = assign_speakers(words, probs)
    return words_to_segments(words), len(audio) / SR
