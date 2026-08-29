"""OpenAI calls for TranscriptoAI.

Every prompt here is grounded in one session's transcript. The chat in
particular is constrained to it: if the transcript does not cover a question,
the assistant says so instead of answering from general knowledge, which is the
whole point of taking notes from a specific lecture or meeting.
"""

import json
import os
import re

import openai

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "whisper-1")

# Whisper's own limit is 25 MB per request.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024


def _client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env.")
    return openai.OpenAI(api_key=key)


def _chat(messages, **kwargs):
    return _client().chat.completions.create(
        model=CHAT_MODEL, messages=messages, **kwargs
    ).choices[0].message.content.strip()


# ------------------------------------------------------------- transcription

def transcribe(path, language=None, prompt=None):
    """Transcribe one audio file.

    `prompt` carries the tail of the previous clip so Whisper keeps names and
    terminology consistent across chunk boundaries.
    """
    params = {"model": TRANSCRIBE_MODEL}
    if language:
        params["language"] = language
    if prompt:
        params["prompt"] = prompt[-400:]

    with open(path, "rb") as fh:
        text = _client().audio.transcriptions.create(file=fh, **params).text.strip()
    return "" if _is_hallucination(text) else text


# Whisper invents text when handed silence or music. These are the phrases it
# reaches for; dropping them keeps junk out of summaries built from quiet audio.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "감사합니다",
    "ご視聴ありがとうございました", "字幕by", "please subscribe",
}


def _is_hallucination(text):
    """True when a clip produced nothing but Whisper's filler for silence."""
    stripped = text.lower().strip(" .!?,。")
    if not stripped:
        return True
    if stripped in _HALLUCINATIONS:
        return True
    # A clip that is one short phrase repeated is also filler, not speech.
    words = stripped.split()
    return len(words) > 6 and len(set(words)) <= 2


# ------------------------------------------------------------- summarisation

SUMMARY_SYSTEM = (
    "You summarise transcripts of lectures and meetings. You work only from the "
    "transcript you are given. Never add facts, examples, or definitions that do "
    "not appear in it. Transcripts come from speech recognition and contain "
    "errors and false starts; read through them, but do not invent content to "
    "fill gaps.\n\n"
    "LANGUAGE: write every word you output — the title, the headings, the "
    "bullets, the keywords — in the same language the transcript is spoken in. "
    "A Korean transcript gets a Korean summary, a Japanese one a Japanese "
    "summary. These instructions are in English; that is not the transcript's "
    "language and must not influence your output language."
)


def summarize(transcript, kind="lecture"):
    """Return {title, summary (markdown), keywords: [...]} for a transcript."""
    focus = (
        "Organise by topic. Bring out the main arguments, definitions and any "
        "worked examples the speaker walked through."
        if kind == "lecture"
        else "Organise by what was decided. Bring out decisions, open questions, "
        "and anything someone committed to doing."
    )

    raw = _chat(
        [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"{focus}\n\n"
                    "Return JSON with exactly these keys:\n"
                    '  "title": a short specific name for this session, 3-7 words\n'
                    '  "summary": markdown, 3-6 headed sections of bullet points\n'
                    '  "keywords": 5-10 terms actually used in the transcript that a '
                    "listener might want explained\n\n"
                    f"Transcript:\n{transcript}\n\n"
                    "Reminder: write the title, summary and keywords in the "
                    "transcript's own language, not in English."
                ),
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=1600,
    )

    data = json.loads(raw)
    return {
        "title": _as_text(data.get("title")) or "Untitled session",
        "summary": _as_markdown(data.get("summary")),
        "keywords": _as_keywords(data.get("keywords")),
    }


def _as_text(value):
    return value.strip() if isinstance(value, str) else ""


def _as_markdown(value, depth=2):
    """Flatten whatever shape the model returned into markdown.

    Asking for a markdown string usually works, but the model sometimes nests
    the sections as objects or arrays instead. Rather than fail the request,
    render those into the same markdown the UI already knows how to display.
    """
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(f"- {item.strip()}")
            else:
                parts.append(_as_markdown(item, depth))
        return "\n".join(p for p in parts if p)

    if isinstance(value, dict):
        # A {heading, points} shaped section, in any of its common spellings.
        heading = value.get("heading") or value.get("title") or value.get("section")
        body = value.get("points") or value.get("bullets") or value.get("content")
        if heading and body is not None:
            return f"{'#' * depth} {heading}\n{_as_markdown(body, depth + 1)}"

        parts = []
        for key, item in value.items():
            parts.append(f"{'#' * depth} {key}\n{_as_markdown(item, depth + 1)}")
        return "\n\n".join(parts)

    return ""


def _as_keywords(value):
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("term") or item.get("keyword") or item.get("name") or ""
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out[:10]


def explain_keyword(keyword, transcript):
    """Explain one term, anchored to how the speaker actually used it."""
    return _chat(
        [
            {
                "role": "system",
                "content": (
                    "You explain a term to someone who just heard it in a lecture or "
                    "meeting. Lead with how it was used in this transcript, then add "
                    "the background needed to make sense of it. Be clear that the "
                    "background is context you are adding. Keep it under 150 words. "
                    "Match the language of the transcript."
                ),
            },
            {
                "role": "user",
                "content": f'Term: "{keyword}"\n\nTranscript:\n{transcript}',
            },
        ],
        max_tokens=400,
    )


# ---------------------------------------------------------------------- chat

CHAT_SYSTEM = (
    "You answer questions about ONE transcript, given below. That transcript is "
    "your only source.\n\n"
    "Rules:\n"
    "1. If the transcript answers the question, answer from it and quote the "
    "relevant line.\n"
    "2. If it does not, say so plainly — for example 'That did not come up "
    "today.' Do not answer from general knowledge.\n"
    "3. If the transcript touches the topic only partly, answer what it covers "
    "and say what it leaves out.\n"
    "4. Speech recognition makes mistakes. If a passage looks garbled, say what "
    "you think was meant rather than treating it as fact.\n"
    "5. Match the language of the question.\n\n"
    "TRANSCRIPT:\n{transcript}"
)


def answer(question, transcript, history=None):
    if not transcript.strip():
        return "There is no transcript for this session yet, so there is nothing to answer from."

    messages = [{"role": "system", "content": CHAT_SYSTEM.format(transcript=transcript)}]
    for turn in (history or [])[-10:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})
    return _chat(messages, max_tokens=700)


# ------------------------------------------------------------------- helpers

def looks_like_url(text):
    return bool(re.match(r"^https?://\S+$", (text or "").strip()))
