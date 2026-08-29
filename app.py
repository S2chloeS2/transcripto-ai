from flask import Flask, request, jsonify, render_template
import openai
import os
import time
from dotenv import load_dotenv
import subprocess
import tempfile
from datetime import datetime, timedelta
import threading

# Heavy, optional dependencies for local real-time transcription.
# The web app boots without them; only the mic-capture thread needs them.
try:
    import numpy as np
    import speech_recognition as sr
    import whisper
    import torch
    STT_AVAILABLE = True
    STT_IMPORT_ERROR = None
except ImportError as _e:  # pragma: no cover
    np = sr = whisper = torch = None
    STT_AVAILABLE = False
    STT_IMPORT_ERROR = _e
from queue import Queue
from time import sleep
import argparse
from sys import platform

app = Flask(__name__, static_url_path='/static')

# Load environment variables
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

# Initialize global variables
saved_files = []  # List of saved audio files
saved_summaries = []  # List to store all generated summaries
transcriptions = []  # Store all transcription text

# Initialize queue for audio data
data_queue = Queue()

# Initialize lock for thread-safe operations
transcriptions_lock = threading.Lock()


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], check=True)
        subprocess.run(["ffprobe", "-version"], check=True)
        print("ffmpeg and ffprobe are properly installed.")
    except subprocess.CalledProcessError:
        print("ffmpeg or ffprobe is not properly installed.")
    except FileNotFoundError:
        print("ffmpeg or ffprobe could not be found in the system path.")


check_ffmpeg()

# Environment Setup
os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin"

# Whisper is loaded lazily so the web server starts instantly.
# Override the size with WHISPER_MODEL=tiny|base|small|medium|large
MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    """Load the Whisper model on first use, then reuse it."""
    global whisper_model
    if whisper_model is None:
        with _whisper_lock:
            if whisper_model is None:
                print(f"Loading Whisper model '{MODEL_NAME}'...")
                whisper_model = whisper.load_model(MODEL_NAME)
                print(f"Whisper model '{MODEL_NAME}' loaded successfully.")
    return whisper_model


