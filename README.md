# TranscriptoAI

**Capture your ideas, transcribe real-time audio, and organize notes effortlessly.**

A study companion that records a lecture, transcribes it, summarizes it into
bullet points, pulls out the key terms, and lets you ask an AI follow-up
questions about what was just said — then keeps it all for review.

Built for *Designing for Generative AI* (Columbia University, Fall 2024).

---

## The idea

**Person** — students and professionals sitting through lectures and meetings.

**Problem** — you cannot listen carefully and take good notes at the same time.
Notes taken while listening are fragmentary; notes taken afterward are already
half-forgotten.

**Approach** — let the machine handle capture so the person can handle
understanding. Audio is transcribed as it comes in, then folded through a
summarize → extract keywords → ask questions loop.

**Metric** — how much of a session a student can reconstruct afterward, and how
long it takes them to do it.

## The flow

| Step | What happens |
|---|---|
| **Discover** | Pick a topic to study |
| **Record** | Mic audio is captured and transcribed in ~5s slices |
| **Edit Summary** | The transcript is condensed into structured bullets |
| **Keyword** | Key terms are extracted; click one for an AI explanation |
| **AI Chat** | Ask questions answered against your own transcript |
| **Review** | Revisit and revise saved summaries |

## Running it

Requires Python 3.9+ and `ffmpeg` (`brew install ffmpeg`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then add your real OpenAI key
python app.py
```

Open http://127.0.0.1:5001.

> Port 5001, not 5000 — macOS runs its AirPlay Receiver on 5000.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | Transcription, summaries, keywords, chat |
| `PORT` | `5001` | Server port |
| `TRANSCRIBE_BACKEND` | `api` | `api` for hosted Whisper, `local` for on-device |
| `WHISPER_MODEL` | `small` | Local model size, when `TRANSCRIBE_BACKEND=local` |
| `ENABLE_MIC` | *(off)* | Capture from the **server's** mic instead of the browser's |

Local transcription needs the extra dependencies in `requirements-local.txt`
(`torch` and friends, several GB). The hosted API is the default because it
needs no model download and no GPU.

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Landing page |
| `/home` | GET | Recording interface |
| `/transcribe` | POST | Accept an audio chunk, return its transcript |
| `/summary` | GET | Summarize the session, extract keywords |
| `/keyword_summary` | GET | Explain a single keyword |
| `/chat` | POST | Answer a question against the transcript |
| `/review` | GET | List saved summaries |
| `/review/<id>` | GET/POST | View or edit one summary |

## Notes on this codebase

Summaries live in memory and are lost on restart — a database was out of scope
for the course. The recorder slices audio into standalone ~5s clips rather than
holding an open stream, which trades a little latency for chunks the server can
decode independently.
