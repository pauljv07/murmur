# Murmur

**Private AI meeting notes that run entirely on your Mac.**
Record a meeting, jot a few rough notes, and Murmur turns them into clean, organised notes using
the transcript. It knows who said what, and you can ask it questions about the meeting.

- 🔒 **Private:** nothing you record ever leaves your Mac. No account, no cloud.
- ✈️ **Works offline:** after the first setup it needs no internet.
- 💸 **Free:** no subscription.

---

## Install (about 5 minutes)

### ⬇️ [Download Murmur for Mac](https://github.com/pauljv07/murmur/releases/latest/download/Murmur-macOS-arm64.zip)

**You need:** a Mac with an Apple chip (M1 or newer) and macOS 14 Sonoma or newer.
*Not sure? Apple menu  → **About This Mac**. It should say "Chip: Apple M…".*

**1. Install it**
Open your **Downloads** folder and double-click **Murmur-macOS-arm64.zip**.
Drag the **Murmur** app into your **Applications** folder.

**2. Open it (the first time only, macOS asks you to confirm)**
Double-click **Murmur** in Applications. macOS will say it can't verify the app (Murmur is free
and isn't registered with Apple). To allow it:
- Click **Done**.
- Open **System Settings → Privacy & Security** and scroll down.
- Next to *"Murmur" was blocked*, click **Open Anyway**, then confirm with your password or Touch ID.

You only need to do this once.

**3. Wait for setup**
A setup page opens in your web browser. Leave it open for a few minutes while Murmur installs
its parts (about 2 GB).

**4. Choose your AI models**
On the **Welcome** screen, the best choice for your Mac is already selected, so just click
**Download and get started**. When it finishes, click **Open Murmur**. That's it! 🎉

> **Prefer one step?** Paste this into **Terminal** (in Applications → Utilities). It downloads,
> installs and opens Murmur, and skips step 2:
> ```
> curl -L https://github.com/pauljv07/murmur/releases/latest/download/Murmur-macOS-arm64.zip -o /tmp/Murmur.zip && ditto -x -k /tmp/Murmur.zip /Applications && open /Applications/Murmur.app
> ```

---

## Using Murmur

1. Click **New note**, then **Record**.
2. Type rough notes while you talk. A few words are enough.
3. Click **Stop**, then **✦ Enhance**. Murmur writes proper notes from your notes and the transcript.
4. Ask anything in the box at the bottom, for example *"What did we decide?"* or *"Write a follow-up email"*.

**The first time you record**, allow these two things when asked:
- **Microphone:** your browser asks. Click **Allow**.
- **Screen & System Audio Recording:** macOS asks. This lets Murmur hear the other people on a
  Zoom, Meet or Teams call. (Murmur only uses the audio, never your screen.)

**Tips**
- In the bottom bar, pick **Mic only** for in-person meetings and **Mic + computer audio** for calls.
- Click a speaker's name (like *Speaker 1*) to rename them.
- Add names or jargon to **Vocabulary** so they're spelled correctly.
- **Models** in the sidebar lets you switch to a faster or smarter AI any time.

---

## Questions

**Where is Murmur? I don't see a window.**
Murmur runs in your web browser. Open the Murmur app again and the page comes back, or go to
<http://127.0.0.1:8765>. There's no Dock icon.

**How do I quit?** Click **Quit Murmur** at the bottom of the sidebar.

**The other people on my call aren't in the transcript.**
Open **System Settings → Privacy & Security → Screen & System Audio Recording**, turn on
**Murmur**, then quit and reopen Murmur.

**My microphone isn't working.**
In your browser, click the icon on the left of the address bar and set **Microphone** to **Allow**.

**Which AI model should I pick?**
Keep the recommended one. In short: **Light** for 8 GB Macs, **Balanced** for 16 GB, **Best** for 32 GB+.
Bigger models write better notes but are slower.

**How much space does it need?** About 5–15 GB, depending on the models you choose.

**Is it really private?** Yes. Recording, transcription and the AI all run on your Mac. Murmur
only uses the internet to download its parts and models, and you can turn Wi-Fi off after that.

**How do I update?** Download and install it again the same way. Your notes are kept.

**How do I uninstall?** Quit Murmur, delete it from Applications, and delete the folder
`~/Library/Application Support/Murmur` (your notes are in there). The AI models are in
`~/.cache/huggingface`.

---

## For developers

### Run from source

    ./run.sh              # then open http://127.0.0.1:8765