def real_time_transcription():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="medium", help="Model to use",
                        choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--non_english", action='store_true',
                        help="Don't use the english model.")
    parser.add_argument("--energy_threshold", default=1000,
                        help="Energy level for mic to detect.", type=int)
    parser.add_argument("--record_timeout", default=2,
                        help="How real time the recording is in seconds.", type=float)
    parser.add_argument("--phrase_timeout", default=3,
                        help="How much empty space between recordings before we "
                             "consider it a new line in the transcription.", type=float)
    parser.add_argument("--default_microphone", default='pulse',
                        help="Default microphone name for SpeechRecognition. "
                             "Run this with 'list' to view available Microphones.", type=str)
    args = parser.parse_args(args=[])

    # The last time a recording was retrieved from the queue.
    phrase_time = None

    # We use SpeechRecognizer to record our audio because it has a nice feature where it can detect when speech ends.
    recorder = sr.Recognizer()
    recorder.energy_threshold = args.energy_threshold
    # Definitely do this, dynamic energy compensation lowers the energy threshold dramatically to a point where the SpeechRecognizer never stops recording.
    recorder.dynamic_energy_threshold = False

    # Important for linux users.
    # Prevents permanent application hang and crash by using the wrong Microphone
    if 'linux' in platform:
        mic_name = args.default_microphone
        if not mic_name or mic_name == 'list':
            print("Available microphone devices are: ")
            for index, name in enumerate(sr.Microphone.list_microphone_names()):
                print(f"Microphone with name \"{name}\" found")
            return
        else:
            for index, name in enumerate(sr.Microphone.list_microphone_names()):
                if mic_name in name:
                    source = sr.Microphone(sample_rate=16000, device_index=index)
                    break
    else:
        source = sr.Microphone(sample_rate=16000)

    record_timeout = args.record_timeout
    phrase_timeout = args.phrase_timeout

    transcription = ['']

    with source:
        recorder.adjust_for_ambient_noise(source)

    def record_callback(_, audio: sr.AudioData) -> None:
        """
        Threaded callback function to receive audio data when recordings finish.
        audio: An AudioData containing the recorded bytes.
        """
        # Grab the raw bytes and push it into the thread safe queue.
        data = audio.get_raw_data()
        data_queue.put(data)

    # Create a background thread that will pass us raw audio bytes.
    # We could do this manually but SpeechRecognizer provides a nice helper.
    recorder.listen_in_background(source, record_callback, phrase_time_limit=record_timeout)

    # Cue the user that we're ready to go.
    print("Real-time transcription thread started.\n")

    while True:
        try:
            now = datetime.utcnow()
            # Pull raw recorded audio from the queue.
            if not data_queue.empty():
                phrase_complete = False
                # If enough time has passed between recordings, consider the phrase complete.
                if phrase_time and now - phrase_time > timedelta(seconds=phrase_timeout):
                    phrase_complete = True
                # This is the last time we received new audio data from the queue.
                phrase_time = now

                # Combine audio data from queue
                audio_data = b''.join([data_queue.get() for _ in range(data_queue.qsize())])

                # Convert in-ram buffer to something the model can use directly without needing a temp file.
                # Convert data from 16 bit wide integers to floating point with a width of 32 bits.
                audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

                # Read the transcription.
                result = get_whisper_model().transcribe(audio_np, fp16=torch.cuda.is_available())
                text = result['text'].strip()

                # If we detected a pause between recordings, add a new item to our transcription.
                # Otherwise edit the existing one.
                with transcriptions_lock:
                    if phrase_complete:
                        # Prevent duplicate transcriptions
                        if not transcriptions or (transcriptions and transcriptions[-1] != text):
                            transcription.append(text)
                            transcriptions.append(text)  # Save to global transcriptions
                            print(f"New transcription added: {text}")
                    else:
                        # Update the last transcription
                        if transcriptions and transcriptions[-1] != text:
                            transcription[-1] = text
                            transcriptions[-1] = text  # Update the last item
                            print(f"Transcription updated: {text}")

                print('', end='', flush=True)
            else:
                # Infinite loops are bad for processors, must sleep.
                sleep(0.25)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error in transcription thread: {e}")
            sleep(1)

    print("\n\nTranscription:")
    for line in transcription:
        print(line)


def transcribe_file(path):
    """Transcribe an audio file.

    Uses OpenAI's hosted Whisper by default so the app runs without a local
    model download. Set TRANSCRIBE_BACKEND=local to use the bundled
    openai-whisper model instead (requires torch, see requirements-local.txt).
    """
    if os.getenv("TRANSCRIBE_BACKEND", "api") == "local":
        if not STT_AVAILABLE:
            raise RuntimeError(f"Local backend unavailable: {STT_IMPORT_ERROR}")
        return get_whisper_model().transcribe(path)["text"]

    with open(path, "rb") as fh:
        result = openai.audio.transcriptions.create(model="whisper-1", file=fh)
    return result.text


# Flask Routes

@app.route("/")
def main():
    return render_template('main.html')

@app.route("/home")
def index():
    return render_template('index.html')

@app.route("/summary")
def summary():
    text = " ".join(transcriptions) if transcriptions else ""

    if not text.strip():
        return jsonify({"error": "No transcription data available."}), 400

    prompt = f"Summarize the following text in bullet points, making it easy to understand by highlighting main topics and key points: {text}"

    try:
        summary_response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=300
        )
        summary_text = summary_response.choices[0].message.content

        saved_summaries.append({
            "title": f"Summary {len(saved_summaries) + 1}",
            "category": "General",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content": summary_text
        })

        keywords_response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": f"Extract relevant keywords from this text: {text}"}
            ],
            max_tokens=50
        )
        keywords_list = [keyword.strip() for keyword in keywords_response.choices[0].message.content.split(",")]

        return render_template("summary.html", summary=summary_text, keywords_list=keywords_list)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/transcribe", methods=["POST"])
