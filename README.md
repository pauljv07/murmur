# Murmur

Local AI meeting notes. Record a meeting, jot rough notes, then **Enhance** to merge your notes
with the transcript. Speech recognition, speaker diarization and the language model all run
on this Mac. No audio or text leaves the machine (after the one-time model download).

## Run

    ./run.sh              # then open http://127.0.0.1:8765

Pick what to record in the bottom bar:

- **Mic + computer audio** (default, most accurate): a native helper (`native/syscap`, ScreenCaptureKit)
  captures whatever the Mac plays (Zoom, Meet, Teams, browser...) as a separate channel. Your
  mic is labelled **Me**; the remote side is diarized into Speaker 1, 2, ... First use needs
  *Screen & System Audio Recording* permission for your terminal (System Settings → Privacy & Security).
- **Mic + shared tab/screen**: same two-channel pipeline, but Chrome captures the audio (no helper needed).
- **Mic only**: for in-person meetings; everyone is separated by voice.

## Models

| Stage | Model | Runtime |
|---|---|---|
| Speaker diarization | `nvidia/diar_streaming_sortformer_4spk-v2.1` (Streaming Sortformer, up to 4 speakers) | NeMo / PyTorch (MPS) |
| Speech-to-text | `nvidia/parakeet-tdt-0.6b-v3` (25 languages, word timestamps) | NeMo / PyTorch (CPU) |
| Notes, titles, chat | `mlx-community/Qwen3-4B-Instruct-2507-4bit` | MLX |

Override with env vars: `MURMUR_DIAR_MODEL`, `MURMUR_ASR_MODEL`, `MURMUR_LLM`
(e.g. `mlx-community/Qwen3-8B-4bit` for better notes, slower), `MURMUR_DIAR_DEVICE`,
`MURMUR_ASR_DEVICE`, `MURMUR_DIAR_CHUNK` (Sortformer chunk in 80 ms frames; default 31 ≈ 2.5 s),
`MURMUR_BOOST_ALPHA` (vocabulary boost strength, default 1.0).

## Transcript accuracy

What Murmur does to get the transcript right:

| Technique | What it fixes |
|---|---|
| Separate mic / computer-audio channels | Who said what: your words are "Me" with certainty; remote speech is clean digital audio |
| Echo removal (per word: same word on both channels at the same moment, or computer audio louder than the mic) | Remote voices leaking from your speakers into the mic are not transcribed twice |
| Custom vocabulary → Parakeet phrase boosting (NeMo GPU-PB boosting tree) | Names, products, jargon. Sources: per-meeting field, global list (sidebar), speaker names, proper nouns in your notes/title |
| Conservative spelling fix against the vocabulary | Terms boosting still missed ("Cuba Flow" → "Kubeflow"); never rewrites ordinary words into terms |
| 3 s of preceding audio as ASR context; cuts at pauses | Words at chunk boundaries |
| Speaker smoothing + sentence-boundary fix | One-word speaker flicker; first word of a turn sticking to the previous speaker |
| **Refine**: whole-recording pass, Sortformer high-accuracy setting (30 s chunks), 40 s ASR windows, one diarization run per channel across all sessions | Live-mode compromises |

Benchmark (synthetic 3-person call, 42 s, remote audio leaking into the mic at −18 dB):

| Mode | WER live | WER refined | Words with correct speaker |
|---|---|---|---|
| Previous: mic + computer audio mixed into one stream | 10.6% | 7.7% | ~70% |
| Two channels + vocabulary | 5.8% | 3.8% | 100% |
| Two channels + vocabulary, native capture through the speakers | ~0% | 0% | 100% |

## How it works

- Browser captures 16 kHz mono audio (AudioWorklet) and streams it over a WebSocket.
- `server/diarize.py` runs Streaming Sortformer incrementally (`forward_streaming_step` with a
  speaker cache, so speaker identities stay consistent across the whole meeting).
- `server/pipeline.py` cuts audio at pauses, transcribes with Parakeet, and assigns each word to
  the speaker with the highest Sortformer activity over that word's time span.
- **Refine** re-runs both models over the whole saved recording for a cleaner transcript.
- Data lives in `data/` (JSON per meeting + WAV).
