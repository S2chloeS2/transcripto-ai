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
import auth
import db
import engines
import media
import plans

app = Flask(__name__, static_url_path="/static")

# Signs the session cookie. A fixed value in .env keeps people logged in across
# restarts; without one we generate a throwaway and everyone is signed out.
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

# Reject oversized bodies before Werkzeug reads them into memory or onto disk.
# Long recordings arrive as uploads, so this is generous — but not unbounded.
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024

db.init()
auth.init_app(app)


@app.context_processor
def inject_user():
    """Every template can show who is signed in and how much time is left."""
    user = auth.current_user()
    return {
        "current_user": user,
        "allowance": plans.allowance(user["id"], user) if user else None,
    }


def current_folders():
    user = auth.current_user()
    return db.list_folders(user["id"]) if user else []


def owned_folder(folder_id):
    user = auth.current_user()
    return db.get_folder(folder_id, user["id"]) if user else None

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


def owned(session_id):
    """The session, but only if it belongs to whoever is asking.

    Returns None for someone else's session as well as for one that does not
    exist, so a stranger cannot tell the two apart.
    """
    user = auth.current_user()
    if not user:
        return None
    return db.get_session(session_id, user_id=user["id"])


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
@auth.login_required
def new_session():
    return render_template("new.html")


@app.route("/session/<int:session_id>")
@auth.login_required
def session_view(session_id):
    session = db.get_session(session_id, user_id=auth.current_user()["id"])
    if not session:
        return render_template("missing.html", session_id=session_id), 404
    speakers = db.speaker_stats(session_id)
    return render_template(
        "session.html",
        session=session,
        segments=db.get_segments(session_id),
        messages=db.get_messages(session_id),
        keywords=_keywords_of(session),
        speakers=speakers,
        speaker_order={sp["label"]: i for i, sp in enumerate(speakers)},
        speaker_names=db.get_speaker_names(session_id),
        keyword_notes=db.get_keyword_notes(session_id),
        folders=current_folders(),
        engines=engines.available(),
    )


@app.route("/review")
@auth.login_required
def review():
    user_id = auth.current_user()["id"]
    raw = request.args.get("folder")
    folder_filter = None
    if raw == "none":
        folder_filter = "none"
    elif raw and raw.isdigit() and db.get_folder(int(raw), user_id):
        folder_filter = int(raw)

    sessions = db.list_sessions(user_id, folder_id=folder_filter)
    for s in sessions:
        s["keyword_list"] = _keywords_of(s)
        s["excerpt"] = _excerpt(s.get("summary"))
    return render_template(
        "review.html", sessions=sessions, folders=current_folders(),
        folder_filter=folder_filter,
    )


@app.route("/folder/<int:folder_id>")
@auth.login_required
def folder_view(folder_id):
    folder = owned_folder(folder_id)
    if not folder:
        return render_template("missing.html", session_id=None), 404
    sessions = db.list_sessions(auth.current_user()["id"], folder_id=folder_id)
    for s in sessions:
        s["keyword_list"] = _keywords_of(s)
        s["excerpt"] = _excerpt(s.get("summary"))
    return render_template(
        "folder.html", folder=folder, sessions=sessions,
        messages=db.get_folder_messages(folder_id),
    )


@app.route("/account")
@auth.login_required
def account():
    return render_template(
        "account.html", plans=plans.PLANS, order=plans.ORDER,
        can_switch=auth.dev_login_allowed(),
    )


def _excerpt(summary, limit=140):
    """First line of real content from a markdown summary, for the review list."""
    for line in (summary or "").splitlines():
        text = line.strip().lstrip("#-* ").strip()
        if text:
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


# ------------------------------------------------------------------ sessions

@app.route("/api/sessions", methods=["POST"])
@auth.login_required
def api_create_session():
    body = request.get_json(silent=True) or {}
    source = body.get("source", "mic")
    if source not in {"system", "mic", "both", "link"}:
        return fail("Unknown source. Use system, mic, both, or link.")

    kind = body.get("kind", "lecture")
    title = (body.get("title") or "").strip() or ("Meeting" if kind == "meeting" else "Lecture")
    session_id = db.create_session(
        title=title, kind=kind, source=source, user_id=auth.current_user()["id"]
    )
    return jsonify({"id": session_id, "url": url_for("session_view", session_id=session_id)})


@app.route("/api/sessions/<int:session_id>", methods=["PATCH", "DELETE"])
@auth.login_required
def api_modify_session(session_id):
    if not owned(session_id):
        return fail("No such session.", 404)

    if request.method == "DELETE":
        db.delete_session(session_id)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    fields = {k: v for k, v in body.items() if k in {"title", "summary", "keywords", "kind"}}

    # Filing into a folder: only into one of the caller's own, or out of any.
    if "folder_id" in body:
        target = body["folder_id"]
        if target in (None, "", "none"):
            fields["folder_id"] = None
        elif str(target).isdigit() and owned_folder(int(target)):
            fields["folder_id"] = int(target)
        else:
            return fail("그 폴더를 찾을 수 없습니다.", 404)

    db.update_session(session_id, **fields)
    return jsonify({"ok": True})


