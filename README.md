# Murmur

Local AI meeting notes. Record a meeting, jot rough notes, then **Enhance** to merge your notes
with the transcript. Speech recognition, speaker diarization and the language model all run
on this Mac. No audio or text leaves the machine (after the one-time model download).

## Run

    ./run.sh              # then open http://127.0.0.1:8765

Use Chrome for the "Computer audio" toggle (captures a call/tab playing on this machine
alongside the mic). Without it only the microphone is recorded, so use speakers instead of headphones.

## Models

| Stage | Model | Runtime |
|---|---|---|
| Speaker diarization | `nvidia/diar_streaming_sortformer_4spk-v2.1` (Streaming Sortformer, up to 4 speakers) | NeMo / PyTorch (MPS) |
| Speech-to-text | `nvidia/parakeet-tdt-0.6b-v3` (25 languages, word timestamps) | NeMo / PyTorch (CPU) |
| Notes, titles, chat | `mlx-community/Qwen3-4B-Instruct-2507-4bit` | MLX |

Override with env vars: `MURMUR_DIAR_MODEL`, `MURMUR_ASR_MODEL`, `MURMUR_LLM`
(e.g. `mlx-community/Qwen3-8B-4bit` for better notes, slower), `MURMUR_DIAR_DEVICE`,
`MURMUR_ASR_DEVICE`, `MURMUR_DIAR_CHUNK` (Sortformer chunk in 80 ms frames; default 31 ≈ 2.5 s).

## How it works

- Browser captures 16 kHz mono audio (AudioWorklet) and streams it over a WebSocket.
- `server/diarize.py` runs Streaming Sortformer incrementally (`forward_streaming_step` with a
  speaker cache, so speaker identities stay consistent across the whole meeting).
- `server/pipeline.py` cuts audio at pauses, transcribes with Parakeet, and assigns each word to
  the speaker with the highest Sortformer activity over that word's time span.
- **Refine** re-runs both models over the whole saved recording for a cleaner transcript.
- Data lives in `data/` (JSON per meeting + WAV).
