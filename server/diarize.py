"""Streaming speaker diarization with NVIDIA Streaming Sortformer (4 speakers).

Mirrors NeMo's `streaming_feat_loader` + `forward_streaming_step`, but consumes audio
incrementally: each step takes `chunk_len` diarization frames (80 ms each) plus left/right
context, and the model's speaker cache keeps speaker identities consistent across the
whole meeting.
"""
import os

import numpy as np
import torch

DIAR_MODEL = os.environ.get("MURMUR_DIAR_MODEL", "nvidia/diar_streaming_sortformer_4spk-v2.1")
FRAME_SEC = 0.08          # one Sortformer output frame
HOP = 160                 # 10 ms feature hop at 16 kHz
SUB = 8                   # feature frames per Sortformer frame
SR = 16000

# (chunk_len, left_ctx, right_ctx, fifo_len, spkcache_refresh_rate, spkcache_len)
# Larger chunks run far faster than real time on CPU; latency of a few seconds is fine for notes.
LIVE_CFG = dict(chunk_len=int(os.environ.get("MURMUR_DIAR_CHUNK", "31")),
                chunk_left_context=1, chunk_right_context=7,
                fifo_len=124, spkcache_refresh_rate=124, spkcache_len=188)


def load_model(device: str = "cpu"):
    from nemo.collections.asr.models import SortformerEncLabelModel
    if DIAR_MODEL.endswith(".nemo"):
        m = SortformerEncLabelModel.restore_from(DIAR_MODEL, map_location=device)
    else:
        m = SortformerEncLabelModel.from_pretrained(DIAR_MODEL, map_location=device)
    m.eval()
    configure(m, LIVE_CFG)
    return m


def configure(model, cfg: dict):
    sm = model.sortformer_modules
    for k, v in cfg.items():
        setattr(sm, k, v)
    sm.log = False


class StreamingDiarizer:
    def __init__(self, model):
        self.m = model
        self.sm = model.sortformer_modules
        self.reset()

    @property
    def n_spk(self):
        return self.sm.n_spk

    def reset(self):
        self.audio = np.zeros(0, dtype=np.float32)
        self.audio_offset = 0                   # global sample index of self.audio[0]
        self.next_feat = 0                      # global feature frame where the next chunk starts
        self.state = self.sm.init_streaming_state(batch_size=1, async_streaming=self.m.async_streaming,
                                                  device=self.m.device)
        self.total_preds = torch.zeros((1, 0, self.n_spk), device=self.m.device)

    def _features(self, start_feat: int, n_feat: int) -> torch.Tensor:
        s = start_feat * HOP - self.audio_offset
        seg = self.audio[s: s + n_feat * HOP]
        x = torch.from_numpy(seg).unsqueeze(0).to(self.m.device)
        feats, _ = self.m.preprocessor(input_signal=x, length=torch.tensor([x.shape[1]], device=self.m.device))
        feats = feats[:, :, :n_feat]
        if feats.shape[2] < n_feat:  # pad the very last frame if the window came up short
            feats = torch.nn.functional.pad(feats, (0, n_feat - feats.shape[2]), value=self.m.negative_init_val)
        return feats

    def _step(self, right_ctx: int, final: bool = False) -> bool:
        avail = (self.audio_offset + len(self.audio)) // HOP
        chunk = self.sm.chunk_len * SUB
        stt = self.next_feat
        end = min(stt + chunk, avail) if final else stt + chunk
        right = min(right_ctx, avail - end)
        if end <= stt or (not final and end + right_ctx > avail):
            return False
        left = min(self.sm.chunk_left_context * SUB, stt)
        feats = self._features(stt - left, left + (end - stt) + right)
        with torch.inference_mode():
            self.state, self.total_preds = self.m.forward_streaming_step(
                processed_signal=feats.transpose(1, 2),
                processed_signal_length=torch.tensor([feats.shape[2]], device=self.m.device),
                streaming_state=self.state, total_preds=self.total_preds,
                left_offset=left, right_offset=right)
        self.next_feat = end
        # drop audio we no longer need (keep left context)
        keep_from = max(0, (self.next_feat - self.sm.chunk_left_context * SUB) * HOP - self.audio_offset)
        if keep_from > SR * 5:
            self.audio = self.audio[keep_from:]
            self.audio_offset += keep_from
        return True

    def push(self, samples: np.ndarray) -> np.ndarray:
        """Feed float32 16 kHz mono audio. Returns newly finalized speaker probs [frames, n_spk]."""
        before = self.total_preds.shape[1]
        self.audio = np.concatenate([self.audio, samples.astype(np.float32)])
        while self._step(self.sm.chunk_right_context * SUB):
            pass
        return self.total_preds[0, before:].cpu().numpy()

    def flush(self) -> np.ndarray:
        before = self.total_preds.shape[1]
        while self._step(self.sm.chunk_right_context * SUB, final=True):
            pass
        return self.total_preds[0, before:].cpu().numpy()


def probs_to_segments(probs: np.ndarray, threshold: float = 0.5, min_dur: float = 0.24):
    """Frame probabilities -> [(start, end, speaker)] using a simple per-speaker threshold."""
    segs = []
    for s in range(probs.shape[1]):
        on = probs[:, s] > threshold
        t = 0
        while t < len(on):
            if on[t]:
                u = t
                while u < len(on) and on[u]:
                    u += 1
                if (u - t) * FRAME_SEC >= min_dur:
                    segs.append((t * FRAME_SEC, u * FRAME_SEC, s))
                t = u
            else:
                t += 1
    return sorted(segs)
