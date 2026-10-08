"""Local LLM (MLX) for note enhancement, titles and chat. Nothing leaves the machine."""
import os
import threading

MODEL_ID = os.environ.get("MURMUR_LLM", "mlx-community/Qwen3-4B-Instruct-2507-4bit")
MAX_TRANSCRIPT_CHARS = int(os.environ.get("MURMUR_MAX_TRANSCRIPT_CHARS", "60000"))

_model = None
_tok = None
_load_lock = threading.Lock()
_gen_lock = threading.Lock()  # MLX generation is not re-entrant on one model


def load():
    global _model, _tok
    with _load_lock:
        if _model is None:
            from mlx_lm import load as mlx_load
            _model, _tok = mlx_load(MODEL_ID)
    return _model, _tok


def stream(messages: list[dict], max_tokens: int = 1500, temp: float = 0.3):
    """Yield text pieces for a chat completion."""
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler

    model, tok = load()
    prompt = tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    with _gen_lock:
        for r in stream_generate(model, tok, prompt, max_tokens=max_tokens,
                                 sampler=make_sampler(temp=temp)):
            yield r.text


def complete(messages: list[dict], **kw) -> str:
    return "".join(stream(messages, **kw))


# ---------------------------------------------------------------- prompts

TEMPLATES = {
    "general": ("General meeting",
                "Sections: a one-line **Summary**, then topic sections with bullet points "
                "(name each section after what was discussed), then **Next steps** "
                "with owners when known."),
    "one_on_one": ("1:1",
                   "Sections: **Updates**, **Wins**, **Blockers / concerns**, **Feedback**, "
                   "**Action items**."),
    "standup": ("Standup",
                "One section per person (use speaker names) with **Yesterday**, **Today**, "
                "**Blockers** bullets. End with **Follow-ups**."),
    "customer": ("Customer call",
                 "Sections: **Context** (who they are), **Pain points**, **Current solution**, "
                 "**Requirements**, **Objections**, **Budget & timeline**, **Next steps**."),
    "interview": ("Interview",
                  "Sections: **Candidate background**, **Strengths**, **Concerns**, "
                  "**Notable answers**, **Overall impression** (neutral, evidence-based)."),
    "lecture": ("Lecture / talk",
                "Sections: **Key ideas**, **Details & examples**, **Open questions**, "
                "**Terms to remember**."),
}


def format_transcript(m: dict) -> str:
    names = m.get("speakers", {})
    lines = []
    for s in m.get("transcript", []):
        who = names.get(str(s["speaker"])) or f"Speaker {int(s['speaker']) + 1}"
        mm, ss = divmod(int(s["start"]), 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {who}: {s['text']}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:  # keep head and tail when very long
        half = MAX_TRANSCRIPT_CHARS // 2
        text = text[:half] + "\n[... middle of transcript omitted ...]\n" + text[-half:]
    return text


def enhance_messages(m: dict) -> list[dict]:
    name, structure = TEMPLATES.get(m.get("template") or "general", TEMPLATES["general"])
    sys = (
        "You turn a meeting transcript plus the user's rough notes into clean, accurate meeting "
        "notes in Markdown. Rules:\n"
        "- The user's own notes are the backbone: keep every point they wrote (rephrase lightly "
        "for clarity) and expand each with relevant detail from the transcript.\n"
        "- Add other important points from the transcript the user missed.\n"
        "- Only state what is supported by the transcript or notes. Never invent facts, names, "
        "numbers, dates, owners, teams or next steps that nobody said. Refer to people by the "
        "speaker labels/names used in the transcript (e.g. 'Speaker 2').\n"
        "- If a template section has nothing to go in it, omit the section.\n"
        "- Be concise: short bullets, no filler, no preamble, no closing remarks.\n"
        "- Use ### for section headings and '-' for bullets.\n"
        f"- Template: {name}. {structure}"
    )
    user = (
        f"Meeting title: {m.get('title') or '(untitled)'}\n\n"
        f"## My notes\n{m.get('notes') or '(none)'}\n\n"
        f"## Transcript\n{format_transcript(m) or '(empty)'}"
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def title_messages(m: dict) -> list[dict]:
    return [
        {"role": "system", "content": "Reply with only a short meeting title (3-7 words), "
                                      "no quotes, no punctuation at the end."},
        {"role": "user", "content": f"Notes:\n{m.get('notes','')[:2000]}\n\n"
                                    f"Transcript:\n{format_transcript(m)[:6000]}"},
    ]


def chat_messages(m: dict, question: str) -> list[dict]:
    sys = (
        "You answer questions about a single meeting using its transcript and notes. Be direct "
        "and brief. Quote or cite timestamps like [12:30] when helpful. If the answer is not in "
        "the meeting, say so. You may also draft follow-up emails, summaries, etc. on request."
    )
    ctx = (
        f"Meeting title: {m.get('title') or '(untitled)'}\n\n"
        f"## User notes\n{m.get('notes') or '(none)'}\n\n"
        f"## Enhanced notes\n{m.get('enhanced') or '(none)'}\n\n"
        f"## Transcript\n{format_transcript(m) or '(empty)'}"
    )
    msgs = [{"role": "system", "content": sys}, {"role": "user", "content": ctx},
            {"role": "assistant", "content": "Got it. What would you like to know?"}]
    msgs += m.get("chat", [])[-8:]
    msgs.append({"role": "user", "content": question})
    return msgs