# ------------------------------------------------------------------- folders

@app.route("/api/folders", methods=["POST"])
@auth.login_required
def api_create_folder():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()[:80]
    if not name:
        return fail("폴더 이름을 입력해주세요.")
    folder_id = db.create_folder(auth.current_user()["id"], name)
    return jsonify({"id": folder_id, "name": name, "url": url_for("folder_view", folder_id=folder_id)})


@app.route("/api/folders/<int:folder_id>", methods=["PATCH", "DELETE"])
@auth.login_required
def api_modify_folder(folder_id):
    if not owned_folder(folder_id):
        return fail("폴더를 찾을 수 없습니다.", 404)
    if request.method == "DELETE":
        db.delete_folder(folder_id)
        return jsonify({"ok": True})
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()[:80]
    if not name:
        return fail("폴더 이름을 입력해주세요.")
    db.rename_folder(folder_id, name)
    return jsonify({"ok": True})


@app.route("/api/folders/<int:folder_id>/chat", methods=["POST"])
@auth.login_required
def api_folder_chat(folder_id):
    """Ask across every recording in the folder — a whole course at once."""
    if not owned_folder(folder_id):
        return fail("폴더를 찾을 수 없습니다.", 404)
    question = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not question:
        return fail("질문을 입력해주세요.")
    try:
        reply = ai.answer_folder(
            question, db.folder_corpus(folder_id), history=db.get_folder_messages(folder_id)
        )
    except Exception as exc:
        app.logger.error("folder chat failed: %s", traceback.format_exc())
        return fail(str(exc), 502)
    db.add_folder_message(folder_id, "user", question)
    db.add_folder_message(folder_id, "assistant", reply)
    return jsonify({"reply": reply})


# ------------------------------------------------------------------- account

@app.route("/api/account/plan", methods=["POST"])
@auth.login_required
def api_set_plan():
    """Switch plans by hand. Only while payment is not wired up, and only on a
    local dev build — a deployed server refuses this outright."""
    if not auth.dev_login_allowed():
        return fail("결제 연동 전에는 플랜을 직접 바꿀 수 없습니다.", 403)
    plan = ((request.get_json(silent=True) or {}).get("plan") or "").strip()
    if plan not in plans.PLANS:
        return fail("없는 플랜입니다.")
    db.set_plan(auth.current_user()["id"], plan)
    return jsonify({"ok": True, "plan": plan})


# ------------------------------------------------------------- transcription

@app.route("/api/sessions/<int:session_id>/transcribe", methods=["POST"])
@auth.login_required
def api_transcribe(session_id):
    """Accept one audio clip from the browser and return its text."""
    if not owned(session_id):
        return fail("No such session.", 404)

    clip = request.files.get("file")
    if not clip:
        return fail("No audio was attached.")

    data = clip.read()
    if not data:
        return fail("The audio clip was empty.")
    if len(data) > ai.MAX_UPLOAD_BYTES:
        return fail("That clip is too large. Keep clips under 24 MB.")

    user = auth.current_user()
    suffix = os.path.splitext(clip.filename or "")[1] or ".webm"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()

        # Meter before spending: a clip is ~6 s, but measure it rather than assume.
        seconds = media.duration_of(tmp.name) or 6.0
        ok, message, allowance = plans.check(user["id"], seconds, user)
        if not ok:
            return jsonify({"error": message, "quota": True,
                            "remaining_s": allowance["remaining_s"]}), 402

        # Live clips are transcribed one at a time for fast feedback, so no
        # diarization here: speaker A in one clip is not speaker A in the next.
        # The session can be re-run with diarization once recording stops.
        session = db.get_session(session_id)
        text = ai.transcribe_text(
            tmp.name,
            kind=session.get("kind", "lecture"),
            prompt=db.get_transcript(session_id),
        )
        db.log_usage(user["id"], session_id, seconds)
    except Exception as exc:
        app.logger.error("transcribe failed: %s", traceback.format_exc())
        return fail(str(exc), 502)
    finally:
        os.unlink(tmp.name)

    remaining = plans.allowance(user["id"], user)["remaining_s"]
    if not text:
        return jsonify({"text": "", "note": "no speech detected", "remaining_s": remaining})

    db.add_segment(session_id, text)
    return jsonify({"text": text, "remaining_s": remaining})


# --------------------------------------------------------------- link import

@app.route("/api/import", methods=["POST"])
@auth.login_required
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

    user = auth.current_user()
    ok, message, allowance = plans.check(user["id"], info["duration"], user)
    if not ok:
        return jsonify({"error": message, "quota": True,
                        "remaining_s": allowance["remaining_s"]}), 402

    session_id = db.create_session(
        title=info["title"], kind=body.get("kind", "lecture"), source="link",
        source_url=url, user_id=user["id"],
    )
    set_job(session_id, state="starting", done=0, total=0, message="Preparing…")

    # A TED link resolves to its YouTube mirror; download from there.
    threading.Thread(
        target=_run_import, args=(session_id, info["webpage_url"]), daemon=True
    ).start()

    return jsonify(
        {
            "id": session_id,
            "title": info["title"],
            "duration": info["duration"],
            "via_youtube": info.get("via_youtube", False),
            "url": url_for("session_view", session_id=session_id),
        }
    )


