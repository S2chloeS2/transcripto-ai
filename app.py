"""TranscriptoAI — capture a lecture or meeting, and turn it into notes.

Three ways in:
  system — audio playing on this computer (Zoom, YouTube, any tab)
  mic    — the room, via the microphone
  link   — paste a URL and let the server fetch it

Everything is scoped to a session, and sessions are stored in SQLite so the
review screen still has something in it tomorrow.
"""

import json
import os
import shutil
import tempfile
import threading
import traceback

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, url_for

load_dotenv()

import ai
import db
import media

app = Flask(__name__, static_url_path="/static")
db.init()

# Progress for link imports, keyed by session id. In-process is fine: a failed
# import is cheap to retry, and the transcript itself is already in SQLite.
_jobs = {}
_jobs_lock = threading.Lock()


def set_job(session_id, **fields):
    with _jobs_lock:
        _jobs.setdefault(session_id, {}).update(fields)


def get_job(session_id):
    with _jobs_lock:
        return dict(_jobs.get(session_id, {}))


def fail(message, code=400):
    return jsonify({"error": message}), code


def _keywords_of(session):
    try:
        return json.loads(session.get("keywords") or "[]")
    except (ValueError, TypeError):
        return []


# --------------------------------------------------------------------- pages

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/new")
def new_session():
    return render_template("new.html")


@app.route("/session/<int:session_id>")
def session_view(session_id):
    session = db.get_session(session_id)
    if not session:
        return render_template("missing.html", session_id=session_id), 404
    return render_template(
        "session.html",
        session=session,
        segments=db.get_segments(session_id),
        messages=db.get_messages(session_id),
        keywords=_keywords_of(session),
    )


@app.route("/review")
def review():
    return render_template("review.html", sessions=db.list_sessions())


# ------------------------------------------------------------------ sessions

@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    body = request.get_json(silent=True) or {}
    source = body.get("source", "mic")
    if source not in {"system", "mic", "both", "link"}:
        return fail("Unknown source. Use system, mic, both, or link.")

    kind = body.get("kind", "lecture")
    title = (body.get("title") or "").strip() or ("Meeting" if kind == "meeting" else "Lecture")
    session_id = db.create_session(title=title, kind=kind, source=source)
    return jsonify({"id": session_id, "url": url_for("session_view", session_id=session_id)})


@app.route("/api/sessions/<int:session_id>", methods=["PATCH", "DELETE"])
def api_modify_session(session_id):
    if not db.get_session(session_id):
        return fail("No such session.", 404)

    if request.method == "DELETE":
        db.delete_session(session_id)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    db.update_session(
        session_id,
        **{k: v for k, v in body.items() if k in {"title", "summary", "keywords", "kind"}},
    )
    return jsonify({"ok": True})


# ------------------------------------------------------------- transcription

@app.route("/api/sessions/<int:session_id>/transcribe", methods=["POST"])
def api_transcribe(session_id):
    """Accept one audio clip from the browser and return its text."""
    if not db.get_session(session_id):
        return fail("No such session.", 404)

    clip = request.files.get("file")
    if not clip:
        return fail("No audio was attached.")

    data = clip.read()
    if not data:
        return fail("The audio clip was empty.")
    if len(data) > ai.MAX_UPLOAD_BYTES:
        return fail("That clip is too large. Keep clips under 24 MB.")

    suffix = os.path.splitext(clip.filename or "")[1] or ".webm"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        # Feed back what we have so terminology stays consistent across clips.
        text = ai.transcribe(tmp.name, prompt=db.get_transcript(session_id))
    except Exception as exc:
        app.logger.error("transcribe failed: %s", traceback.format_exc())
        return fail(str(exc), 502)
    finally:
        os.unlink(tmp.name)

    if not text:
        return jsonify({"text": "", "note": "no speech detected"})

    db.add_segment(session_id, text)
    return jsonify({"text": text})


# --------------------------------------------------------------- link import

@app.route("/api/import", methods=["POST"])
def api_import():
    """Start fetching a URL in the background; returns a session to watch."""
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not ai.looks_like_url(url):
        return fail("That does not look like a link. It should start with http.")

    try:
        info = media.probe(url)
    except media.MediaError as exc:
        return fail(str(exc))

    session_id = db.create_session(
        title=info["title"], kind=body.get("kind", "lecture"), source="link", source_url=url
    )
    set_job(session_id, state="starting", done=0, total=0, message="Preparing…")

    threading.Thread(target=_run_import, args=(session_id, url), daemon=True).start()

    return jsonify(
        {
            "id": session_id,
            "title": info["title"],
            "duration": info["duration"],
            "url": url_for("session_view", session_id=session_id),
        }
    )


