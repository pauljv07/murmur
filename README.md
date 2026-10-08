# Murmur

Local AI meeting notes. Record a meeting, jot rough notes, then **Enhance** to merge your notes
with the transcript. Speech recognition, speaker diarization and the language model all run
on this Mac. No audio or text leaves the machine (after the one-time model download).

## Install (Mac app)

**Requirements:** a Mac with Apple silicon (M1 or newer), macOS 14 or later, 16 GB of RAM
recommended, about 12 GB of free disk space, and an internet connection for the first launch only.

1. Download `Murmur-<version>-macOS-arm64.zip`, unzip it, and drag **Murmur.app** to Applications.
2. Open it. Murmur isn't signed with an Apple Developer ID, so macOS blocks the first launch:
   open **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to
   the Murmur message. (Alternatively, in Terminal: `xattr -dr com.apple.quarantine /Applications/Murmur.app`.)
3. A setup page opens in your browser. The first launch installs a private Python environment and
   downloads the models (~10 GB in total); this takes a while on a slow connection.
4. When setup finishes the page turns into the app. Allow microphone access in the browser, and
   allow **Screen & System Audio Recording** for Murmur when macOS asks (needed to capture the
   other side of calls).

Murmur runs in the background with no Dock icon: open the app again to bring the page back, and use
**Quit Murmur** in the sidebar to stop it. Notes live in `~/Library/Application Support/Murmur/data`.
To uninstall, delete the app and that `Murmur` folder (models are cached in `~/.cache/huggingface`).

### Works offline

After the first launch Murmur needs no internet connection. It loads every model from the local
cache in strict offline mode, and the web UI loads nothing from external sites. It was tested
with all network access blocked while recording, transcribing, enhancing and chatting: no
connections left the machine.

## Run from source

    ./run.sh              # then open http://127.0.0.1:8765

Build the app yourself with `packaging/build.sh <path-to-uv-binary> <version>` (needs Xcode command
line tools for the native audio helper; get `uv` for aarch64-apple-darwin from astral-sh/uv releases).

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
| Notes, titles, chat | `mlx-community/Qwen3-8B-4bit` (thinking mode off) | MLX |

Override with env vars: `MURMUR_DIAR_MODEL`, `MURMUR_ASR_MODEL`, `MURMUR_LLM`
(e.g. `mlx-community/Qwen3-4B-Instruct-2507-4bit` for a lighter, faster model), `MURMUR_DIAR_DEVICE`,
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