@app.route("/api/import/file", methods=["POST"])
@auth.login_required
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

    seconds = media.duration_of(path)
    if seconds <= 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return fail("오디오 길이를 읽을 수 없습니다. 오디오·영상 파일이 맞는지 확인해주세요.")

    user = auth.current_user()
    ok, message, allowance = plans.check(user["id"], seconds, user)
    if not ok:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"error": message, "quota": True,
                        "remaining_s": allowance["remaining_s"]}), 402

    title = os.path.splitext(os.path.basename(upload.filename))[0][:120]
    session_id = db.create_session(
        title=title, kind=request.form.get("kind", "meeting"), source="link",
        user_id=auth.current_user()["id"],
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

        session = db.get_session(session_id)
        kind = session.get("kind", "lecture")
        # The request was already checked against the plan; now record what it
        # actually cost, from the file itself.
        db.log_usage(session.get("user_id"), session_id, media.duration_of(path))
        # Meetings want to know who spoke; lectures are one voice, so we skip
        # diarization there and use the cheaper engine.
        diarize = kind == "meeting"
        offset_ms = 0

        for index, chunk in enumerate(chunks, start=1):
            set_job(session_id, done=index - 1,
                    message=f"Transcribing part {index} of {len(chunks)}…")
            result = ai.transcribe(
                chunk, kind=kind, diarize=diarize, prompt=db.get_transcript(session_id)
            )
            for seg in result["segments"]:
                db.add_segment(
                    session_id,
                    seg["text"],
                    speaker=seg.get("speaker"),
                    start_ms=(seg["start_ms"] + offset_ms) if seg.get("start_ms") is not None else None,
                    end_ms=(seg["end_ms"] + offset_ms) if seg.get("end_ms") is not None else None,
                )
            # Chunks are cut at a fixed length, so each one starts that much
            # further into the recording.
            offset_ms += media.CHUNK_SECONDS * 1000

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
@auth.login_required
def api_progress(session_id):
    # Ownership first: progress leaks a session's title and state otherwise.
    session = owned(session_id)
    if not session:
        return fail("기록을 찾을 수 없습니다.", 404)
    job = get_job(session_id)
    if job:
        return jsonify(job)
    return jsonify({"state": "done" if session.get("summary") else "idle"})


# ------------------------------------------------------------------- summary

def _build_summary(session_id):
    session = db.get_session(session_id)
    # Meetings summarise better when the model knows who said what.
    transcript = db.get_transcript(session_id, with_speakers=True)
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
@auth.login_required
def api_summary(session_id):
    if not owned(session_id):
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
@auth.login_required
def api_keyword(session_id):
    if not owned(session_id):
        return fail("기록을 찾을 수 없습니다.", 404)
    keyword = (request.args.get("q") or "").strip()
    if not keyword:
        return fail("키워드를 지정해주세요.")
    # Already looked up once? Serve the saved note — no second model call.
    cached = db.get_keyword_notes(session_id).get(keyword)
    if cached:
        return jsonify({"keyword": keyword, "explanation": cached, "cached": True})

    transcript = db.get_transcript(session_id)
    if not transcript:
        return fail("아직 받아적은 내용이 없습니다.")
    try:
        explanation = ai.explain_keyword(keyword, transcript)
    except Exception as exc:
        app.logger.error("keyword failed: %s", traceback.format_exc())
        return fail(str(exc), 502)

    db.save_keyword_note(session_id, keyword, explanation)
    return jsonify({"keyword": keyword, "explanation": explanation, "cached": False})


# ---------------------------------------------------------------------- chat

@app.route("/api/sessions/<int:session_id>/chat", methods=["POST"])
@auth.login_required
def api_chat(session_id):
    if not owned(session_id):
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


@app.route("/api/sessions/<int:session_id>/speakers", methods=["GET", "PATCH"])
@auth.login_required
def api_speakers(session_id):
    """Read talk-time per speaker, or rename one."""
    if not owned(session_id):
        return fail("No such session.", 404)

    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        label = (body.get("label") or "").strip()
        name = (body.get("name") or "").strip()
        if not label:
            return fail("어떤 화자인지 지정해주세요.")
        db.set_speaker_name(session_id, label, name[:60])
        return jsonify({"ok": True})

    return jsonify({"speakers": db.speaker_stats(session_id)})


@app.route("/api/sessions/<int:session_id>/transcript")
@auth.login_required
def api_transcript(session_id):
    if not owned(session_id):
        return fail("No such session.", 404)
    return jsonify({"segments": db.get_segments(session_id)})


@app.errorhandler(413)
def too_large(_):
    limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return fail(f"파일이 너무 큽니다. {limit}MB 이하만 올릴 수 있습니다.", 413)


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "No such endpoint."}), 404
    return render_template("missing.html", session_id=None), 404


if __name__ == "__main__":
    # 5001 because macOS runs its AirPlay Receiver on 5000.
    app.run(debug=True, port=int(os.getenv("PORT", 5001)))