@app.route("/api/import/file", methods=["POST"])
def api_import_file():
    """Accept a recording — a Zoom/Teams local recording, a voice memo, anything
    ffmpeg can decode — and run it through the same pipeline as a link."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return fail("No file was attached.")

    workdir = media.workspace()
    suffix = os.path.splitext(upload.filename)[1] or ".m4a"
    path = os.path.join(workdir, f"upload{suffix}")
    upload.save(path)

    if os.path.getsize(path) == 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return fail("That file is empty.")

    title = os.path.splitext(os.path.basename(upload.filename))[0][:120]
    session_id = db.create_session(
        title=title, kind=request.form.get("kind", "meeting"), source="link"
    )
    set_job(session_id, state="starting", done=0, total=0, message="Preparing…")

    threading.Thread(
        target=_run_pipeline, args=(session_id, path, workdir), daemon=True
    ).start()

    return jsonify(
        {"id": session_id, "title": title,
         "url": url_for("session_view", session_id=session_id)}
    )


def _run_import(session_id, url):
    workdir = media.workspace()
    try:
        set_job(session_id, state="downloading", message="Downloading audio…")
        path = media.download_audio(url, workdir)
    except Exception as exc:
        app.logger.error("download failed: %s", traceback.format_exc())
        set_job(session_id, state="error", message=str(exc))
        shutil.rmtree(workdir, ignore_errors=True)
        return
    _run_pipeline(session_id, path, workdir)


def _run_pipeline(session_id, path, workdir):
    """Split an audio file, transcribe every chunk, then summarise."""
    try:
        set_job(session_id, state="splitting", message="Splitting into chunks…")
        chunks = media.split(path, workdir)
        set_job(session_id, total=len(chunks), state="transcribing")

        for index, chunk in enumerate(chunks, start=1):
            set_job(session_id, done=index - 1,
                    message=f"Transcribing part {index} of {len(chunks)}…")
            text = ai.transcribe(chunk, prompt=db.get_transcript(session_id))
            if text:
                db.add_segment(session_id, text)

        set_job(session_id, done=len(chunks), state="summarizing",
                message="Writing the summary…")
        _build_summary(session_id)
        set_job(session_id, state="done", message="Ready")

    except Exception as exc:
        app.logger.error("pipeline failed: %s", traceback.format_exc())
        set_job(session_id, state="error", message=str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.route("/api/sessions/<int:session_id>/progress")
def api_progress(session_id):
    job = get_job(session_id)
    if job:
        return jsonify(job)
    session = db.get_session(session_id)
    if not session:
        return fail("No such session.", 404)
    return jsonify({"state": "done" if session.get("summary") else "idle"})


# ------------------------------------------------------------------- summary

def _build_summary(session_id):
    session = db.get_session(session_id)
    transcript = db.get_transcript(session_id)
    if not transcript:
        raise ValueError("There is nothing transcribed in this session yet.")

    result = ai.summarize(transcript, kind=session.get("kind", "lecture"))
    fields = {"summary": result["summary"], "keywords": result["keywords"]}
    # Only adopt the generated title if the user has not set one of their own.
    if session["title"] in {"Lecture", "Meeting", "Untitled session"}:
        fields["title"] = result["title"]
    db.update_session(session_id, **fields)
    return result


@app.route("/api/sessions/<int:session_id>/summary", methods=["POST"])
def api_summary(session_id):
    if not db.get_session(session_id):
        return fail("No such session.", 404)
    try:
        _build_summary(session_id)
    except ValueError as exc:
        return fail(str(exc))
    except Exception as exc:
        app.logger.error("summary failed: %s", traceback.format_exc())
        return fail(str(exc), 502)

    session = db.get_session(session_id)
    return jsonify({
        "summary": session["summary"],
        "keywords": _keywords_of(session),
        "title": session["title"],
    })


@app.route("/api/sessions/<int:session_id>/keyword")
def api_keyword(session_id):
    keyword = (request.args.get("q") or "").strip()
    if not keyword:
        return fail("No keyword given.")
    transcript = db.get_transcript(session_id)
    if not transcript:
        return fail("There is nothing transcribed in this session yet.")
    try:
        return jsonify({
            "keyword": keyword,
            "explanation": ai.explain_keyword(keyword, transcript),
        })
    except Exception as exc:
        app.logger.error("keyword failed: %s", traceback.format_exc())
        return fail(str(exc), 502)


# ---------------------------------------------------------------------- chat

@app.route("/api/sessions/<int:session_id>/chat", methods=["POST"])
def api_chat(session_id):
    if not db.get_session(session_id):
        return fail("No such session.", 404)

    question = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not question:
        return fail("Type a question first.")

    transcript = db.get_transcript(session_id)
    try:
        reply = ai.answer(question, transcript, history=db.get_messages(session_id))
    except Exception as exc:
        app.logger.error("chat failed: %s", traceback.format_exc())
        return fail(str(exc), 502)

    db.add_message(session_id, "user", question)
    db.add_message(session_id, "assistant", reply)
    return jsonify({"reply": reply})


@app.route("/api/sessions/<int:session_id>/transcript")
def api_transcript(session_id):
    if not db.get_session(session_id):
        return fail("No such session.", 404)
    return jsonify({"segments": db.get_segments(session_id)})


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "No such endpoint."}), 404
    return render_template("missing.html", session_id=None), 404


if __name__ == "__main__":
    # 5001 because macOS runs its AirPlay Receiver on 5000.
    app.run(debug=True, port=int(os.getenv("PORT", 5001)))
