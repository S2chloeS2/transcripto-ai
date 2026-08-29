"""Transcription engines.

Three back ends, picked per session because lectures and meetings want
different things:

    Groq        cheapest by far, no speaker labels — right for a lecture,
                where one person talks
    AssemblyAI  speaker labels included for almost nothing, and still less
                than half the price of whisper-1 — right for a meeting
    OpenAI      the fallback, and what runs when no other key is configured

Every engine returns the same shape, so the rest of the app never knows which
one ran:

    [{"text": str, "speaker": str | None, "start_ms": int, "end_ms": int}, …]
"""

import os
import time

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "whisper-large-v3-turbo")
ASSEMBLY_URL = "https://api.assemblyai.com/v2"
OPENAI_MODEL = os.getenv("TRANSCRIBE_MODEL", "whisper-1")

# Whisper's own cap. AssemblyAI accepts far more, but we stay under one number.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024


class TranscribeError(Exception):
    """The audio could not be transcribed."""


# ------------------------------------------------------------------ selection

def available():
    """Which engines have a key configured."""
    return {
        "groq": bool(os.getenv("GROQ_API_KEY")),
        "assemblyai": bool(os.getenv("ASSEMBLYAI_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
    }


def pick(kind="lecture", diarize=False):
    """Choose an engine for this session, falling back to whatever is present.

    A meeting that wants speaker labels needs AssemblyAI; nothing else here
    provides them. Everything else prefers Groq on cost.
    """
    have = available()

    if diarize:
        if have["assemblyai"]:
            return "assemblyai"
        # No diarization available — the caller is told via the result.
    if kind == "meeting" and have["assemblyai"]:
        return "assemblyai"
    if have["groq"]:
        return "groq"
    if have["openai"]:
        return "openai"
    raise TranscribeError(
        "전사 키가 하나도 없습니다. .env 에 OPENAI_API_KEY 를 넣어주세요."
    )


def transcribe(path, kind="lecture", diarize=False, prompt=None, language=None):
    """Transcribe one audio file with whichever engine suits this session."""
    engine = pick(kind=kind, diarize=diarize)
    if engine == "assemblyai":
        segments = _assemblyai(path, diarize=diarize, language=language)
    elif engine == "groq":
        segments = _groq(path, prompt=prompt, language=language)
    else:
        segments = _openai(path, prompt=prompt, language=language)
    return {
        "engine": engine,
        "diarized": engine == "assemblyai" and diarize,
        "segments": segments,
    }


def plain_text(result):
    return " ".join(s["text"] for s in result["segments"]).strip()


# --------------------------------------------------------------------- Groq

def _groq(path, prompt=None, language=None):
    data = {"model": GROQ_MODEL, "response_format": "json"}
    if prompt:
        data["prompt"] = prompt[-400:]
    if language:
        data["language"] = language

    with open(path, "rb") as fh:
        response = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            files={"file": (os.path.basename(path), fh, "application/octet-stream")},
            data=data,
            timeout=180,
        )
    if response.status_code != 200:
        raise TranscribeError(f"Groq 전사 실패 ({response.status_code}): {response.text[:200]}")

    text = (response.json().get("text") or "").strip()
    return [{"text": text, "speaker": None, "start_ms": None, "end_ms": None}] if text else []


# --------------------------------------------------------------- AssemblyAI

def _assemblyai(path, diarize=True, language=None):
    key = os.environ["ASSEMBLYAI_API_KEY"]
    headers = {"authorization": key}

    with open(path, "rb") as fh:
        upload = httpx.post(
            f"{ASSEMBLY_URL}/upload", headers=headers, content=fh.read(), timeout=300
        )
    if upload.status_code != 200:
        raise TranscribeError(f"AssemblyAI 업로드 실패 ({upload.status_code})")
    audio_url = upload.json()["upload_url"]

    body = {"audio_url": audio_url, "speaker_labels": bool(diarize)}
    if language:
        body["language_code"] = language
    else:
        body["language_detection"] = True

    created = httpx.post(f"{ASSEMBLY_URL}/transcript", json=body, headers=headers, timeout=60)
    if created.status_code not in (200, 201):
        raise TranscribeError(
            f"AssemblyAI 전사 요청 실패 ({created.status_code}): {created.text[:200]}"
        )
    transcript_id = created.json()["id"]

    # Poll. AssemblyAI runs well ahead of real time, but long files still take
    # a while, so this ceiling is generous.
    deadline = time.time() + 3600
    while time.time() < deadline:
        poll = httpx.get(f"{ASSEMBLY_URL}/transcript/{transcript_id}", headers=headers, timeout=60)
        result = poll.json()
        status = result.get("status")
        if status == "completed":
            return _assembly_segments(result, diarize)
        if status == "error":
            raise TranscribeError(f"AssemblyAI 전사 오류: {result.get('error')}")
        time.sleep(3)

    raise TranscribeError("AssemblyAI 전사가 시간 안에 끝나지 않았습니다.")


def _assembly_segments(result, diarize):
    if diarize and result.get("utterances"):
        return [
            {
                "text": (u.get("text") or "").strip(),
                "speaker": u.get("speaker"),
                "start_ms": u.get("start"),
                "end_ms": u.get("end"),
            }
            for u in result["utterances"]
            if (u.get("text") or "").strip()
        ]

    text = (result.get("text") or "").strip()
    return [{"text": text, "speaker": None, "start_ms": None, "end_ms": None}] if text else []


# ------------------------------------------------------------------- OpenAI

def _openai(path, prompt=None, language=None):
    import openai

    params = {"model": OPENAI_MODEL}
    if prompt:
        params["prompt"] = prompt[-400:]
    if language:
        params["language"] = language

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    with open(path, "rb") as fh:
        text = client.audio.transcriptions.create(file=fh, **params).text.strip()
    return [{"text": text, "speaker": None, "start_ms": None, "end_ms": None}] if text else []
