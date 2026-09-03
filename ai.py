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

import engines

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")

# Re-exported so callers keep importing one module for limits.
MAX_UPLOAD_BYTES = engines.MAX_UPLOAD_BYTES


def _client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY 가 설정되지 않았습니다. .env 에 넣은 뒤 "
            "check_keys.py 로 확인해보세요."
        )
    return openai.OpenAI(api_key=key)


def _chat(messages, **kwargs):
    return _client().chat.completions.create(
        model=CHAT_MODEL, messages=messages, **kwargs
    ).choices[0].message.content.strip()


# ------------------------------------------------------------- transcription

def transcribe(path, kind="lecture", diarize=False, prompt=None, language=None):
    """Transcribe a file and drop Whisper's filler-for-silence.

    Returns engines.transcribe's shape with hallucinated segments removed.
    """
    result = engines.transcribe(
        path, kind=kind, diarize=diarize, prompt=prompt, language=language
    )
    result["segments"] = [
        seg for seg in result["segments"] if not _is_hallucination(seg["text"])
    ]
    return result


def transcribe_text(path, prompt=None, **kwargs):
    """Convenience wrapper for callers that only want the words."""
    return engines.plain_text(transcribe(path, prompt=prompt, **kwargs))


# Whisper invents text when handed silence or music. These are the phrases it
# reaches for; dropping them keeps junk out of summaries built from quiet audio.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "감사합니다",
    "ご視聴ありがとうございました", "字幕by", "please subscribe",
}


def _is_hallucination(text):
    """True when a clip produced nothing but Whisper's filler for silence."""
    stripped = (text or "").lower().strip(" .!?,。")
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
        "and anything someone committed to doing. When the transcript is "
        "labelled with speakers, name who raised each point and who committed "
        "to each action."
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
    # Keywords sometimes arrive nested inside the summary object instead of at
    # the top level. Fall back to that before giving up on them.
    kw = data.get("keywords")
    if not kw and isinstance(data.get("summary"), dict):
        kw = data["summary"].get("keywords")
    return {
        "title": _as_text(data.get("title")) or "Untitled session",
        "summary": _as_markdown(data.get("summary")),
        "keywords": _as_keywords(kw),
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
            # The model sometimes folds the whole response object into the
            # summary field. Drop the sibling keys so they do not surface as
            # empty "keywords" / "title" sections in the notes.
            if key.lower() in {"keywords", "keyword", "title", "tags"}:
                continue
            rendered = _as_markdown(item, depth + 1)
            if rendered.strip():
                parts.append(f"{'#' * depth} {key}\n{rendered}")
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
                    "background is context you are adding. Keep it under 150 words.\n\n"
                    "LANGUAGE: write in the language the TRANSCRIPT is spoken in, not "
                    "the language the term happens to be written in. A Korean lecture "
                    "that uses the English term 'RAG' still gets a Korean explanation. "
                    "These instructions are in English; that must not affect your "
                    "output language."
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
    "2. If it does not, say so plainly in ONE short sentence, in the "
    "transcript's language, and stop. Do not answer from general knowledge, "
    "and do not add a translation or a second sentence in another language.\n"
    "3. If the transcript touches the topic only partly, answer what it covers "
    "and say what it leaves out.\n"
    "4. Speech recognition makes mistakes. If a passage looks garbled, say what "
    "you think was meant rather than treating it as fact.\n"
    "5. Answer in the language the TRANSCRIPT is spoken in, unless the question "
    "is clearly asked in a different language — then use the question's. A "
    "technical term written in English does not make the answer English. These "
    "instructions are in English; that must not affect your output language.\n\n"
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


# --------------------------------------------------------------- folder chat

FOLDER_SYSTEM = (
    "You answer questions about a FOLDER of related recordings — for example "
    "every lecture of one course. The material below is your only source.\n\n"
    "Rules:\n"
    "1. Answer only from the material. Quote the relevant line, and say which "
    "recording (by its title) it came from.\n"
    "2. If several recordings touch the topic, bring them together and note "
    "how the later one built on or changed the earlier one.\n"
    "3. If nothing in the material covers the question, say so in ONE short "
    "sentence in the material's language and stop. Do not add general knowledge.\n"
    "4. Speech recognition makes mistakes; read through obvious errors.\n"
    "5. Answer in the language the material is spoken in, unless the question "
    "is clearly in another language. These instructions being in English must "
    "not affect your output language.\n\n"
    "MATERIAL:\n{material}"
)

_STOP = set("the a an of to in is are was were and or for on at by with that this "
            "그 이 저 것 수 등 및 또 더 좀 는 은 이 가 을 를 에 의 로 와 과".split())


def _terms(text):
    return {t for t in re.findall(r"[\w가-힣]{2,}", (text or "").lower()) if t not in _STOP}


def build_folder_material(question, corpus, max_chars=40_000, top_segments=45):
    """Assemble what the folder chat gets to see.

    Every session's summary goes in (they are short and give the shape of the
    course). Then the segments most related to the question, from any session,
    ranked by shared terms — so a folder of twelve lectures still fits in the
    model's window and the answer can cite the exact line.
    """
    q_terms = _terms(question)
    parts, used = [], 0

    for s in corpus:
        block = f"### {s['title']} ({s['date']})\n{s['summary'].strip() or '(요약 없음)'}\n"
        parts.append(block)
        used += len(block)

    scored = []
    for s in corpus:
        for text in s["segments"]:
            overlap = len(q_terms & _terms(text))
            if overlap:
                scored.append((overlap, s["title"], text))
    scored.sort(key=lambda x: -x[0])

    if scored:
        parts.append("\n### 관련 구절\n")
        for _, title, text in scored[:top_segments]:
            line = f"[{title}] {text}\n"
            if used + len(line) > max_chars:
                break
            parts.append(line)
            used += len(line)

    return "".join(parts)


def answer_folder(question, corpus, history=None):
    if not any(s["segments"] for s in corpus):
        return "이 폴더에는 아직 받아적은 내용이 없어서 답할 근거가 없습니다."
    material = build_folder_material(question, corpus)
    messages = [{"role": "system", "content": FOLDER_SYSTEM.format(material=material)}]
    for turn in (history or [])[-10:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})
    return _chat(messages, max_tokens=900)


# ------------------------------------------------------------------- helpers

def looks_like_url(text):
    return bool(re.match(r"^https?://\S+$", (text or "").strip()))