Build the app with `packaging/build.sh <path-to-uv-binary> <version>` (needs Xcode command line
tools for the native audio helper; get `uv` for aarch64-apple-darwin from the astral-sh/uv
releases). It produces `dist/Murmur.app`, a versioned zip, and `dist/Murmur-macOS-arm64.zip`
(attach this one to each release so the download link above always gets the latest version).

### Models

Chosen in the app (Welcome screen / **Models**). They're downloaded from Hugging Face, fetching
only the files each model needs, and then load strictly from the local cache.

| Role | Options |
|---|---|
| Speech-to-text | NVIDIA Parakeet TDT 0.6B v3 (25 languages) or v2 (English) |
| Speaker diarization | NVIDIA Streaming Sortformer 4spk v2.1 |
| Notes, titles, chat | Qwen3 1.7B, Qwen3 4B Instruct, Qwen3 8B or Qwen3 14B (4-bit MLX; thinking mode off) |

Developer overrides: `MURMUR_ASR_MODEL`, `MURMUR_DIAR_MODEL`, `MURMUR_LLM` (any Hugging Face repo id),
`MURMUR_DIAR_DEVICE`, `MURMUR_ASR_DEVICE`, `MURMUR_DIAR_CHUNK` (Sortformer chunk in 80 ms frames;
default 31 ≈ 2.5 s), `MURMUR_BOOST_ALPHA` (vocabulary boost strength, default 1.0).

### Audio sources

- **Mic + computer audio** (default, most accurate): a native helper (`native/syscap`,
  ScreenCaptureKit) captures whatever the Mac plays as a separate channel. Your mic is labelled
  **Me**; the remote side is diarized into Speaker 1, 2, …
- **Mic + shared tab/screen**: the same two-channel pipeline, with Chrome capturing the audio.
- **Mic only**: for in-person meetings; everyone is separated by voice.

### Transcript accuracy

| Technique | What it fixes |
|---|---|
| Separate mic / computer-audio channels | Who said what: your words are "Me" with certainty, and remote speech is clean digital audio |
| Echo removal (per word: the same word on both channels at the same moment, or computer audio louder than the mic) | Remote voices leaking from your speakers into the mic aren't transcribed twice |
| Custom vocabulary → Parakeet phrase boosting (NeMo GPU-PB boosting tree) | Names, products, jargon. Sources: per-meeting field, global list, speaker names, proper nouns in your notes and title |
| Conservative spelling fix against the vocabulary | Terms boosting still missed ("Cuba Flow" → "Kubeflow"); never rewrites ordinary words into terms |
| 3 s of preceding audio as ASR context; cuts at pauses | Words at chunk boundaries |
| Speaker smoothing + sentence-boundary fix | One-word speaker flicker; first word of a turn sticking to the previous speaker |
| **Refine**: whole-recording pass, Sortformer high-accuracy setting, 40 s ASR windows, one diarization run per channel | Live-mode compromises |

Benchmark (synthetic 3-person call, 42 s, remote audio leaking into the mic at −18 dB):

| Mode | WER live | WER refined | Words with correct speaker |
|---|---|---|---|
| Mic + computer audio mixed into one stream | 10.6% | 7.7% | ~70% |
| Two channels + vocabulary | 5.8% | 3.8% | 100% |

### Offline by design

After setup, models load in strict offline mode and the web UI loads nothing from external sites.
It was tested with all network access blocked while recording, transcribing, enhancing and
chatting, and no connections left the machine. Model downloads from the **Models** screen run in a
separate process with network access.

### How it works

- The browser captures 16 kHz audio (AudioWorklet) and streams it over a WebSocket; computer
  audio comes from the native helper or a shared tab.
- `server/diarize.py` runs Streaming Sortformer incrementally (`forward_streaming_step` with a
  speaker cache, so speaker identities stay consistent across the meeting).
- `server/pipeline.py` cuts audio at pauses, transcribes with Parakeet, and assigns each word to
  the speaker with the highest Sortformer activity over that word's time span.
- `server/models.py` holds the model catalog, the download manager and the user's selection.
- `packaging/` builds the Mac app: a launcher, a first-run setup page (`bootstrap.py`, which
  installs pinned packages with a bundled `uv`) and an ad-hoc code signature.

## Credits

The interface's visual style is modelled on the [llama.cpp](https://github.com/ggml-org/llama.cpp)
web UI (MIT License). Models: NVIDIA Parakeet and Streaming Sortformer (NeMo), and Qwen3 (Apache 2.0)
via MLX. They're downloaded on first use, not bundled. See `packaging/THIRD_PARTY_NOTICES.md`.