def transcribe_audio():
    try:
        if 'file' not in request.files:
            print("No file part in the request")
            return jsonify({"error": "No audio file provided"}), 400

        file = request.files['file']

        if file.filename == '':
            print("No selected file")
            return jsonify({"error": "No selected file"}), 400

        # Read audio data
        audio_data = file.read()
        if not audio_data:
            print("No audio data received")
            return jsonify({"error": "No audio data received"}), 400

        print(f"Received audio data of size: {len(audio_data)} bytes")

        # The browser sends a compressed container (WebM/Opus or MP4), not raw
        # PCM, so it cannot be fed to np.frombuffer. Write it to a temp file and
        # let the transcription backend decode it.
        suffix = os.path.splitext(file.filename)[1] or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name

        try:
            text = transcribe_file(tmp_path)
        finally:
            os.unlink(tmp_path)

        text = (text or "").strip()
        if not text:
            return jsonify({"transcription": "", "message": "No speech detected"}), 200

        with transcriptions_lock:
            transcriptions.append(text)

        return jsonify({"transcription": text}), 200

    except Exception as e:
        print(f"Error in /transcribe: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/keyword_summary", methods=["GET"])
def keyword_summary():
    keyword = request.args.get("keyword")
    
    if not keyword:
        return jsonify({"error": "No keyword provided"}), 400

    try:
        # Generate a detailed explanation for the keyword
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an expert in providing concise explanations."},
                {"role": "user", "content": f"Explain the following keyword in simple terms: {keyword}"}
            ],
            max_tokens=100
        )
        explanation = response.choices[0].message.content
        return jsonify({"summary": explanation})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    summary_context = "Here is a summary of the main topics for context:\n" + " ".join(transcriptions)

    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "assistant", "content": summary_context},
                {"role": "user", "content": user_message}
            ]
        )
        ai_response = response.choices[0].message.content
        return jsonify({"response": ai_response})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/review")
def review():
    """
    Render a review page displaying all saved summaries with titles, dates, 
    categories, and links to detailed summary pages.
    """
    return render_template("review.html", summaries=saved_summaries)

@app.route("/review/<int:summary_id>", methods=["GET", "POST"])
def review_summary(summary_id):
    """
    특정 요약을 수정할 수 있는 페이지를 표시하거나 업데이트합니다.
    """
    if 0 <= summary_id < len(saved_summaries):
        summary = saved_summaries[summary_id]

        if request.method == "POST":
            # 클라이언트로부터 업데이트된 데이터를 받음
            updated_title = request.form.get("title", summary["title"])
            updated_content = request.form.get("content", summary["content"])
            updated_keywords = request.form.get("keywords", "").split(",")
            updated_ai_chat = request.form.get("ai_chat", summary.get("ai_chat", ""))

            # 저장된 데이터를 업데이트
            summary["title"] = updated_title
            summary["content"] = updated_content
            summary["keywords"] = updated_keywords
            summary["ai_chat"] = updated_ai_chat

            return jsonify({"message": "Summary updated successfully!"}), 200

        # 수정 가능한 데이터와 함께 템플릿 렌더링
        return render_template(
            "edit_summary.html",
            summary=summary,
            keywords=summary.get("keywords", []),
            ai_chat=summary.get("ai_chat", ""),
            is_edit=True,
        )
    return jsonify({"error": "Summary not found"}), 404

@app.route("/current_transcription", methods=["GET"])
def current_transcription():
    with transcriptions_lock:
        if not transcriptions:
            return jsonify({"transcription": ""})
        # 최신 텍스트만 반환
        return jsonify({"transcription": transcriptions[-1]})

@app.route("/send_audio", methods=["POST"])
def send_audio_route():
    """
    Receive audio data from client and put it into the queue for transcription.
    """
    try:
        audio_data = request.get_data()
        if not audio_data:
            return jsonify({"error": "No audio data received"}), 400
        data_queue.put(audio_data)
        return jsonify({"message": "Audio data received"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Mic capture runs on the machine hosting the server, so it is opt-in.
    # Enable with ENABLE_MIC=1 when running locally with a microphone.
    if os.getenv("ENABLE_MIC") == "1":
        if STT_AVAILABLE:
            transcription_thread = threading.Thread(target=real_time_transcription, daemon=True)
            transcription_thread.start()
        else:
            print(f"ENABLE_MIC=1 but speech deps are missing: {STT_IMPORT_ERROR}")
            print("Install them with: pip install -r requirements.txt")

    app.run(debug=True, port=int(os.getenv("PORT", 5001)))
